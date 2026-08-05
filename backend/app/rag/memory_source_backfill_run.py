"""Durable production backfill-run reporting (migration 0025) wrapping
app/rag/memory_source_backfill.py::backfill_memory_source_units() — the last blocker that
module's own docstring named before it may ever be run against production. See migration
0025's module docstring for the full table-design rationale (cursor semantics, snapshot
counters, the "no fabricated completion claims" rule).

One unit of work per `advance_backfill_run()` call: exactly one bounded batch (up to
`run.batch_size` candidates, `max_batches=1` passed straight through to
`backfill_memory_source_units()`), never more — matching app/routers/admin.py's
`trigger_claim_type_backfill` convention of bounding work to one HTTP request at a time
(no unbounded loop held open inside a request, no work with no durable row behind it). A
caller that wants to drive a run to completion (an HTTP endpoint retried by an operator, or an
offline/maintenance script looping until `run.status != "running"`) simply calls this
repeatedly — each call is its own committed unit, so an interruption between calls loses
nothing already durably recorded.

Idempotent reruns: `create_or_resume_backfill_run()` returns the existing `pending`/`running`
run for `(owner_id)` if one already exists, race-safe via the SAVEPOINT + IntegrityError +
rollback-to-savepoint + fresh-SELECT pattern app/rag/memory_source.py's
`get_or_create_memory_source_unit()` already established (see that module's docstring for why
a single `INSERT ... ON CONFLICT` + `SELECT` is NOT safe against a concurrent committer under
READ COMMITTED), backed by migration 0025's own partial unique index
(`uq_msbr_one_active_per_owner`) as the actual source of truth. No silent retries: a run that
is `failed`/`completed`/`cancelled` is never silently resumed or reused — `advance_backfill_run`
raises on a non-advanceable run rather than quietly no-op'ing or restarting it.

Single-flight per run (founder review, round 2): `advance_backfill_run()`/`cancel_backfill_run()`
serialize on a session-level Postgres advisory lock keyed by `run.id` (see `_run_lock()` below)
for the ENTIRE call, not just a `SELECT ... FOR UPDATE`. A bare `FOR UPDATE` on the run row is
NOT sufficient by itself: `backfill_memory_source_units()` commits per-claim on the SAME
session (its own restart-safety design), and a `COMMIT` releases every row lock the transaction
held — so a `FOR UPDATE` taken before that first internal commit would already be gone for the
rest of the batch, leaving a concurrent caller free to interleave. Both functions still take a
`SELECT ... FOR UPDATE` re-read immediately after acquiring the advisory lock (defense in depth,
and the mechanism the founder explicitly asked for) — but the advisory lock, held on its own
dedicated connection exactly like `app/cleanup.py`'s `_CLEANUP_LOCK_KEY`, is what actually keeps
a second `advance()`/`cancel()` for the same run from starting at all until the first finishes.
Non-blocking: a caller that can't acquire it raises `BackfillRunBusy` immediately (mapped to 409
by the router) rather than hanging an HTTP request on another in-flight call.
"""

import contextlib
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import engine
from app.models.memory_source_backfill_run import BackfillRunMode, BackfillRunStatus, MemorySourceBackfillFailure, MemorySourceBackfillRun
from app.models.knowledge_claim import KnowledgeClaim
from app.rag.memory_source_backfill import MemorySourceBackfillResult, _apply_cursor, backfill_memory_source_units

logger = logging.getLogger("mainai.rag.memory_source_backfill_run")

# One bounded batch per advance() call — see module docstring. Matches
# app/rag/memory_source_backfill.py's own BACKFILL_BATCH_SIZE as the default so a caller that
# doesn't override batch_size gets identical per-call sizing to the underlying function.
DEFAULT_RUN_BATCH_SIZE = 50

# Distinct from app/rag/blob_references.py's hashtextextended seeds 0/1 (a completely
# different lock domain, keyed by storage key text, not run id) — kept separate purely so the
# two domains are trivially distinguishable by inspection, even though a collision between
# them was already astronomically unlikely (different `key` text entirely).
_RUN_LOCK_SEED = 2

# Never persist raw exception text — see _safe_error_summary()'s docstring.
_MAX_ERROR_SUMMARY_LENGTH = 200


class BackfillRunNotAdvanceable(RuntimeError):
    """Raised by advance_backfill_run()/cancel_backfill_run() when the run's status is
    already terminal (completed/failed/cancelled) — never silently no-ops or resumes a
    finished run (see module docstring's "no silent retries")."""


class BackfillRunBusy(RuntimeError):
    """Raised when another advance()/cancel() call for the SAME run is already in flight (the
    `_run_lock()` advisory lock is held elsewhere). Never blocks waiting for it — the caller
    (an HTTP request) retries shortly instead of hanging. See module docstring's "single-flight
    per run" section."""


