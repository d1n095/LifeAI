"""Immediate revoke / kill-switch for workforce activation and live assignments.

Activation can be revoked without leaving stale authority or reusable assignments.

Per-owner scoped by design: activate_kill_switch(owner_id=A) must only ever affect
owner A. A single process-global flag here would mean any one owner's kill event
silently disables workforce execution for EVERY other owner sharing the process --
a real cross-owner denial-of-service via a legitimate, expected code path
(run_first_safe_internal_mainai_run calls activate_kill_switch at the end of every
successful run). See docs/MAINAI_FIRST_SAFE_INTERNAL_RUN.md.

A TRUE global emergency stop is a real, distinct, intentional capability -- kept as
its own explicit function (activate_global_kill_switch) so the per-owner path can
never accidentally BE the global one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.workforce import WorkforceAssignment
from app.workforce.activation_gates import (
    GateStatus,
    REQUIRED_ACTIVATION_GATES,
    get_activation_gates,
    record_gate_verification,
)
from app.workforce.authority import revoke_assignment_authority

_LIVE_STATUSES = ("assigned", "running", "awaiting_verification")


class KillSwitchError(Exception):
    pass


@dataclass
class KillSwitchState:
    active: bool = False
    reason: str = ""
    activated_at: str | None = None
    revoked_assignment_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "active": self.active,
            "reason": self.reason,
            "activated_at": self.activated_at,
            "revoked_assignment_ids": list(self.revoked_assignment_ids),
        }


# Per-owner kill state. A missing key means "never activated for this owner" --
# treated the same as an explicit inactive KillSwitchState().
_STATE: dict[uuid.UUID, KillSwitchState] = {}

# TRUE global emergency stop -- distinct from any single owner's state. Only
# activate_global_kill_switch()/clear it via clear_kill_switch_for_recovery(owner_id=None).
_GLOBAL_STATE: KillSwitchState = KillSwitchState()


def get_kill_switch(owner_id: uuid.UUID) -> KillSwitchState:
    """This owner's kill-switch state. Does NOT reflect the true global stop --
    callers that also need to know about a global stop should check
    get_global_kill_switch() (assert_not_killed() already checks both)."""
    return _STATE.get(owner_id, KillSwitchState())


def get_global_kill_switch() -> KillSwitchState:
    return _GLOBAL_STATE


def reset_kill_switch_for_tests() -> None:
    global _STATE, _GLOBAL_STATE
    _STATE = {}
    _GLOBAL_STATE = KillSwitchState()


def assert_not_killed(owner_id: uuid.UUID) -> None:
    """Fail closed if EITHER this owner's kill switch OR the true global stop is active.

    Requires owner_id: a bare global check here is exactly the cross-owner
    denial-of-service this module exists to prevent (see module docstring).
    """
    if _GLOBAL_STATE.active:
        raise KillSwitchError(f"workforce kill switch active (global): {_GLOBAL_STATE.reason}")
    state = _STATE.get(owner_id)
    if state is not None and state.active:
        raise KillSwitchError(f"workforce kill switch active: {state.reason}")


def _revoke_live_assignments(db: Session, *, owner_id: uuid.UUID | None, reason: str) -> list[str]:
    stmt = select(WorkforceAssignment).where(WorkforceAssignment.status.in_(_LIVE_STATUSES))
    if owner_id is not None:
        stmt = stmt.where(WorkforceAssignment.owner_id == owner_id)
    live = list(db.execute(stmt).scalars())
    revoked: list[str] = []
    for asg in live:
        revoke_assignment_authority(asg, reason=f"kill_switch:{reason}")
        asg.status = "revoked"
        asg.updated_at = datetime.utcnow()
        revoked.append(str(asg.id))
    db.flush()
    return revoked


def activate_kill_switch(
    db: Session,
    *,
    owner_id: uuid.UUID,
    reason: str,
) -> KillSwitchState:
    """Revoke all of THIS owner's non-terminal assignments and set THIS owner's kill state.

    Owner-scoped only: does NOT touch any other owner's state, and does NOT clear the
    process-wide activation gates (that would be a real cross-owner side effect --
    every owner sharing the process would have their provider-delegation eligibility
    silently reset just because one owner needed a kill switch). A caller that
    genuinely needs the true global emergency-stop semantics (revoke every owner +
    clear gates) must call activate_global_kill_switch() explicitly instead.
    """
    global _STATE
    revoked = _revoke_live_assignments(db, owner_id=owner_id, reason=reason)
    state = KillSwitchState(
        active=True,
        reason=reason,
        activated_at=datetime.utcnow().isoformat() + "Z",
        revoked_assignment_ids=revoked,
    )
    _STATE[owner_id] = state
    return state


def activate_global_kill_switch(db: Session, *, reason: str) -> KillSwitchState:
    """TRUE global emergency stop: revokes EVERY owner's live assignments and clears
    activation gates to UNKNOWN (UNKNOWN != VERIFIED -> fail closed for the provider
    path, for every owner, until re-verified). This is the explicit, distinct
    capability activate_kill_switch()'s own per-owner path must never accidentally be.
    """
    global _GLOBAL_STATE
    revoked = _revoke_live_assignments(db, owner_id=None, reason=reason)

    for key in REQUIRED_ACTIVATION_GATES:
        g = get_activation_gates().gates.get(key)
        if g and g.status == GateStatus.verified:
            record_gate_verification(
                key,
                status=GateStatus.unknown,
                evidence_ref=None,
                notes=f"cleared_by_global_kill_switch:{reason}",
            )

    _GLOBAL_STATE = KillSwitchState(
        active=True,
        reason=reason,
        activated_at=datetime.utcnow().isoformat() + "Z",
        revoked_assignment_ids=revoked,
    )
    return _GLOBAL_STATE


def clear_kill_switch_for_recovery(
    *, founder_ack: str, owner_id: uuid.UUID | None = None
) -> KillSwitchState:
    """Founder must explicitly clear — not automatic.

    owner_id=None clears the TRUE global stop (matching activate_global_kill_switch());
    pass the specific owner_id to clear only that owner's kill state.
    """
    if not founder_ack:
        raise KillSwitchError("founder_ack required to clear kill switch")
    if owner_id is None:
        global _GLOBAL_STATE
        _GLOBAL_STATE = KillSwitchState(active=False, reason=f"cleared:{founder_ack}")
        return _GLOBAL_STATE
    global _STATE
    state = KillSwitchState(active=False, reason=f"cleared:{founder_ack}")
    _STATE[owner_id] = state
    return state


def prove_no_reusable_live_authority(db: Session, *, owner_id: uuid.UUID) -> bool:
    """After kill switch: no non-terminal assignment still holds live authority."""
    from app.workforce.authority import assignment_authority_is_live

    terminal = frozenset(
        {"completed", "failed", "cancelled", "revoked", "expired", "superseded"}
    )
    rows = list(
        db.execute(
            select(WorkforceAssignment).where(WorkforceAssignment.owner_id == owner_id)
        ).scalars()
    )
    for asg in rows:
        if asg.status in terminal:
            continue
        if assignment_authority_is_live(asg).live:
            return False
    return True
