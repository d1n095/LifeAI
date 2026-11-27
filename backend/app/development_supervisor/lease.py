"""Claim/renew/release for `supervisor_goal_leases` (migration 0059) -- see that migration's
own module docstring for why this is a distinct, narrow lease rather than reusing
`app/jobs/mainai_job_lease.py`'s one-shot `mainai_jobs` claim machinery.

`claim_supervisor_goal_lease()` is a single atomic statement, exactly like every other
lease-claim in this codebase (`claim_next_mainai_job`, `claim_specific_mainai_job`): either
there is no current active lease for this goal (plain insert succeeds), or there is one and it
has genuinely expired (the existing row is taken over in place -- `lease_generation` bumped by
exactly 1, matching `AgentScopeLease`'s own "a takeover bumps THIS row's generation in place,
does not create a new row" discipline), or there is one and it is still genuinely held (the
conflict's `DO UPDATE ... WHERE` guard makes the statement a no-op, returning nothing). A
caller getting `None` back MUST treat this as "someone else already owns this goal's
Supervisor loop right now" and skip it this tick -- never retry-with-force, never invent a
second lease."""

import json
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

_CLAIM_SQL = text("""
    INSERT INTO supervisor_goal_leases
        (id, owner_id, goal_id, envelope_id, worker_id, lease_generation, status, acquired_at, expires_at, last_heartbeat_at)
    VALUES
        (gen_random_uuid(), :owner_id, :goal_id, :envelope_id, :worker_id, 1, 'active', now(),
         now() + make_interval(secs => :lease_seconds), now())
    ON CONFLICT (goal_id) WHERE status = 'active'
    DO UPDATE SET
        worker_id = EXCLUDED.worker_id,
        envelope_id = EXCLUDED.envelope_id,
        lease_generation = supervisor_goal_leases.lease_generation + 1,
        expires_at = EXCLUDED.expires_at,
        last_heartbeat_at = now(),
        acquired_at = now()
    WHERE supervisor_goal_leases.expires_at < now()
    RETURNING id, lease_generation
""")


class SupervisorLeaseLostError(Exception):
    """Raised by `renew_supervisor_goal_lease()`/`release_supervisor_goal_lease()` when the
    caller's (lease_id, worker_id, lease_generation) no longer matches the row's current
    claim -- the lease was reclaimed by another worker (this worker's own lease already
    expired) or is no longer active. The caller MUST stop the Supervisor run in progress and
    treat any further DB writes it was about to make as unsafe -- a different claimant may
    already be running `run_supervisor()` for the same goal concurrently."""

    def __init__(self, lease_id: uuid.UUID):
        self.lease_id = lease_id
        super().__init__(
            f"SupervisorGoalLease {lease_id}: lease no longer held at the expected generation "
            "-- it was reclaimed or already released."
        )


# F4: recycle the goal's OWN released row instead of inserting a new one every tick.
# `released_at DESC` picks the most recently released row (there is normally exactly one after
# this migration's backfill); `FOR UPDATE SKIP LOCKED` makes a concurrent claimer skip it and
# fall through to the INSERT below, whose partial-unique conflict then correctly reports "still
# held" instead of creating a second active row. `released_at` is cleared so the row's own
# timestamps always describe its CURRENT hold; the previous hold survives in
# `supervisor_goal_lease_events`.
_RECYCLE_RELEASED_SQL = text("""
    UPDATE supervisor_goal_leases
    SET status = 'active',
        worker_id = :worker_id,
        envelope_id = :envelope_id,
        lease_generation = supervisor_goal_leases.lease_generation + 1,
        acquired_at = now(),
        expires_at = now() + make_interval(secs => :lease_seconds),
        last_heartbeat_at = now(),
        released_at = NULL
    WHERE id = (
        SELECT id FROM supervisor_goal_leases
        WHERE goal_id = :goal_id AND owner_id = :owner_id AND status = 'released'
        ORDER BY released_at DESC NULLS LAST, acquired_at DESC
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id, lease_generation
""")


_EVENT_SQL = text("""
    INSERT INTO supervisor_goal_lease_events
        (owner_id, goal_id, lease_id, worker_id, lease_generation, event_type, acquired_at, released_at, detail)
    VALUES
        (:owner_id, :goal_id, :lease_id, :worker_id, :lease_generation, :event_type,
         CASE WHEN :event_type = 'released' THEN NULL ELSE now() END,
         CASE WHEN :event_type = 'released' THEN now() ELSE NULL END,
         CAST(:detail AS jsonb))
""")


def _record_lease_event(
    db: Session,
    *,
    owner_id: uuid.UUID,
    goal_id: uuid.UUID,
    lease_id: uuid.UUID,
    worker_id: str,
    lease_generation: int,
    event_type: str,
    detail: dict | None = None,
) -> None:
    """Append one durable history row (migration 0090). Flush-only, like every other write in
    this module: the caller's own transaction decides whether the claim/release and its history
    become durable together (they always do, or neither does).

    Deliberately NOT called for renewals: a heartbeat changes no ownership, and recording it
    would reintroduce exactly the per-tick growth this lifecycle change removes.
    """
    db.execute(
        _EVENT_SQL,
        {
            "owner_id": str(owner_id),
            "goal_id": str(goal_id),
            "lease_id": str(lease_id),
            "worker_id": worker_id,
            "lease_generation": lease_generation,
            "event_type": event_type,
            "detail": json.dumps(detail or {}),
        },
    )


