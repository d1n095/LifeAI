"""Owner-scoped stop vs global emergency stop — durable, never auto-cleared by boot.

BOOT != FOUNDER ACK.
RESTART != FOUNDER ACK.
MAINAI != FOUNDER.
MAINAI MAY NOT ACKNOWLEDGE HER OWN SAFETY CLEAR.
PROCESS START MUST NEVER SILENTLY REMOVE A STOP CONDITION.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.workforce import WorkforceAssignment
from app.workforce.activation_gates import (
    GateStatus,
    REQUIRED_ACTIVATION_GATES,
    get_activation_gates,
    record_gate_verification,
)
from app.workforce.authority import assignment_authority_is_live, revoke_assignment_authority

# Fabricated strings used historically by boot/composed paths — forever rejected.
_FABRICATED_ACK_DENYLIST = frozenset(
    {
        "composed_safe_internal_clear",
        "first_real_internal_boot",
        "post_shutdown_clear",
        "safe_internal_clear",
        "boot_clear",
        "auto_clear",
        "test",
    }
)
_ACK_PREFIX_RE = re.compile(r"^(founder_ack|operator_ack):.{8,}$")


class KillSwitchError(Exception):
    def __init__(self, message: str, *, code: str = "KILL_SWITCH_ACTIVE", details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass
class KillSwitchState:
    """Compatibility view — prefer query_stop_status for scoped truth."""

    active: bool = False
    reason: str = ""
    activated_at: str | None = None
    revoked_assignment_ids: list[str] = field(default_factory=list)
    scope: str | None = None
    owner_id: str | None = None
    sequence: int = 0

    def as_dict(self) -> dict:
        return {
            "active": self.active,
            "reason": self.reason,
            "activated_at": self.activated_at,
            "revoked_assignment_ids": list(self.revoked_assignment_ids),
            "scope": self.scope,
            "owner_id": self.owner_id,
            "sequence": self.sequence,
        }


def _now() -> datetime:
    return datetime.utcnow()


def _validate_founder_ack(founder_ack: str) -> None:
    if not founder_ack or not str(founder_ack).strip():
        raise KillSwitchError("founder_ack required to clear kill switch", code="ACK_REQUIRED")
    ack = str(founder_ack).strip()
    if ack in _FABRICATED_ACK_DENYLIST or ack.lower() in _FABRICATED_ACK_DENYLIST:
        raise KillSwitchError(
            "fabricated founder_ack rejected — BOOT != FOUNDER ACK",
            code="FABRICATED_ACK",
            details={"ack": ack},
        )
    if not _ACK_PREFIX_RE.match(ack):
        raise KillSwitchError(
            "founder_ack must be 'founder_ack:<explicit text>' or 'operator_ack:<explicit text>'",
            code="ACK_FORMAT",
        )


def _get_or_create_state(db: Session, *, scope: str, owner_id: uuid.UUID | None) -> dict[str, Any]:
    """Race-safe ensure of stop-state row (unique indexes on global/owner)."""
    if scope == "global":
        row = db.execute(
            text(
                "SELECT id, scope, owner_id, active, reason, sequence, updated_at "
                "FROM mainai_stop_state WHERE scope = 'global' LIMIT 1"
            )
        ).mappings().first()
        if row is not None:
            return dict(row)
        try:
            with db.begin_nested():
                db.execute(
                    text(
                        "INSERT INTO mainai_stop_state (scope, owner_id, active, reason, sequence) "
                        "VALUES ('global', NULL, false, '', 0)"
                    )
                )
        except Exception:
            # Concurrent creator won unique index — fall through to SELECT.
            pass
        row = db.execute(
            text(
                "SELECT id, scope, owner_id, active, reason, sequence, updated_at "
                "FROM mainai_stop_state WHERE scope = 'global' LIMIT 1"
            )
        ).mappings().one()
        return dict(row)

    row = db.execute(
        text(
            "SELECT id, scope, owner_id, active, reason, sequence, updated_at "
            "FROM mainai_stop_state WHERE scope = 'owner' AND owner_id = :oid LIMIT 1"
        ),
        {"oid": str(owner_id)},
    ).mappings().first()
    if row is not None:
        return dict(row)
    try:
        with db.begin_nested():
            db.execute(
                text(
                    "INSERT INTO mainai_stop_state (scope, owner_id, active, reason, sequence) "
                    "VALUES ('owner', :oid, false, '', 0)"
                ),
                {"oid": str(owner_id)},
            )
    except Exception:
        pass
    row = db.execute(
        text(
            "SELECT id, scope, owner_id, active, reason, sequence, updated_at "
            "FROM mainai_stop_state WHERE scope = 'owner' AND owner_id = :oid LIMIT 1"
        ),
        {"oid": str(owner_id)},
    ).mappings().one()
    return dict(row)


def _append_event(
    db: Session,
    *,
    scope: str,
    owner_id: uuid.UUID | None,
    event_kind: str,
    sequence: int,
    reason: str,
    actor_kind: str,
    founder_ack: str | None = None,
    clear_request_id: uuid.UUID | None = None,
    provenance: dict | None = None,
) -> None:
    db.execute(
        text(
            """
            INSERT INTO mainai_stop_events (
                scope, owner_id, event_kind, sequence, reason,
                founder_ack, clear_request_id, actor_kind, provenance
            ) VALUES (
                :scope, :owner_id, :event_kind, :sequence, :reason,
                :founder_ack, :clear_request_id, :actor_kind, CAST(:provenance AS jsonb)
            )
            """
        ),
        {
            "scope": scope,
            "owner_id": str(owner_id) if owner_id else None,
            "event_kind": event_kind,
            "sequence": sequence,
            "reason": reason,
            "founder_ack": founder_ack,
            "clear_request_id": str(clear_request_id) if clear_request_id else None,
            "actor_kind": actor_kind,
            "provenance": __import__("json").dumps(provenance or {}),
        },
    )
    db.flush()


def query_stop_status(db: Session, *, owner_id: uuid.UUID | None = None) -> dict[str, Any]:
    global_row = _get_or_create_state(db, scope="global", owner_id=None)
    owner_row = None
    if owner_id is not None:
        owner_row = _get_or_create_state(db, scope="owner", owner_id=owner_id)
    blocked = bool(global_row["active"]) or bool(owner_row and owner_row["active"])
    code = None
    if global_row["active"]:
        code = "BLOCKED_BY_GLOBAL_EMERGENCY_STOP"
    elif owner_row and owner_row["active"]:
        code = "BLOCKED_BY_OWNER_STOP"
    return {
        "blocked": blocked,
        "code": code,
        "global": {
            "active": bool(global_row["active"]),
            "reason": global_row["reason"],
            "sequence": int(global_row["sequence"]),
        },
        "owner": (
            {
                "active": bool(owner_row["active"]),
                "reason": owner_row["reason"],
                "sequence": int(owner_row["sequence"]),
                "owner_id": str(owner_id),
            }
            if owner_row
            else None
        ),
    }


def assert_not_killed(db: Session | None = None, *, owner_id: uuid.UUID | None = None) -> None:
    """Fail closed if global emergency OR this owner's stop is active.

    db is required for durable truth. If db is None (legacy callers), raises unless
    tests have reset — prefer always passing db.
    """
    if db is None:
        # Legacy no-db path: only check fragile process cache for backward compat during migrate.
        if _PROCESS_CACHE.get("global_active") or (
            owner_id and _PROCESS_CACHE.get("owners", {}).get(str(owner_id))
        ):
            raise KillSwitchError(
                "kill switch active (process cache; pass db for durable check)",
                code="KILL_SWITCH_ACTIVE",
            )
        return
    status = query_stop_status(db, owner_id=owner_id)
    if status["blocked"]:
        raise KillSwitchError(
            f"{status['code']}: {status}",
            code=status["code"] or "KILL_SWITCH_ACTIVE",
            details=status,
        )


def _lock_authority_fence(db: Session, *, owner_id: uuid.UUID | None) -> dict[str, Any]:
    """Durable serialization between STOP and GRANT.

    Locks global stop row (+ owner row when owner_id set) with FOR UPDATE so a concurrent
    grant cannot commit live authority after stop has revoked-and-activated (or vice versa).
    PROCESS FLAGS ARE NOT AUTHORITY.
    """
    # Ensure rows exist first (plain path), then lock.
    _get_or_create_state(db, scope="global", owner_id=None)
    if owner_id is not None:
        _get_or_create_state(db, scope="owner", owner_id=owner_id)
    db.flush()
    global_row = db.execute(
        text(
            "SELECT id, scope, owner_id, active, reason, sequence, updated_at "
            "FROM mainai_stop_state WHERE scope = 'global' FOR UPDATE"
        )
    ).mappings().one()
    owner_row = None
    if owner_id is not None:
        owner_row = db.execute(
            text(
                "SELECT id, scope, owner_id, active, reason, sequence, updated_at "
                "FROM mainai_stop_state WHERE scope = 'owner' AND owner_id = :oid FOR UPDATE"
            ),
            {"oid": str(owner_id)},
        ).mappings().one()
    return {"global": dict(global_row), "owner": dict(owner_row) if owner_row else None}


def assert_authority_grant_allowed(db: Session, *, owner_id: uuid.UUID) -> dict[str, Any]:
    """Call BEFORE minting any live WorkforceAssignment.

    Holds the durable stop fence lock for the remainder of the caller's transaction so a
    concurrent activate_* cannot slip a stop between this check and the grant INSERT.
    """
    fence = _lock_authority_fence(db, owner_id=owner_id)
    assert_not_killed(db, owner_id=owner_id)
    return {
        "global_sequence": int(fence["global"]["sequence"]),
        "owner_sequence": int(fence["owner"]["sequence"]) if fence["owner"] else 0,
        "fence": "mainai_stop_state_for_update",
    }


def activate_owner_stop(
    db: Session,
    *,
    owner_id: uuid.UUID,
    reason: str,
) -> KillSwitchState:
    """OWNER STOP — blocks only this owner's workforce/assignments.

    Serialized with grants via FOR UPDATE on mainai_stop_state.
    AFTER STOP COMMIT → no reusable live authority for this owner.
    """
    fence = _lock_authority_fence(db, owner_id=owner_id)
    state = fence["owner"]
    assert state is not None
    new_seq = int(state["sequence"]) + 1
    revoked = _revoke_owner_assignments(db, owner_id=owner_id, reason=reason)
    db.execute(
        text(
            "UPDATE mainai_stop_state SET active = true, reason = :reason, "
            "sequence = :seq, updated_at = now() WHERE scope = 'owner' AND owner_id = :oid"
        ),
        {"reason": reason, "seq": new_seq, "oid": str(owner_id)},
    )
    _append_event(
        db,
        scope="owner",
        owner_id=owner_id,
        event_kind="activate",
        sequence=new_seq,
        reason=reason,
        actor_kind="operator",
        provenance={"revoked": revoked, "fence": "mainai_stop_state_for_update"},
    )
    db.flush()
    if not prove_no_reusable_live_authority(db, owner_id=owner_id):
        raise KillSwitchError(
            "AFTER STOP COMMIT invariant failed: reusable live authority remains",
            code="AUTHORITY_SURVIVED_STOP",
            details={"revoked": revoked, "owner_id": str(owner_id)},
        )
    _PROCESS_CACHE.setdefault("owners", {})[str(owner_id)] = True
    return KillSwitchState(
        active=True,
        reason=reason,
        activated_at=_now().isoformat() + "Z",
        revoked_assignment_ids=revoked,
        scope="owner",
        owner_id=str(owner_id),
        sequence=new_seq,
    )


def activate_global_emergency_stop(
    db: Session,
    *,
    reason: str,
    founder_authority_ref: str,
) -> KillSwitchState:
    """GLOBAL SYSTEM EMERGENCY STOP — blocks everyone. Explicit separate control.

    Revokes ALL live assignments across owners under the durable fence.
    """
    if not founder_authority_ref or not str(founder_authority_ref).startswith("founder_ack:"):
        raise KillSwitchError(
            "global emergency requires founder_authority_ref starting with founder_ack:",
            code="ACK_REQUIRED",
        )
    fence = _lock_authority_fence(db, owner_id=None)
    state = fence["global"]
    new_seq = int(state["sequence"]) + 1
    # Clear activation gates globally so provider path fail-closes.
    for key in REQUIRED_ACTIVATION_GATES:
        g = get_activation_gates().gates.get(key)
        if g and g.status == GateStatus.verified:
            record_gate_verification(
                key,
                status=GateStatus.unknown,
                evidence_ref=None,
                notes=f"cleared_by_global_emergency:{reason}",
            )
    revoked = _revoke_all_live_assignments(db, reason=reason)
    db.execute(
        text(
            "UPDATE mainai_stop_state SET active = true, reason = :reason, "
            "sequence = :seq, updated_at = now() WHERE scope = 'global'"
        ),
        {"reason": reason, "seq": new_seq},
    )
    _append_event(
        db,
        scope="global",
        owner_id=None,
        event_kind="activate",
        sequence=new_seq,
        reason=reason,
        actor_kind="founder",
        founder_ack=founder_authority_ref,
        provenance={"global_emergency": True, "revoked": revoked, "fence": "mainai_stop_state_for_update"},
    )
    db.flush()
    # Global: every owner with any assignment must have no live authority.
    lingering = db.execute(
        select(WorkforceAssignment).where(WorkforceAssignment.revoked_at.is_(None))
    ).scalars().all()
    for asg in lingering:
        if assignment_authority_is_live(asg).live:
            raise KillSwitchError(
                "AFTER GLOBAL STOP: reusable live authority remains",
                code="AUTHORITY_SURVIVED_STOP",
                details={"assignment_id": str(asg.id), "owner_id": str(asg.owner_id)},
            )
    _PROCESS_CACHE["global_active"] = True
    return KillSwitchState(
        active=True,
        reason=reason,
        activated_at=_now().isoformat() + "Z",
        revoked_assignment_ids=revoked,
        scope="global",
        sequence=new_seq,
    )


def activate_kill_switch(
    db: Session,
    *,
    owner_id: uuid.UUID,
    reason: str,
) -> KillSwitchState:
    """Backward-compatible name → OWNER STOP (not global)."""
    return activate_owner_stop(db, owner_id=owner_id, reason=reason)


def clear_owner_stop(
    db: Session,
    *,
    owner_id: uuid.UUID,
    founder_ack: str,
    clear_request_id: uuid.UUID,
    expected_sequence: int | None = None,
) -> KillSwitchState:
    """Explicit founder/operator recovery for ONE owner. Never called by boot."""
    _validate_founder_ack(founder_ack)
    # Replay protection: clear_request_id unique
    existing = db.execute(
        text("SELECT id FROM mainai_stop_events WHERE clear_request_id = :rid LIMIT 1"),
        {"rid": str(clear_request_id)},
    ).first()
    if existing:
        raise KillSwitchError(
            "stale/replayed clear_request_id rejected",
            code="CLEAR_REPLAY",
            details={"clear_request_id": str(clear_request_id)},
        )
    state = _get_or_create_state(db, scope="owner", owner_id=owner_id)
    if expected_sequence is not None and int(state["sequence"]) != int(expected_sequence):
        _append_event(
            db,
            scope="owner",
            owner_id=owner_id,
            event_kind="reject_clear",
            sequence=int(state["sequence"]),
            reason="sequence_mismatch",
            actor_kind="system",
            founder_ack=founder_ack,
            clear_request_id=clear_request_id,
            provenance={"expected": expected_sequence, "actual": int(state["sequence"])},
        )
        raise KillSwitchError(
            "clear rejected: stop sequence advanced (stale recovery request)",
            code="STALE_SEQUENCE",
            details={"expected": expected_sequence, "actual": int(state["sequence"])},
        )
    new_seq = int(state["sequence"]) + 1
    db.execute(
        text(
            "UPDATE mainai_stop_state SET active = false, reason = :reason, "
            "sequence = :seq, updated_at = now() WHERE scope = 'owner' AND owner_id = :oid"
        ),
        {"reason": f"cleared:{founder_ack[:80]}", "seq": new_seq, "oid": str(owner_id)},
    )
    _append_event(
        db,
        scope="owner",
        owner_id=owner_id,
        event_kind="clear",
        sequence=new_seq,
        reason="owner_stop_cleared",
        actor_kind="founder" if founder_ack.startswith("founder_ack:") else "operator",
        founder_ack=founder_ack,
        clear_request_id=clear_request_id,
    )
    _PROCESS_CACHE.setdefault("owners", {}).pop(str(owner_id), None)
    return KillSwitchState(active=False, reason=f"cleared:{founder_ack[:80]}", scope="owner", owner_id=str(owner_id), sequence=new_seq)


def clear_global_emergency_stop(
    db: Session,
    *,
    founder_ack: str,
    clear_request_id: uuid.UUID,
    expected_sequence: int | None = None,
) -> KillSwitchState:
    """Clear GLOBAL only — does not invent owner authority or clear owner stops."""
    _validate_founder_ack(founder_ack)
    if not founder_ack.startswith("founder_ack:"):
        raise KillSwitchError("global clear requires founder_ack: prefix", code="ACK_REQUIRED")
    existing = db.execute(
        text("SELECT id FROM mainai_stop_events WHERE clear_request_id = :rid LIMIT 1"),
        {"rid": str(clear_request_id)},
    ).first()
    if existing:
        raise KillSwitchError("stale/replayed clear_request_id rejected", code="CLEAR_REPLAY")
    state = _get_or_create_state(db, scope="global", owner_id=None)
    if expected_sequence is not None and int(state["sequence"]) != int(expected_sequence):
        _append_event(
            db,
            scope="global",
            owner_id=None,
            event_kind="reject_clear",
            sequence=int(state["sequence"]),
            reason="sequence_mismatch",
            actor_kind="system",
            founder_ack=founder_ack,
            clear_request_id=clear_request_id,
        )
        raise KillSwitchError("clear rejected: global sequence advanced", code="STALE_SEQUENCE")
    new_seq = int(state["sequence"]) + 1
    db.execute(
        text(
            "UPDATE mainai_stop_state SET active = false, reason = :reason, "
            "sequence = :seq, updated_at = now() WHERE scope = 'global'"
        ),
        {"reason": f"cleared:{founder_ack[:80]}", "seq": new_seq},
    )
    _append_event(
        db,
        scope="global",
        owner_id=None,
        event_kind="clear",
        sequence=new_seq,
        reason="global_emergency_cleared",
        actor_kind="founder",
        founder_ack=founder_ack,
        clear_request_id=clear_request_id,
    )
    _PROCESS_CACHE["global_active"] = False
    return KillSwitchState(active=False, reason=f"cleared:{founder_ack[:80]}", scope="global", sequence=new_seq)


def clear_kill_switch_for_recovery(
    *,
    founder_ack: str,
    db: Session | None = None,
    owner_id: uuid.UUID | None = None,
    clear_request_id: uuid.UUID | None = None,
    scope: str = "owner",
) -> KillSwitchState:
    """Explicit recovery only. Requires db + clear_request_id. Boot must NOT call this."""
    _validate_founder_ack(founder_ack)
    if db is None or clear_request_id is None:
        raise KillSwitchError(
            "clear requires db and clear_request_id — automatic/boot clear forbidden",
            code="CLEAR_REQUIRES_EXPLICIT_REQUEST",
        )
    if scope == "global":
        return clear_global_emergency_stop(
            db, founder_ack=founder_ack, clear_request_id=clear_request_id
        )
    if owner_id is None:
        raise KillSwitchError("owner clear requires owner_id", code="OWNER_REQUIRED")
    return clear_owner_stop(
        db, owner_id=owner_id, founder_ack=founder_ack, clear_request_id=clear_request_id
    )


def record_boot_blocked(db: Session, *, owner_id: uuid.UUID | None, reason: str) -> None:
    """Audit boot refusal — scope/sequence/reason must come from ONE blocking stop identity."""
    status = query_stop_status(db, owner_id=owner_id)
    if status.get("global", {}).get("active"):
        scope = "global"
        event_owner_id = None
        seq = int(status["global"]["sequence"])
        stop_reason = status["global"].get("reason") or reason
    elif status.get("owner", {}).get("active"):
        scope = "owner"
        event_owner_id = owner_id
        seq = int(status["owner"]["sequence"])
        stop_reason = status["owner"].get("reason") or reason
    else:
        # Boot blocked without active stop (should be rare) — still audit as system.
        scope = "global"
        event_owner_id = None
        seq = int((status.get("global") or {}).get("sequence") or 0)
        stop_reason = reason
    _append_event(
        db,
        scope=scope,
        owner_id=event_owner_id,
        event_kind="boot_blocked",
        sequence=seq,
        reason=stop_reason,
        actor_kind="system",
        provenance={
            "boot_cannot_clear": True,
            "status": status,
            "caller_reason": reason,
            "blocking_identity": scope,
        },
    )


def get_kill_switch() -> KillSwitchState:
    """Process-cache view only — prefer query_stop_status(db)."""
    return KillSwitchState(
        active=bool(_PROCESS_CACHE.get("global_active"))
        or bool(_PROCESS_CACHE.get("owners")),
        reason="process_cache_only_use_query_stop_status",
        scope="mixed",
    )


def reset_kill_switch_for_tests(db: Session | None = None) -> None:
    """TEST-ONLY. Never call from production boot."""
    global _PROCESS_CACHE
    _PROCESS_CACHE = {"global_active": False, "owners": {}}
    if db is not None:
        try:
            db.execute(text("UPDATE mainai_stop_state SET active = false, reason = '', sequence = sequence"))
            db.flush()
        except Exception:
            pass


def prove_no_reusable_live_authority(db: Session, *, owner_id: uuid.UUID) -> bool:
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


def _revoke_owner_assignments(db: Session, *, owner_id: uuid.UUID, reason: str) -> list[str]:
    """Revoke every still-live assignment for owner (not status-list alone)."""
    rows = list(
        db.execute(
            select(WorkforceAssignment).where(WorkforceAssignment.owner_id == owner_id)
        ).scalars()
    )
    revoked: list[str] = []
    for asg in rows:
        if not assignment_authority_is_live(asg).live:
            continue
        revoke_assignment_authority(asg, reason=f"kill_switch:{reason}")
        asg.status = "revoked"
        asg.updated_at = datetime.utcnow()
        revoked.append(str(asg.id))
    db.flush()
    return revoked


def _revoke_all_live_assignments(db: Session, *, reason: str) -> list[str]:
    rows = list(db.execute(select(WorkforceAssignment)).scalars())
    revoked: list[str] = []
    for asg in rows:
        if not assignment_authority_is_live(asg).live:
            continue
        revoke_assignment_authority(asg, reason=f"kill_switch_global:{reason}")
        asg.status = "revoked"
        asg.updated_at = datetime.utcnow()
        revoked.append(str(asg.id))
    db.flush()
    return revoked


_PROCESS_CACHE: dict[str, Any] = {"global_active": False, "owners": {}}
