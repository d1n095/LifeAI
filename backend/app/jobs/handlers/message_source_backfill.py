"""A MainAI job type on the Durable Job Foundation (migration 0026, app/models/mainai_job.py):
`message_source_backfill` — S1C's historical find-or-create pass (docs/
LIFE_SOURCE_FOUNDATION_BOOTSTRAP.md), run as a real, persisted, resumable, cancellable job.
Modeled directly on message_sequence_backfill.py (the S1B job this reuses the exact runtime
pattern from) — see that module's docstring for why this must be a job, not an endpoint or a
migration, and why reusing `mainai_jobs`' lease/fencing/heartbeat/cancellation/retry/audit
machinery is the point rather than building a second queue.

No AI, no provider, no network — app/rag/backfill/message_source.py is pure SQL + the
deterministic find-or-create primitive, so `_CAPABILITY_PROVIDER_ROLE` for this job type maps
to `None`, the same as `message_sequence_backfill`.
"""

import logging
import uuid

from sqlalchemy.orm import Session

from app.jobs.mainai_job_lease import JobLeaseLostError, renew_mainai_job_lease
from app.jobs.service import mark_cancelled, mark_completed, mark_failed, update_progress
from app.models.mainai_job import MainAIJob, MainAIJobErrorCategory, MainAIJobStatus
from app.rag.backfill.message_source import backfill_message_source_units, count_messages_without_source_unit

logger = logging.getLogger("mainai.jobs.message_source_backfill")

MESSAGE_SOURCE_BACKFILL_JOB_TYPE = "message_source_backfill"

# Same reasoning as message_sequence_backfill.py's own cap: bounds how long a single claimed
# job can hold its lease. Expressed in messages (not batches) here since that's the unit the
# founder's own completeness language (docs/LIFE_SOURCE_FOUNDATION_BOOTSTRAP.md §P) counts in.
MAX_MESSAGES_PER_RUN = 20_000
_BATCH_SIZE = 200


def _refresh(db: Session, job_id: uuid.UUID) -> MainAIJob | None:
    db.expire_all()
    return db.get(MainAIJob, job_id)


async def run_message_source_backfill_job(
    db: Session, job_id: uuid.UUID, owner_id: uuid.UUID, *, worker_id: str, lease_generation: int, lease_seconds: int
) -> None:
    """Entry point called by app/worker.py's poll loop once a `message_source_backfill` job has
    been claimed. `db` must already be scoped to `owner_id`'s RLS context (see
    app/worker.py's `_set_mainai_job_rls_owner`)."""
    job = db.get(MainAIJob, job_id)
    if job is None:
        return

    total = count_messages_without_source_unit(db, owner_id)
    processed = 0
    created = 0
    hit_run_cap = False

    def _lease_lost(where: str) -> None:
        logger.warning(
            "message_source_backfill job %s: lease lost (%s) — worker %s, generation %s. Stopping.",
            job_id, where, worker_id, lease_generation,
        )

    try:
        update_progress(db, job, worker_id=worker_id, lease_generation=lease_generation, current=0, total=total, phase="backfilling")
        db.commit()
    except JobLeaseLostError:
        _lease_lost("initial progress")
        return

    while processed < total:
        if processed >= MAX_MESSAGES_PER_RUN:
            hit_run_cap = True
            break

        job = _refresh(db, job_id)
        if job is None:
            return
        if job.cancel_requested:
            try:
                mark_cancelled(db, job, worker_id=worker_id, lease_generation=lease_generation)
            except JobLeaseLostError:
                _lease_lost("cancel")
            return

        # PR #61 correction pass (post-review, founder-mandated): renew_mainai_job_lease() must
        # NOT be committed here on its own -- its docstring is explicit that it "does NOT
        # commit — the caller's own batch transaction boundary decides when this becomes
        # durable" (same discipline as update_progress()/corpus_review.py). Committing right
        # after renew (the previous bug) closed the fencing window too early: the UPDATE's row
        # lock was released at that commit, so a worker whose subsequent batch took long enough
        # for the freshly-renewed lease to still expire could have its domain writes below land
        # anyway, since nothing re-checked the lease at THAT commit. Leaving the renewal
        # uncommitted keeps its row lock held for the batch's entire duration below -- no other
        # worker's claim_next_mainai_job() can reclaim this row (SELECT ... FOR UPDATE SKIP
        # LOCKED skips locked rows) until backfill_message_source_units' own commit lands,
        # which now durably includes the lease renewal and the batch's domain writes as one
        # atomic transaction. A lease that was already lost before this call still raises
        # immediately with nothing written, exactly as before.
        try:
            renew_mainai_job_lease(db, job_id, worker_id, lease_generation, lease_seconds)
        except JobLeaseLostError:
            db.rollback()
            _lease_lost("heartbeat")
            return

        batch_cap = min(_BATCH_SIZE, MAX_MESSAGES_PER_RUN - processed)
        try:
            result = backfill_message_source_units(db, owner_id, max_batches=1, batch_size=batch_cap)
        except JobLeaseLostError:
            db.rollback()
            _lease_lost("backfill batch")
            return
        except Exception:  # noqa: BLE001 - a genuinely unexpected failure still fails the whole job
            db.rollback()
            logger.exception("message_source_backfill job %s: unexpected error", job_id)
            job = _refresh(db, job_id)
            if job is not None:
                try:
                    mark_failed(db, job, worker_id=worker_id, lease_generation=lease_generation, error_category=MainAIJobErrorCategory.unexpected)
                except JobLeaseLostError:
                    _lease_lost("mark_failed:unexpected")
            return

        if result.processed == 0:
            break  # no candidates left — caught up

        processed += result.processed
        created += result.created
        try:
            update_progress(db, job, worker_id=worker_id, lease_generation=lease_generation, current=min(processed, total), total=max(total, processed), phase="backfilling")
            db.commit()
        except JobLeaseLostError:
            _lease_lost("progress")
            return

    job = _refresh(db, job_id)
    if job is None:
        return
    if job.status != MainAIJobStatus.running:
        return

    remaining = count_messages_without_source_unit(db, owner_id)
    parts = [f"Created {created} message_source_units row(s) across {processed} message(s) (snapshot total {total})."]
    if hit_run_cap:
        parts.append(f"Stopped at this run's {MAX_MESSAGES_PER_RUN}-message cap.")
    if remaining:
        parts.append(f"{remaining} message(s) still without a source unit — run again.")
    else:
        parts.append("No messages remain without a source unit for this owner.")

    try:
        mark_completed(db, job, worker_id=worker_id, lease_generation=lease_generation, public_message=" ".join(parts)[:500])
    except JobLeaseLostError:
        _lease_lost("mark_completed")