def claim_supervisor_goal_lease(
    db: Session, *, owner_id: uuid.UUID, goal_id: uuid.UUID, envelope_id: uuid.UUID, worker_id: str, lease_seconds: int
) -> tuple[uuid.UUID, int] | None:
    """Returns (lease_id, lease_generation) on a successful claim or stale-lease takeover, or
    `None` if another worker genuinely still holds this goal's lease. Does NOT commit -- the
    caller's own tick decides the transaction boundary, exactly like `renew_mainai_job_lease`."""
    params = {
        "owner_id": str(owner_id),
        "goal_id": str(goal_id),
        "envelope_id": str(envelope_id),
        "worker_id": worker_id,
        "lease_seconds": lease_seconds,
    }
    recycled = db.execute(_RECYCLE_RELEASED_SQL, params).first()
    if recycled is not None:
        lease_id, generation = uuid.UUID(str(recycled[0])), int(recycled[1])
        _record_lease_event(
            db,
            owner_id=owner_id,
            goal_id=goal_id,
            lease_id=lease_id,
            worker_id=worker_id,
            lease_generation=generation,
            event_type="claimed",
            detail={"recycled_released_row": True},
        )
        return lease_id, generation

    row = db.execute(_CLAIM_SQL, params).first()
    if row is None:
        return None
    lease_id, generation = uuid.UUID(str(row[0])), int(row[1])
    # Generation 1 means the plain INSERT won (no active lease existed); anything higher means
    # the ON CONFLICT branch took over an EXPIRED active row in place -- the one distinction
    # the old table expressed only by its absence.
    _record_lease_event(
        db,
        owner_id=owner_id,
        goal_id=goal_id,
        lease_id=lease_id,
        worker_id=worker_id,
        lease_generation=generation,
        event_type="taken_over" if generation > 1 else "claimed",
    )
    return lease_id, generation


def renew_supervisor_goal_lease(db: Session, *, lease_id: uuid.UUID, worker_id: str, lease_generation: int, lease_seconds: int) -> None:
    """Heartbeat, called between bounded `run_supervisor()` calls for a long-running goal so a
    live worker's lease is never mistaken for stale mid-run. Requires (and atomically
    re-verifies) the caller's own worker_id/lease_generation -- see SupervisorLeaseLostError."""
    result = db.execute(
        text("""
            UPDATE supervisor_goal_leases
            SET expires_at = now() + make_interval(secs => :lease_seconds), last_heartbeat_at = now()
            WHERE id = :lease_id AND worker_id = :worker_id AND lease_generation = :lease_generation AND status = 'active'
        """),
        {"lease_id": str(lease_id), "worker_id": worker_id, "lease_generation": lease_generation, "lease_seconds": lease_seconds},
    )
    if result.rowcount == 0:
        raise SupervisorLeaseLostError(lease_id)


def release_supervisor_goal_lease(db: Session, *, lease_id: uuid.UUID, worker_id: str, lease_generation: int) -> bool:
    """Ends this worker's hold on the lease at the end of a tick (success OR failure) so the
    next eligible tick -- this worker or another -- can claim it immediately rather than
    waiting out the full TTL. A caller whose lease was already lost (SupervisorLeaseLostError
    already raised by a prior renew) must NOT call this -- there is nothing of theirs left to
    release, and doing so would silently no-op against the fenced WHERE clause anyway, never
    against a different worker's now-current claim.

    Returns True if this call actually released a row it held, False if the fenced WHERE
    clause matched nothing (already released, or -- the case worth a caller actually
    noticing -- this worker's own lease was reclaimed by someone else before this tick even
    got to release it, e.g. an unexpectedly slow operation outliving the TTL despite the
    per-task renewal in `prepare_context()`). The caller decides what a False means at its own
    boundary; this function only reports it, matching every other fenced UPDATE...RETURNING-
    shaped write in this codebase (never a silent, unobservable no-op)."""
    result = db.execute(
        text("""
            UPDATE supervisor_goal_leases
            SET status = 'released', released_at = now()
            WHERE id = :lease_id AND worker_id = :worker_id AND lease_generation = :lease_generation AND status = 'active'
        """),
        {"lease_id": str(lease_id), "worker_id": worker_id, "lease_generation": lease_generation},
    )
    if result.rowcount == 0:
        # Nothing of this worker's was released (already released, or reclaimed) -- writing a
        # `released` history row here would claim an event that never happened.
        return False
    row = db.execute(
        text("SELECT owner_id, goal_id FROM supervisor_goal_leases WHERE id = :lease_id"),
        {"lease_id": str(lease_id)},
    ).first()
    if row is not None:
        _record_lease_event(
            db,
            owner_id=uuid.UUID(str(row[0])),
            goal_id=uuid.UUID(str(row[1])),
            lease_id=lease_id,
            worker_id=worker_id,
            lease_generation=lease_generation,
            event_type="released",
        )
    return True