@contextlib.contextmanager
def _run_lock(run_id: uuid.UUID):
    """Serializes advance_backfill_run()/cancel_backfill_run() for ONE run across the whole
    call, including the internal per-claim commits app/rag/memory_source_backfill.py's
    `_apply()` performs on the same session (see module docstring for why a plain `FOR UPDATE`
    can't do this alone). Session-level advisory lock (`pg_try_advisory_lock`/
    `pg_advisory_unlock`, NOT the transaction-scoped `_xact_lock` variant), acquired and
    released on its own dedicated connection — same pattern and same reasoning as
    app/cleanup.py's `_CLEANUP_LOCK_KEY`: a SQLAlchemy Session releases its pooled connection
    back on every commit(), so acquiring/releasing via `db.execute()` on the caller's session
    could silently hand the unlock call to a DIFFERENT physical connection, leaking the lock.
    """
    lock_conn = engine.connect()
    try:
        acquired = lock_conn.execute(
            sa_text("SELECT pg_try_advisory_lock(pg_catalog.hashtextextended(:key, :seed))"),
            {"key": str(run_id), "seed": _RUN_LOCK_SEED},
        ).scalar()
        lock_conn.commit()
        if not acquired:
            raise BackfillRunBusy(f"run {run_id} already has an advance()/cancel() call in progress")
        try:
            yield
        finally:
            lock_conn.execute(
                sa_text("SELECT pg_advisory_unlock(pg_catalog.hashtextextended(:key, :seed))"),
                {"key": str(run_id), "seed": _RUN_LOCK_SEED},
            )
            lock_conn.commit()
    finally:
        lock_conn.close()


def _safe_error_summary(exc: Exception) -> str:
    """Type name only — never `str(exc)`. A raw exception message can carry SQL text, bound
    parameters, or other backend internals (e.g. an IntegrityError SQLAlchemy didn't hide
    parameters for); matches the discipline app/rag/memory_source_backfill.py's own per-claim
    "unexpected error" path already uses for exactly this reason. Length-bounded defensively —
    the type name itself is always short, but this keeps the field bounded even if a future
    caller changes what's passed in here."""
    return f"unexpected error: {type(exc).__name__}"[:_MAX_ERROR_SUMMARY_LENGTH]


def _real_candidates_remain(db: Session, owner_id: uuid.UUID, after: tuple[datetime, uuid.UUID] | None) -> bool:
    """Authoritative, lock-independent completion gate for REAL-mode runs (founder review,
    round 2 — "completion får aldrig markeras medan eligible claims finns"). A batch that finds
    zero candidates via `SELECT ... FOR UPDATE SKIP LOCKED` cannot tell "truly nothing left"
    apart from "everything left is momentarily locked by an unrelated concurrent transaction" —
    those look identical to `_apply()`. This plain, non-locking existence check is the
    tie-breaker: Postgres MVCC lets a plain read see a row regardless of another transaction's
    row lock, so it correctly reports True even while every remaining candidate is locked.

    MUST apply the same `after` cursor filter `_apply()` itself uses (`_apply_cursor`,
    imported from app/rag/memory_source_backfill.py — not reimplemented, so this can never
    silently disagree with what the batch actually considered reachable). A claim this run has
    already considered and permanently excluded via cursor advancement (e.g. a structural
    resolution failure — see memory_source_backfill.py's `_apply()`) still has
    `memory_source_id IS NULL` forever; a GLOBAL (cursor-blind) existence check would treat
    that claim as "still remaining" forever too, making it impossible for any run with even one
    permanently-unresolvable claim to ever reach `completed`. Passing `None` (no cursor yet, or
    the lock-gap freeze left it at the very start) correctly falls back to a global check.

    Dry-run mode never calls this — it takes no locks at all (see memory_source_backfill.py's
    `_dry_run`), so its own cursor-exhaustion check is already authoritative."""
    query = db.query(KnowledgeClaim.id).filter(
        KnowledgeClaim.owner_id == owner_id, KnowledgeClaim.memory_source_id.is_(None)
    )
    query = _apply_cursor(query, after)
    return query.first() is not None


def _active_run_snapshot(db: Session, owner_id: uuid.UUID) -> tuple[int, int]:
    """(total_candidates, already_done) taken once, right now — see migration 0025's module
    docstring: a progress denominator, not a guarantee."""
    total_candidates = (
        db.query(KnowledgeClaim)
        .filter(KnowledgeClaim.owner_id == owner_id, KnowledgeClaim.memory_source_id.is_(None))
        .count()
    )
    already_done = (
        db.query(KnowledgeClaim)
        .filter(KnowledgeClaim.owner_id == owner_id, KnowledgeClaim.memory_source_id.isnot(None))
        .count()
    )
    return total_candidates, already_done


