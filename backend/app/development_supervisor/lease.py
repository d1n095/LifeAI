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


def claim_supervisor_goal_lease(
    db: Session, *, owner_id: uuid.UUID, goal_id: uuid.UUID, envelope_id: uuid.UUID, worker_id: str, lease_seconds: int
) -> tuple[uuid.UUID, int] | None:
    """Returns (lease_id, lease_generation) on a successful claim or stale-lease takeover, or
    `None` if another worker genuinely still holds this goal's lease. Does NOT commit -- the
    caller's own tick decides the transaction boundary, exactly like `renew_mainai_job_lease`."""
    row = db.execute(
        _CLAIM_SQL,
        {
            "owner_id": str(owner_id),
            "goal_id": str(goal_id),
            "envelope_id": str(envelope_id),
            "worker_id": worker_id,
            "lease_seconds": lease_seconds,
        },
    ).first()
    if row is None:
        return None
    return uuid.UUID(str(row[0])), int(row[1])


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


def release_supervisor_goal_lease(db: Session, *, lease_id: uuid.UUID, worker_id: str, lease_generation: int) -> None:
    """Ends this worker's hold on the lease at the end of a tick (success OR failure) so the
    next eligible tick -- this worker or another -- can claim it immediately rather than
    waiting out the full TTL. A caller whose lease was already lost (SupervisorLeaseLostError
    already raised by a prior renew) must NOT call this -- there is nothing of theirs left to
    release, and doing so would silently no-op against the fenced WHERE clause anyway, never
    against a different worker's now-current claim."""
    db.execute(
        text("""
            UPDATE supervisor_goal_leases
            SET status = 'released', released_at = now()
            WHERE id = :lease_id AND worker_id = :worker_id AND lease_generation = :lease_generation AND status = 'active'
        """),
        {"lease_id": str(lease_id), "worker_id": worker_id, "lease_generation": lease_generation},
    )
