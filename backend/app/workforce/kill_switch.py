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

DURABLE, DB-BACKED, RACE-CLOSED (migration 0069, workforce_authority_epoch table):

Kill-switch state used to live in process-local Python globals (`_STATE`/`_GLOBAL_STATE`
dicts) -- fixed for OWNER-SCOPING by PR #239, but still process-local, which meant (a) a
stop committed by one process was invisible to every other uvicorn/gunicorn worker, and
(b) nothing serialized `activate_kill_switch()`'s "revoke all live assignments" SELECT
against a genuinely concurrent NEW assignment grant (`broker.resolve_delegation`) on a
separate DB connection -- a grant that committed in the gap between that SELECT returning
empty and the kill switch's own commit survived PERMANENTLY as live, unrevoked execution
authority while the kill switch itself reported active=True. `prove_no_reusable_live_
authority()` (this module's own safety oracle) correctly returns False for that state.

Fix: every scope (GLOBAL + one row per owner) now has a durable `authority_epoch` row in
Postgres (see the 0069 migration docstring for the full design/lock-ordering writeup).
`assert_grant_allowed()` is the actual fix -- the grant path (`broker.resolve_delegation`)
must call it, in the SAME transaction that inserts the new assignment, BEFORE that insert.
It takes a `SELECT ... FOR SHARE` lock on the GLOBAL row then this owner's row and refuses
the grant if either is stopped. `activate_kill_switch`/`activate_global_kill_switch` take
a conflicting `SELECT ... FOR UPDATE` on the same row(s) as part of the SAME transaction
that revokes live assignments -- Postgres's own lock manager, not application-level
timing, then enforces one strict ordering between any one grant and any one stop.
`assert_not_killed()` (checked again at assignment EXECUTION time in provider_worker.py)
is also now DB-backed, as a defense-in-depth second check -- a stop that lands after a
grant already committed but before that assignment executes is still caught there.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select, text
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

# Sentinel scope_key for the single true-global-stop row (workforce_authority_epoch.owner_id
# is NULL for this row -- see migration 0069).
_GLOBAL_SCOPE_KEY = "GLOBAL"


class KillSwitchError(Exception):
    def __init__(self, message: str, *, code: str = "KILL_SWITCH_ACTIVE", details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass
class KillSwitchState:
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


# BOOT != FOUNDER ACK — fabricated clear strings must never unlock stop.
_FABRICATED_ACK_DENYLIST = frozenset(
    {
        "composed_safe_internal_clear",
        "boot_clear",
        "auto_clear",
        "startup_clear",
        "safe_internal_clear",
    }
)
_ACK_PREFIX_RE = re.compile(r"^(founder_ack|operator_ack):.+\S")


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


def _owner_scope_key(owner_id: uuid.UUID) -> str:
    return str(owner_id)


def _ensure_epoch_row(db: Session, *, scope_key: str, owner_id: uuid.UUID | None) -> None:
    """Idempotent get-or-create for a scope's epoch row. Safe under concurrency: two
    sessions racing to create the SAME owner's row both run this INSERT .. ON CONFLICT DO
    NOTHING; Postgres's unique index (the primary key) serializes them, so at most one
    actually inserts and neither raises. The GLOBAL row is pre-seeded by migration 0069
    and this is a no-op for it thereafter."""
    db.execute(
        text(
            """
            INSERT INTO workforce_authority_epoch (scope_key, owner_id, epoch, stopped)
            VALUES (:scope_key, :owner_id, 0, false)
            ON CONFLICT (scope_key) DO NOTHING
            """
        ),
        {"scope_key": scope_key, "owner_id": str(owner_id) if owner_id is not None else None},
    )


def _lock_epoch_row_for_share(db: Session, *, scope_key: str, owner_id: uuid.UUID | None) -> dict:
    """Grant-side lock: SELECT .. FOR SHARE. Many concurrent grants (any owner) may hold
    this simultaneously -- readers don't block readers -- but Postgres queues this request
    behind any already-pending FOR UPDATE request on the same row (preventing writer
    starvation), and a NEW FOR UPDATE request blocks until every current FOR SHARE holder's
    transaction ends. That is the actual serialization primitive that closes the grant/stop
    race: this call, made before the new WorkforceAssignment is inserted, cannot return
    'not stopped' and then have a concurrent stop revoke nothing and still land -- either
    this grant's transaction commits first (stop then sees the new row and revokes it), or
    the stop's transaction commits first (this call blocks until then and re-reads stopped).
    """
    _ensure_epoch_row(db, scope_key=scope_key, owner_id=owner_id)
    row = db.execute(
        text(
            """
            SELECT epoch, stopped, reason, activated_at, revoked_assignment_ids
            FROM workforce_authority_epoch
            WHERE scope_key = :scope_key
            FOR SHARE
            """
        ),
        {"scope_key": scope_key},
    ).mappings().one()
    return dict(row)


def _lock_epoch_row_for_update(db: Session, *, scope_key: str, owner_id: uuid.UUID | None) -> dict:
    """Stop-side lock: SELECT .. FOR UPDATE -- exclusive, conflicts with FOR SHARE and with
    another concurrent FOR UPDATE on the same row. Blocks until every concurrent grant that
    already holds FOR SHARE on this row has committed or rolled back."""
    _ensure_epoch_row(db, scope_key=scope_key, owner_id=owner_id)
    row = db.execute(
        text(
            """
            SELECT epoch, stopped, reason, activated_at, revoked_assignment_ids
            FROM workforce_authority_epoch
            WHERE scope_key = :scope_key
            FOR UPDATE
            """
        ),
        {"scope_key": scope_key},
    ).mappings().one()
    return dict(row)


def _state_from_row(row: dict | None) -> KillSwitchState:
    if not row or not row.get("stopped"):
        return KillSwitchState(sequence=int((row or {}).get("epoch") or 0))
    activated_at = row.get("activated_at")
    return KillSwitchState(
        active=True,
        reason=row.get("reason") or "",
        activated_at=(activated_at.isoformat() + "Z") if activated_at else None,
        revoked_assignment_ids=list(row.get("revoked_assignment_ids") or []),
        sequence=int(row.get("epoch") or 0),
    )


def get_kill_switch(db: Session, owner_id: uuid.UUID) -> KillSwitchState:
    """This owner's kill-switch state, read fresh from the DB. Does NOT reflect the true
    global stop -- callers that also need to know about a global stop should check
    get_global_kill_switch() (assert_not_killed()/assert_grant_allowed() already check
    both)."""
    row = db.execute(
        text("SELECT epoch, stopped, reason, activated_at, revoked_assignment_ids "
             "FROM workforce_authority_epoch WHERE scope_key = :k"),
        {"k": _owner_scope_key(owner_id)},
    ).mappings().first()
    return _state_from_row(dict(row) if row else None)


def get_global_kill_switch(db: Session) -> KillSwitchState:
    row = db.execute(
        text("SELECT epoch, stopped, reason, activated_at, revoked_assignment_ids "
             "FROM workforce_authority_epoch WHERE scope_key = :k"),
        {"k": _GLOBAL_SCOPE_KEY},
    ).mappings().first()
    return _state_from_row(dict(row) if row else None)


def reset_kill_switch_for_tests(db: Session) -> None:
    """Test-only: reset ALL durable authority-epoch state. There is no process-local flag
    left to reset -- this deletes every owner-scoped row and restores the GLOBAL row to its
    cleared default, then commits so the reset is visible to every session/connection a
    test may open afterward (matching the cross-process durability this table exists for)."""
    _ensure_epoch_row(db, scope_key=_GLOBAL_SCOPE_KEY, owner_id=None)
    db.execute(text("DELETE FROM workforce_authority_epoch WHERE scope_key <> :g"), {"g": _GLOBAL_SCOPE_KEY})
    db.execute(
        text(
            """
            UPDATE workforce_authority_epoch
            SET epoch = 0, stopped = false, reason = NULL, activated_at = NULL,
                revoked_assignment_ids = '[]'::jsonb, updated_at = now()
            WHERE scope_key = :g
            """
        ),
        {"g": _GLOBAL_SCOPE_KEY},
    )
    db.commit()


def assert_not_killed(db: Session, owner_id: uuid.UUID) -> None:
    """Fail closed if EITHER this owner's kill switch OR the true global stop is active.

    DB-backed (workforce_authority_epoch), not a process-local flag: a stop committed by
    ANY process is visible here immediately via a plain read-committed SELECT. This is the
    defense-in-depth check at assignment EXECUTION time (provider_worker.py); the actual
    fix for the grant/stop race is assert_grant_allowed(), checked at assignment GRANT
    time with row locking, not here.

    Requires owner_id: a bare global check here is exactly the cross-owner
    denial-of-service this module exists to prevent (see module docstring).
    """
    global_row = db.execute(
        text("SELECT stopped, reason FROM workforce_authority_epoch WHERE scope_key = :k"),
        {"k": _GLOBAL_SCOPE_KEY},
    ).mappings().first()
    if global_row and global_row["stopped"]:
        raise KillSwitchError(
            f"workforce kill switch active (global): {global_row['reason']}",
            code="BLOCKED_BY_GLOBAL_EMERGENCY_STOP",
        )
    owner_row = db.execute(
        text("SELECT stopped, reason FROM workforce_authority_epoch WHERE scope_key = :k"),
        {"k": _owner_scope_key(owner_id)},
    ).mappings().first()
    if owner_row and owner_row["stopped"]:
        raise KillSwitchError(
            f"workforce kill switch active: {owner_row['reason']}",
            code="BLOCKED_BY_OWNER_STOP",
        )


def assert_grant_allowed(db: Session, *, owner_id: uuid.UUID) -> dict:
    """THE fix for the authority-widening kill-switch race. Must be called by the grant
    path (broker.resolve_delegation), in the SAME transaction that will insert the new
    WorkforceAssignment, BEFORE that insert -- and that transaction must not commit until
    after this call returns without raising.

    Takes a SELECT .. FOR SHARE lock on the GLOBAL row, then this owner's row (always in
    that order -- see module docstring / migration 0069 for why this ordering, matched by
    activate_global_kill_switch locking GLOBAL only and activate_kill_switch locking the
    owner row only, can never deadlock), and refuses the grant if either scope is stopped.
    A concurrent activate_kill_switch/activate_global_kill_switch call takes a conflicting
    SELECT .. FOR UPDATE on the SAME row as part of ITS OWN commit, so the database's lock
    manager -- not application timing -- enforces one strict ordering between this grant
    and any concurrent stop for the scopes involved.

    Returns fence metadata for assignment provenance (compatible with #237 callers).
    """
    global_row = _lock_epoch_row_for_share(db, scope_key=_GLOBAL_SCOPE_KEY, owner_id=None)
    if global_row["stopped"]:
        raise KillSwitchError(
            f"workforce kill switch active (global): {global_row['reason']}",
            code="BLOCKED_BY_GLOBAL_EMERGENCY_STOP",
        )
    owner_row = _lock_epoch_row_for_share(db, scope_key=_owner_scope_key(owner_id), owner_id=owner_id)
    if owner_row["stopped"]:
        raise KillSwitchError(
            f"workforce kill switch active: {owner_row['reason']}",
            code="BLOCKED_BY_OWNER_STOP",
        )
    return {
        "mechanism": "workforce_authority_epoch",
        "global_epoch": int(global_row.get("epoch") or 0),
        "owner_epoch": int(owner_row.get("epoch") or 0),
        "owner_id": str(owner_id),
    }


def assert_authority_grant_allowed(db: Session, *, owner_id: uuid.UUID) -> dict:
    """Alias for assert_grant_allowed — single canonical fence (#243)."""
    return assert_grant_allowed(db, owner_id=owner_id)


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


def _write_stop_state(db: Session, *, scope_key: str, reason: str, revoked: list[str]) -> datetime:
    now = datetime.utcnow()
    db.execute(
        text(
            """
            UPDATE workforce_authority_epoch
            SET epoch = epoch + 1, stopped = true, reason = :reason,
                activated_at = :now, revoked_assignment_ids = CAST(:revoked AS jsonb), updated_at = :now
            WHERE scope_key = :scope_key
            """
        ),
        {"reason": reason, "now": now, "revoked": json.dumps(revoked), "scope_key": scope_key},
    )
    db.flush()
    return now


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

    Takes SELECT .. FOR UPDATE on this owner's authority-epoch row BEFORE scanning for
    live assignments to revoke -- this is the other half of the grant/stop race fix (see
    assert_grant_allowed()): it blocks until every grant already holding FOR SHARE on this
    row has committed (so the live-assignment scan below is guaranteed to see it) or has
    not yet started (so it will block on FOR SHARE until this call commits, then observe
    stopped=True and refuse).
    """
    scope_key = _owner_scope_key(owner_id)
    _lock_epoch_row_for_update(db, scope_key=scope_key, owner_id=owner_id)
    revoked = _revoke_live_assignments(db, owner_id=owner_id, reason=reason)
    now = _write_stop_state(db, scope_key=scope_key, reason=reason, revoked=revoked)
    if not prove_no_reusable_live_authority(db, owner_id=owner_id):
        raise KillSwitchError(
            "stop postcondition failed: reusable live authority remains",
            code="STOP_POSTCONDITION",
        )
    epoch_row = db.execute(
        text("SELECT epoch FROM workforce_authority_epoch WHERE scope_key = :k"),
        {"k": scope_key},
    ).mappings().one()
    return KillSwitchState(
        active=True,
        reason=reason,
        activated_at=now.isoformat() + "Z",
        revoked_assignment_ids=revoked,
        scope="owner",
        owner_id=str(owner_id),
        sequence=int(epoch_row["epoch"]),
    )


def activate_owner_stop(
    db: Session, *, owner_id: uuid.UUID, reason: str
) -> KillSwitchState:
    """#237 name → canonical owner stop."""
    return activate_kill_switch(db, owner_id=owner_id, reason=reason)


def activate_global_kill_switch(db: Session, *, reason: str) -> KillSwitchState:
    """TRUE global emergency stop: revokes EVERY owner's live assignments and clears
    activation gates to UNKNOWN (UNKNOWN != VERIFIED -> fail closed for the provider
    path, for every owner, until re-verified). This is the explicit, distinct
    capability activate_kill_switch()'s own per-owner path must never accidentally be.

    Takes SELECT .. FOR UPDATE on the GLOBAL authority-epoch row first -- every grant, for
    every owner, always locks this same row (FOR SHARE) as its first step, so this single
    lock is enough to serialize this global stop against ALL concurrent grants regardless
    of owner, the same way activate_kill_switch()'s owner-row lock serializes against just
    that owner's grants.
    """
    _lock_epoch_row_for_update(db, scope_key=_GLOBAL_SCOPE_KEY, owner_id=None)
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

    now = _write_stop_state(db, scope_key=_GLOBAL_SCOPE_KEY, reason=reason, revoked=revoked)
    return KillSwitchState(
        active=True,
        reason=reason,
        activated_at=now.isoformat() + "Z",
        revoked_assignment_ids=revoked,
        scope="global",
    )


def activate_global_emergency_stop(
    db: Session, *, reason: str, founder_authority_ref: str | None = None
) -> KillSwitchState:
    """#237 name → canonical global stop. founder_authority_ref is recorded in reason only."""
    if founder_authority_ref:
        reason = f"{reason}|authority:{founder_authority_ref}"
    return activate_global_kill_switch(db, reason=reason)


def clear_kill_switch_for_recovery(
    db: Session, *, founder_ack: str, owner_id: uuid.UUID | None = None
) -> KillSwitchState:
    """Founder must explicitly clear — not automatic.

    owner_id=None clears the TRUE global stop (matching activate_global_kill_switch());
    pass the specific owner_id to clear only that owner's kill state. Does NOT reset the
    epoch counter -- it keeps monotonically increasing across stop/clear cycles, it is a
    generation number, not a "currently stopped" flag (that's the separate `stopped`
    column).
    """
    _validate_founder_ack(founder_ack)
    scope_key = _GLOBAL_SCOPE_KEY if owner_id is None else _owner_scope_key(owner_id)
    _lock_epoch_row_for_update(db, scope_key=scope_key, owner_id=owner_id)
    reason = f"cleared:{founder_ack}"
    now = datetime.utcnow()
    db.execute(
        text(
            """
            UPDATE workforce_authority_epoch
            SET stopped = false, reason = :reason, updated_at = :now
            WHERE scope_key = :scope_key
            """
        ),
        {"reason": reason, "now": now, "scope_key": scope_key},
    )
    db.flush()
    return KillSwitchState(active=False, reason=reason)


def clear_owner_stop(
    db: Session,
    *,
    owner_id: uuid.UUID,
    founder_ack: str,
    clear_request_id: uuid.UUID | None = None,
    expected_sequence: int | None = None,
) -> KillSwitchState:
    """#237 clear API → canonical owner clear.

    clear_request_id reserved for future audit events. expected_sequence (epoch) must
    match current epoch when provided — stale clears after a newer stop are rejected.
    """
    _ = clear_request_id
    _validate_founder_ack(founder_ack)
    scope_key = _owner_scope_key(owner_id)
    row = _lock_epoch_row_for_update(db, scope_key=scope_key, owner_id=owner_id)
    if expected_sequence is not None and int(row.get("epoch") or 0) != int(expected_sequence):
        raise KillSwitchError(
            f"stale clear: expected epoch {expected_sequence}, current {row.get('epoch')}",
            code="STALE_SEQUENCE",
            details={"expected": expected_sequence, "current": int(row.get("epoch") or 0)},
        )
    return clear_kill_switch_for_recovery(db, founder_ack=founder_ack, owner_id=owner_id)


def clear_global_emergency_stop(
    db: Session,
    *,
    founder_ack: str,
    clear_request_id: uuid.UUID | None = None,
    expected_sequence: int | None = None,
) -> KillSwitchState:
    _ = clear_request_id
    _validate_founder_ack(founder_ack)
    row = _lock_epoch_row_for_update(db, scope_key=_GLOBAL_SCOPE_KEY, owner_id=None)
    if expected_sequence is not None and int(row.get("epoch") or 0) != int(expected_sequence):
        raise KillSwitchError(
            f"stale clear: expected epoch {expected_sequence}, current {row.get('epoch')}",
            code="STALE_SEQUENCE",
            details={"expected": expected_sequence, "current": int(row.get("epoch") or 0)},
        )
    return clear_kill_switch_for_recovery(db, founder_ack=founder_ack, owner_id=None)


def query_stop_status(db: Session, *, owner_id: uuid.UUID | None = None) -> dict:
    """Canonical observability over workforce_authority_epoch (not a second stop SoT)."""
    global_state = get_global_kill_switch(db)
    owner_state = get_kill_switch(db, owner_id) if owner_id is not None else None
    global_row = db.execute(
        text("SELECT epoch FROM workforce_authority_epoch WHERE scope_key = :k"),
        {"k": _GLOBAL_SCOPE_KEY},
    ).mappings().first()
    owner_epoch = 0
    if owner_id is not None:
        orow = db.execute(
            text("SELECT epoch FROM workforce_authority_epoch WHERE scope_key = :k"),
            {"k": _owner_scope_key(owner_id)},
        ).mappings().first()
        owner_epoch = int((orow or {}).get("epoch") or 0)
    blocked = bool(global_state.active) or bool(owner_state and owner_state.active)
    code = None
    if global_state.active:
        code = "BLOCKED_BY_GLOBAL_EMERGENCY_STOP"
    elif owner_state and owner_state.active:
        code = "BLOCKED_BY_OWNER_STOP"
    return {
        "blocked": blocked,
        "code": code,
        "canonical_table": "workforce_authority_epoch",
        "global": {
            "active": bool(global_state.active),
            "reason": global_state.reason,
            "sequence": int((global_row or {}).get("epoch") or 0),
        },
        "owner": (
            {
                "active": bool(owner_state.active),
                "reason": owner_state.reason,
                "sequence": owner_epoch,
                "owner_id": str(owner_id),
            }
            if owner_state is not None
            else None
        ),
    }


def record_boot_blocked(db: Session, *, owner_id: uuid.UUID | None, reason: str) -> None:
    """Audit boot refusal against ONE blocking identity from query_stop_status.

    Events table (0070) may append later; identity must still be single-row coherent now.
    """
    status = query_stop_status(db, owner_id=owner_id)
    # Touch updated_at on the blocking epoch row so audit is durable without a second SoT.
    if status.get("global", {}).get("active"):
        scope_key = _GLOBAL_SCOPE_KEY
        oid = None
    elif status.get("owner", {}).get("active") and owner_id is not None:
        scope_key = _owner_scope_key(owner_id)
        oid = owner_id
    else:
        scope_key = _GLOBAL_SCOPE_KEY
        oid = None
    _ensure_epoch_row(db, scope_key=scope_key, owner_id=oid)
    db.execute(
        text(
            """
            UPDATE workforce_authority_epoch
            SET updated_at = :now,
                reason = CASE
                    WHEN stopped THEN reason
                    ELSE :reason
                END
            WHERE scope_key = :scope_key
            """
        ),
        {
            "now": datetime.utcnow(),
            "reason": f"boot_blocked:{reason}"[:500],
            "scope_key": scope_key,
        },
    )
    db.flush()


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
