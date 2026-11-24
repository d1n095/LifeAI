"""Read-only work ownership for one proposed change (Phase 2).

Answers a narrower question than `app.agent_coordination.founder_intelligence`: not "what is
every agent doing", but "is another non-terminal assignment already scoped to a path this
change would touch". It reuses the existing `agent_work_assignments` / `agent_scope_leases`
tables and the SAME deterministic path-prefix rule the coordinator enforces
(`app.agent_coordination.service.paths_conflict_any`). No table, migration, lease, dispatch or
write is added here.

Honesty rules:

- A non-terminal assignment is evidence of an ASSIGNMENT, not proof that an agent is alive.
  `lease_active` and `lease_generation` are reported separately so a `planned` assignment with
  no lease is never described as "running".
- An assignment with an empty `allowed_paths` is `unscoped`, not "conflicts with everything".
  The report says `unclear` rather than inventing ownership.
- A terminal assignment (completed/failed/cancelled/superseded) is never an owner.
- An expired lease is not active, even though its row still says `active` until a sweep.
- Every query filters `owner_id` and expects the caller's session to carry the same RLS
  scope used elsewhere (`app.current_user_id`); a different owner's rows are not visible.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_coordination.service import is_lease_stale, paths_conflict_any
from app.models.agent_coordination import (
    TERMINAL_WORK_ASSIGNMENT_STATUSES,
    AgentScopeLease,
    AgentScopeLeaseStatus,
    AgentWorkAssignment,
    CoordinationAgent,
)

OWNED = "owned"
CLEAR = "clear"
UNCLEAR = "unclear"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class WorkOwner:
    assignment_id: str
    agent_key: str | None
    role: str
    status: str
    read_write_mode: str
    branch: str | None
    allowed_paths: tuple[str, ...]
    conflicting_paths: tuple[str, ...]
    unscoped: bool
    lease_generation: int | None
    lease_mode: str | None
    lease_expires_at: str | None

    def to_dict(self) -> dict:
        return {
            "assignment_id": self.assignment_id,
            "agent_key": self.agent_key,
            "role": self.role,
            "status": self.status,
            "read_write_mode": self.read_write_mode,
            "branch": self.branch,
            "allowed_paths": list(self.allowed_paths),
            "conflicting_paths": list(self.conflicting_paths),
            "unscoped": self.unscoped,
            "lease_generation": self.lease_generation,
            "lease_mode": self.lease_mode,
            "lease_expires_at": self.lease_expires_at,
        }

    def evidence_ref(self) -> str:
        scope = ",".join(self.conflicting_paths) if self.conflicting_paths else "no path scope"
        lease = (
            f", lease generation {self.lease_generation}"
            if self.lease_generation is not None
            else ", no live lease"
        )
        return (
            f"active assignment {self.assignment_id}: "
            f"agent={self.agent_key or 'unknown'}, role={self.role}, "
            f"status={self.status}, mode={self.read_write_mode}, "
            f"branch={self.branch or 'none'}, scope={scope}{lease}"
        )


@dataclass(frozen=True)
class OwnershipReport:
    requested_paths: tuple[str, ...]
    owners: tuple[WorkOwner, ...]
    status: str
    reason: str
    execution_authorized: bool = False

    def evidence_refs(self) -> tuple[str, ...]:
        return tuple(owner.evidence_ref() for owner in self.owners)

    def to_dict(self) -> dict:
        return {
            "requested_paths": list(self.requested_paths),
            "status": self.status,
            "reason": self.reason,
            "owners": [owner.to_dict() for owner in self.owners],
            "execution_authorized": False,
        }


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _normalize(paths: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(path.strip() for path in paths if path and path.strip())


def find_work_owners(
    db: Session,
    *,
    owner_id: uuid.UUID,
    paths: tuple[str, ...] | list[str],
    exclude_assignment_id: uuid.UUID | None = None,
) -> OwnershipReport:
    """Report non-terminal assignments whose scope overlaps `paths`.

    Read-only. The caller is responsible for binding the same RLS scope as the rest of the
    request (`app.current_user_id`); this function additionally filters `owner_id` so a
    session that somehow sees more rows still cannot attribute them to another owner.
    """
    requested = _normalize(paths)
    if not requested:
        return OwnershipReport(
            requested_paths=(),
            owners=(),
            status=UNKNOWN,
            reason="no paths requested; ownership cannot be inferred from an empty scope",
        )

    terminal = tuple(status.value for status in TERMINAL_WORK_ASSIGNMENT_STATUSES)
    assignments = list(
        db.execute(
            select(AgentWorkAssignment).where(
                AgentWorkAssignment.owner_id == owner_id,
                AgentWorkAssignment.status.notin_(terminal),
            )
        ).scalars()
    )
    if exclude_assignment_id is not None:
        assignments = [a for a in assignments if a.id != exclude_assignment_id]
    if not assignments:
        return OwnershipReport(
            requested_paths=requested,
            owners=(),
            status=CLEAR,
            reason="no non-terminal assignment exists for this owner",
        )

    agent_ids = {assignment.agent_id for assignment in assignments}
    agents = {
        agent.id: agent
        for agent in db.execute(
            select(CoordinationAgent).where(CoordinationAgent.id.in_(agent_ids))
        ).scalars()
    }

    leases = list(
        db.execute(
            select(AgentScopeLease).where(
                AgentScopeLease.owner_id == owner_id,
                AgentScopeLease.assignment_id.in_([a.id for a in assignments]),
                AgentScopeLease.status == AgentScopeLeaseStatus.active,
            )
        ).scalars()
    )
    live_leases: dict[uuid.UUID, AgentScopeLease] = {}
    for lease in leases:
        if is_lease_stale(lease):
            continue
        current = live_leases.get(lease.assignment_id)
        if current is None or lease.lease_generation > current.lease_generation:
            live_leases[lease.assignment_id] = lease

    owners: list[WorkOwner] = []
    for assignment in assignments:
        allowed = tuple(assignment.allowed_paths or ())
        conflicting = tuple(
            path for path in requested if paths_conflict_any((path,), allowed)
        )
        unscoped = not allowed
        if not conflicting and not unscoped:
            continue
        lease = live_leases.get(assignment.id)
        agent = agents.get(assignment.agent_id)
        owners.append(
            WorkOwner(
                assignment_id=str(assignment.id),
                agent_key=agent.agent_key if agent is not None else None,
                role=_enum_value(assignment.role),
                status=_enum_value(assignment.status),
                read_write_mode=_enum_value(assignment.read_write_mode),
                branch=assignment.branch,
                allowed_paths=allowed,
                conflicting_paths=conflicting,
                unscoped=unscoped,
                lease_generation=lease.lease_generation if lease is not None else None,
                lease_mode=_enum_value(lease.mode) if lease is not None else None,
                lease_expires_at=(
                    lease.expires_at.isoformat()
                    if lease is not None and lease.expires_at is not None
                    else None
                ),
            )
        )

    owners.sort(key=lambda owner: (not owner.conflicting_paths, owner.assignment_id))
    if any(owner.conflicting_paths for owner in owners):
        status = OWNED
        reason = "at least one non-terminal assignment already covers a requested path"
    elif owners:
        status = UNCLEAR
        reason = "assignment(s) exist without a path scope; review manually"
    else:
        status = CLEAR
        reason = "no non-terminal assignment overlaps the requested paths"

    return OwnershipReport(
        requested_paths=requested,
        owners=tuple(owners),
        status=status,
        reason=reason,
    )
