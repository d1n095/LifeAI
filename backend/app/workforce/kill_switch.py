"""Immediate revoke / kill-switch for workforce activation and live assignments.

Activation can be revoked without leaving stale authority or reusable assignments.
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
    reset_activation_gates_for_tests,
)
from app.workforce.authority import revoke_assignment_authority


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


_STATE = KillSwitchState()


def get_kill_switch() -> KillSwitchState:
    return _STATE


def reset_kill_switch_for_tests() -> None:
    global _STATE
    _STATE = KillSwitchState()


def assert_not_killed() -> None:
    if _STATE.active:
        raise KillSwitchError(f"workforce kill switch active: {_STATE.reason}")


def activate_kill_switch(
    db: Session,
    *,
    owner_id: uuid.UUID,
    reason: str,
) -> KillSwitchState:
    """Revoke all non-terminal assignments for owner + clear activation gates to unknown.

    Does NOT mark gates failed from CI — sets UNKNOWN so provider path fail-closes.
    """
    global _STATE
    live = list(
        db.execute(
            select(WorkforceAssignment).where(
                WorkforceAssignment.owner_id == owner_id,
                WorkforceAssignment.status.in_(
                    ("assigned", "running", "awaiting_verification")
                ),
            )
        ).scalars()
    )
    revoked: list[str] = []
    for asg in live:
        revoke_assignment_authority(asg, reason=f"kill_switch:{reason}")
        asg.status = "revoked"
        asg.updated_at = datetime.utcnow()
        revoked.append(str(asg.id))
    db.flush()

    # Clear activation — UNKNOWN != VERIFIED → fail closed for provider.
    for key in REQUIRED_ACTIVATION_GATES:
        g = get_activation_gates().gates.get(key)
        if g and g.status == GateStatus.verified:
            record_gate_verification(
                key,
                status=GateStatus.unknown,
                evidence_ref=None,
                notes=f"cleared_by_kill_switch:{reason}",
            )

    _STATE = KillSwitchState(
        active=True,
        reason=reason,
        activated_at=datetime.utcnow().isoformat() + "Z",
        revoked_assignment_ids=revoked,
    )
    return _STATE


def clear_kill_switch_for_recovery(*, founder_ack: str) -> KillSwitchState:
    """Founder must explicitly clear — not automatic."""
    if not founder_ack:
        raise KillSwitchError("founder_ack required to clear kill switch")
    global _STATE
    _STATE = KillSwitchState(active=False, reason=f"cleared:{founder_ack}")
    return _STATE


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
