"""The second MainAI job type on the Durable Job Foundation (migration 0026,
app/models/mainai_job.py): `message_sequence_backfill` — S1B's historical numbering pass, run
as a real, persisted, resumable, cancellable job rather than an unbounded HTTP call.

WHY A JOB AND NOT AN ENDPOINT OR A MIGRATION. §6.12 is explicit that memory-processing
backfills run as durable jobs, and CLAUDE.md's standing rule is that no "background
processing" may be claimed without a durable row behind it. Reusing the `mainai_jobs` runtime
built in PR #36 — its lease, fencing token, heartbeat, cancellation, retry budget, audit trail
and admin UI — is the whole point: §6.12's `memory_processing_jobs` sketch predates that
runtime, and building a second, parallel queue for one backfill would be exactly the "ad hoc
parallel mechanism" the project has repeatedly refused.

DIFFERENT IN ONE IMPORTANT WAY FROM `corpus_review`. This capability calls no provider and
needs no AI at all (see app/rag/message_sequence_backfill.py — pure SQL over existing rows),
which is why `_CAPABILITY_PROVIDER_ROLE` maps it to `None` rather than to `"chat"`. Numbering
the founder's own message history must not become unavailable because a model key is missing;
that is the "keep working without AI wherever architecturally possible" rule applied to a
capability where it costs nothing to honour.

IT MODIFIES EXISTING ROWS. Unlike `corpus_review` (read-mostly: reads documents, writes only
NEW proposal rows) this job UPDATEs existing `messages` rows, so its
`_CAPABILITY_WRITE_PROFILE` entry says `modifies_existing_data: True`. The modification is
strictly additive in meaning — NULL -> an ordinal, never a change to an already-assigned one,
enforced by migration 0030's `messages_deny_sequence_number_rewrite` trigger at the database
level, not by this module's good behaviour. No message content, role, timestamp or id is
touched by any statement in this path.

RESTART / LEASE / CANCEL SEMANTICS. One conversation is one unit of work. `cancel_requested` is
checked and the lease renewed before each one, so a cancel or a stale-lease reclaim is never
delayed by more than a single conversation's numbering. Every write against the claimed job
goes through app/rag/mainai_jobs_service.py's fencing-guarded helpers with this run's exact
(worker_id, lease_generation), and the progress write happens INSIDE the same transaction as
the numbering it describes (see backfill_conversation's `on_outcome`), so a lease lost
mid-conversation rolls the numbering back with it instead of leaving a write behind from a
worker that no longer owns the job.
"""

import logging
import uuid

from sqlalchemy.orm import Session

from app.jobs.mainai_job_lease import JobLeaseLostError, renew_mainai_job_lease
from app.models.mainai_job import MainAIJob, MainAIJobErrorCategory, MainAIJobStatus
from app.rag.mainai_jobs_service import mark_cancelled, mark_completed, mark_failed, update_progress
from app.rag.message_sequence_backfill import (
    ConversationOutcome,
    candidate_conversation_ids,
    backfill_conversation,
    count_conversations_with_unsequenced_messages,
    count_unsequenced_messages,
)

logger = logging.getLogger("mainai.jobs.message_sequence_backfill")

MESSAGE_SEQUENCE_BACKFILL_JOB_TYPE = "message_sequence_backfill"

# A hard ceiling on conversations per run, independent of how many candidates exist. A single
# claimed job must not be able to hold its lease for an unbounded time: hitting this cap ends
# the run truthfully ("N of M, more remain") rather than pretending completion, and the founder
# simply creates another job. Same reasoning as app/routers/admin.py's
# MAX_BACKFILL_BATCHES_PER_REQUEST.
MAX_CONVERSATIONS_PER_RUN = 2000


def _refresh(db: Session, job_id: uuid.UUID) -> MainAIJob | None:
    db.expire_all()
    return db.get(MainAIJob, job_id)