def create_or_resume_backfill_run(
    db: Session, owner_id: uuid.UUID, *, mode: BackfillRunMode, batch_size: int = DEFAULT_RUN_BATCH_SIZE
) -> MemorySourceBackfillRun:
    """Returns the existing active (pending/running) run for `owner_id` if one exists —
    REGARDLESS of its `mode` (a real run in progress must not silently get a concurrent
    dry-run counterpart racing over the same claims' worth of DB load, and vice versa) —
    otherwise creates and returns a brand new one in the requested `mode`.

    Race-safe: two concurrent callers both attempting to create a first run for the same
    owner will have exactly one INSERT succeed (migration 0025's partial unique index
    `uq_msbr_one_active_per_owner`); the other catches IntegrityError and returns the
    winner's row instead of erroring."""
    if batch_size <= 0:
        raise ValueError(f"batch_size must be a positive integer, got {batch_size!r}")

    existing = (
        db.query(MemorySourceBackfillRun)
        .filter(
            MemorySourceBackfillRun.owner_id == owner_id,
            MemorySourceBackfillRun.status.in_([BackfillRunStatus.pending, BackfillRunStatus.running]),
        )
        .first()
    )
    if existing is not None:
        return existing

    total_candidates, already_done = _active_run_snapshot(db, owner_id)
    savepoint = db.begin_nested()
    try:
        run = MemorySourceBackfillRun(
            owner_id=owner_id,
            mode=mode,
            status=BackfillRunStatus.pending,
            batch_size=batch_size,
            total_candidates_snapshot=total_candidates,
            already_done_snapshot=already_done,
        )
        db.add(run)
        db.flush()
        savepoint.commit()
        db.commit()
        return run
    except IntegrityError as exc:
        savepoint.rollback()
        db.rollback()
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint != "uq_msbr_one_active_per_owner":
            raise
        winner = (
            db.query(MemorySourceBackfillRun)
            .filter(
                MemorySourceBackfillRun.owner_id == owner_id,
                MemorySourceBackfillRun.status.in_([BackfillRunStatus.pending, BackfillRunStatus.running]),
            )
            .first()
        )
        if winner is None:
            # The concurrent winner's transaction hasn't become visible to us yet under
            # READ COMMITTED (vanishingly unlikely — its own INSERT already committed before
            # our IntegrityError could even fire) — never fabricate a run row here; the
            # caller retries.
            raise RuntimeError(
                f"owner {owner_id}: a concurrent backfill run was created but is not yet visible; retry"
            ) from exc
        return winner


def record_failure(db: Session, run: MemorySourceBackfillRun, claim_id: uuid.UUID, reason: str) -> None:
    """Upserts one `memory_source_backfill_failures` row for `(run.id, claim_id)` — a claim
    re-considered within the SAME run increments `attempt_count` instead of duplicating a row
    (unique on `(run_id, claim_id)`, migration 0025). `reason` must never be
    `KnowledgeClaim.claim_text` — only the structural failure strings
    app/rag/memory_source_backfill.py's `_resolve_locator()`/`_apply()` already produce
    (document/chunk/version/owner ids)."""
    savepoint = db.begin_nested()
    try:
        db.add(
            MemorySourceBackfillFailure(
                run_id=run.id, owner_id=run.owner_id, claim_id=claim_id, reason=reason, attempt_count=1
            )
        )
        db.flush()
        savepoint.commit()
    except IntegrityError as exc:
        savepoint.rollback()
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint != "uq_msbf_run_claim":
            raise
        existing = (
            db.query(MemorySourceBackfillFailure)
            .filter(MemorySourceBackfillFailure.run_id == run.id, MemorySourceBackfillFailure.claim_id == claim_id)
            .with_for_update()
            .one()
        )
        existing.reason = reason
        existing.attempt_count += 1
        db.add(existing)
        db.flush()