async def run_message_sequence_backfill_job(
    db: Session, job_id: uuid.UUID, owner_id: uuid.UUID, *, worker_id: str, lease_generation: int, lease_seconds: int
) -> None:
    """Entry point called by app/worker.py's poll loop once a `message_sequence_backfill` job
    has been claimed. `db` must already be scoped to `owner_id`'s RLS context (see
    app/worker.py's `_set_mainai_job_rls_owner`) — every conversation this touches is selected
    through that scope, so the job can only ever number its own owner's history.

    `async` purely to match the dispatch signature app/worker.py already uses; there is no
    awaited I/O in this path (no provider call exists to make)."""
    job = db.get(MainAIJob, job_id)
    if job is None:
        return

    # Snapshot total, fixed once at the top of this run — the same honesty rule
    # corpus_review_job.py states for its own `total`: this is the size of the work THIS run
    # set out to do, not a live count. It can only shrink (every new message is numbered by
    # migration 0030's trigger and never enters this set), so it is a safe denominator.
    total = count_conversations_with_unsequenced_messages(db, owner_id)
    processed = 0
    numbered_conversations = 0
    conflicted: set[uuid.UUID] = set()
    messages_assigned = 0
    hit_run_cap = False

    def _lease_lost(where: str) -> None:
        logger.warning(
            "message_sequence_backfill job %s: lease lost (%s) — worker %s, generation %s. Stopping.",
            job_id,
            where,
            worker_id,
            lease_generation,
        )

    try:
        update_progress(db, job, worker_id=worker_id, lease_generation=lease_generation, current=0, total=total, phase="numbering")
        db.commit()
    except JobLeaseLostError:
        _lease_lost("initial progress")
        return

    while True:
        if processed >= MAX_CONVERSATIONS_PER_RUN:
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

        try:
            renew_mainai_job_lease(db, job_id, worker_id, lease_generation, lease_seconds)
            db.commit()
        except JobLeaseLostError:
            _lease_lost("heartbeat")
            return

        # One conversation at a time, re-queried each iteration rather than a list captured up
        # front: a conversation numbered by a concurrent run (or newly emptied) must drop out
        # of the candidate set immediately, and already-conflicted ones must stay out or this
        # loop would rediscover the same unfixable conversation forever.
        candidates = candidate_conversation_ids(db, owner_id, 1, conflicted)
        if not candidates:
            break
        conversation_id = candidates[0]

        # Defensive, and normally unreachable: `total` is a snapshot of a set that can only
        # shrink (every INSERT is numbered by migration 0030's trigger, so no new row can join
        # the unnumbered set). If it somehow did — the only realistic route being the trigger
        # having been dropped out from under a running system — migration 0028's
        # `ck_mainai_jobs_progress_current_le_total` would abort this job on the progress write.
        # Raising the denominator instead reports the larger real workload truthfully rather
        # than failing a job that is doing exactly the right thing.
        if processed + 1 > total:
            logger.warning(
                "message_sequence_backfill job %s: more unnumbered conversations than the start snapshot (%d) — raising total.",
                job_id,
                total,
            )
            total = processed + 1

        # The progress write and the numbering commit together — see backfill_conversation's
        # `on_outcome` docstring. `update_progress` deliberately does not commit; this
        # closure's writes ride along on the single commit backfill_conversation performs.
        #
        # `_job`/`_current`/`_total` are bound as defaults rather than closed over: this
        # function is defined inside a loop, and a late-binding closure over `job`/`processed`/
        # `total` would silently read whatever those hold at CALL time. It happens to be called
        # within the same iteration today, so the values would agree — binding them here makes
        # that a property of the code instead of a property of the current call ordering.
        def _on_outcome(
            outcome: ConversationOutcome,
            _job: MainAIJob = job,
            _current: int = processed + 1,
            _total: int = total,
        ) -> None:
            update_progress(
                db,
                _job,
                worker_id=worker_id,
                lease_generation=lease_generation,
                current=_current,
                total=_total,
                phase="numbering",
            )

        try:
            outcome = backfill_conversation(db, conversation_id, on_outcome=_on_outcome)
        except JobLeaseLostError:
            db.rollback()
            _lease_lost("conversation numbering")
            return
        except Exception:  # noqa: BLE001 - a genuinely unexpected failure still fails the whole job
            db.rollback()
            logger.exception("message_sequence_backfill job %s: unexpected error on conversation %s", job_id, conversation_id)
            job = _refresh(db, job_id)
            if job is not None:
                try:
                    mark_failed(db, job, worker_id=worker_id, lease_generation=lease_generation, error_category=MainAIJobErrorCategory.unexpected)
                except JobLeaseLostError:
                    _lease_lost("mark_failed:unexpected")
            return

        processed += 1
        if outcome.status == "conflict":
            # Left completely unnumbered and remembered, so the next iteration's candidate
            # query skips it. Counted and reported in the completion message — never folded
            # into "processed" as if it had succeeded.
            conflicted.add(conversation_id)
        else:
            numbered_conversations += 1
            messages_assigned += outcome.assigned

    job = _refresh(db, job_id)
    if job is None:
        return
    if job.status != MainAIJobStatus.running:
        return

    remaining = count_conversations_with_unsequenced_messages(db, owner_id)
    remaining_messages = count_unsequenced_messages(db, owner_id)
    parts = [
        f"Numbered {messages_assigned} message(s) across {numbered_conversations} of {total} conversation(s) "
        f"(snapshot at job start)."
    ]
    if conflicted:
        parts.append(f"{len(conflicted)} conversation(s) skipped as conflicting and left unchanged.")
    if hit_run_cap:
        parts.append(f"Stopped at this run's {MAX_CONVERSATIONS_PER_RUN}-conversation cap.")
    if remaining:
        # Explicit, and never softened: a run that ends with work left is not "done". This is
        # the same rule migration 0025's `status` docstring sets for memory_source_backfill_runs
        # ("a capped run that still has real candidates left stays running, not completed") —
        # expressed here in the public message, since `mainai_jobs` has no partial status.
        parts.append(f"{remaining} conversation(s) / {remaining_messages} message(s) still unnumbered — run again.")
    else:
        parts.append("No unnumbered messages remain for this owner.")

    try:
        mark_completed(db, job, worker_id=worker_id, lease_generation=lease_generation, public_message=" ".join(parts)[:500])
    except JobLeaseLostError:
        _lease_lost("mark_completed")