def advance_backfill_run(db: Session, run: MemorySourceBackfillRun) -> MemorySourceBackfillRun:
    """Performs exactly ONE bounded batch of work for `run` and durably updates it — see
    module docstring. Transitions `pending` -> `running` on the first call. Transitions to
    `completed` ONLY when the underlying call reports zero candidates considered AND (for
    real-mode runs) `_real_candidates_remain()` independently confirms none are left — never a
    batch-cap artifact, and never inferred from a SKIP LOCKED batch that may just have found
    everything momentarily locked. Raises `BackfillRunNotAdvanceable` for a run that is already
    `completed`/`failed`/`cancelled`, or `BackfillRunBusy` if another advance()/cancel() call
    for this SAME run is already in flight (see module docstring's "single-flight per run")."""
    with _run_lock(run.id):
        # Re-read with a row lock now that we hold the advisory lock — the status check the
        # founder asked for happens AFTER lock acquisition, against the current committed row,
        # not whatever the caller's (possibly stale) `run` object looked like before we got
        # here.
        run = db.query(MemorySourceBackfillRun).filter(MemorySourceBackfillRun.id == run.id).with_for_update().one()

        if run.status not in (BackfillRunStatus.pending, BackfillRunStatus.running):
            raise BackfillRunNotAdvanceable(f"run {run.id} is {run.status.value}, not advanceable")

        now = datetime.now(timezone.utc)
        if run.status == BackfillRunStatus.pending:
            run.status = BackfillRunStatus.running
            run.started_at = now
            # Committed as its OWN transaction, before backfill_memory_source_units() is ever
            # called — that function's `_apply()` does its own per-claim db.rollback() on a
            # resolution failure, which (since it's the same session/transaction) would silently
            # discard this pending->running transition too if it were left uncommitted. Safe to
            # commit here (releasing the row-level FOR UPDATE early) because the advisory lock,
            # not the row lock, is what's actually serializing this call end-to-end.
            db.add(run)
            db.commit()

        after = None
        if run.last_cursor_created_at is not None and run.last_cursor_claim_id is not None:
            after = (run.last_cursor_created_at, run.last_cursor_claim_id)

        try:
            result: MemorySourceBackfillResult = backfill_memory_source_units(
                db,
                run.owner_id,
                batch_size=run.batch_size,
                max_batches=1,
                dry_run=(run.mode == BackfillRunMode.dry_run),
                after=after,
            )
        except Exception as exc:
            db.rollback()
            run.status = BackfillRunStatus.failed
            run.error_summary = _safe_error_summary(exc)
            run.completed_at = datetime.now(timezone.utc)
            db.add(run)
            db.commit()
            raise

        for claim_id, reason in result.failures:
            record_failure(db, run, claim_id, reason)

        run.processed_count += result.candidates_total
        run.exact_chunk_count += result.chunk_backed
        run.degraded_version_count += result.version_backed
        run.missing_document_only_count += result.document_backed
        run.skipped_unresolvable_count += result.skipped_mismatch
        run.failed_count += result.failed
        run.batches_completed += 1
        if result.last_seen_created_at is not None and result.last_seen_id is not None:
            run.last_cursor_created_at = result.last_seen_created_at
            run.last_cursor_claim_id = result.last_seen_id

        if result.candidates_total == 0:
            after_for_check = None
            if run.last_cursor_created_at is not None and run.last_cursor_claim_id is not None:
                after_for_check = (run.last_cursor_created_at, run.last_cursor_claim_id)
            if run.mode == BackfillRunMode.real and _real_candidates_remain(db, run.owner_id, after_for_check):
                logger.warning(
                    "memory_source_backfill_run: run %s found zero candidates this batch but an "
                    "eligible claim still exists for owner %s (likely lock contention) — staying "
                    "running, not marking completed",
                    run.id,
                    run.owner_id,
                )
            else:
                run.status = BackfillRunStatus.completed
                run.completed_at = datetime.now(timezone.utc)

        db.add(run)
        db.commit()
        return run


def cancel_backfill_run(db: Session, run: MemorySourceBackfillRun) -> MemorySourceBackfillRun:
    """Operator-triggered stop. Remaining candidates simply stay `memory_source_id IS NULL` —
    still valid candidates for a future run, same as ImportJob's `cancelled` semantics. Shares
    `advance_backfill_run()`'s advisory lock (keyed by `run.id`) so a `cancel()` can never land
    while an `advance()` batch for the same run is in flight and get silently clobbered back to
    `running`/`completed` by that batch's own final commit — see module docstring."""
    with _run_lock(run.id):
        run = db.query(MemorySourceBackfillRun).filter(MemorySourceBackfillRun.id == run.id).with_for_update().one()
        if run.status not in (BackfillRunStatus.pending, BackfillRunStatus.running):
            raise BackfillRunNotAdvanceable(f"run {run.id} is {run.status.value}, not cancellable")
        run.status = BackfillRunStatus.cancelled
        run.completed_at = datetime.now(timezone.utc)
        db.add(run)
        db.commit()
        return run


def get_backfill_run(db: Session, owner_id: uuid.UUID, run_id: uuid.UUID) -> MemorySourceBackfillRun | None:
    return (
        db.query(MemorySourceBackfillRun)
        .filter(MemorySourceBackfillRun.id == run_id, MemorySourceBackfillRun.owner_id == owner_id)
        .first()
    )


def list_backfill_runs(db: Session, owner_id: uuid.UUID, *, limit: int = 20) -> list[MemorySourceBackfillRun]:
    return (
        db.query(MemorySourceBackfillRun)
        .filter(MemorySourceBackfillRun.owner_id == owner_id)
        .order_by(MemorySourceBackfillRun.created_at.desc())
        .limit(limit)
        .all()
    )
