"""MainAI Runtime Truthfulness and Durable Job Foundation — founder re-review round (PR #36):
lease fencing token + DB-level state invariants.

A fresh, from-scratch founder review of the whole integration (not relying on the frozen
branch's own Pass 14-17 history) found a real BLOCKER in `app/jobs/mainai_job_lease.py`:
`renew_mainai_job_lease()` took only `job_id` — no `worker_id`, no proof the caller still
actually holds the claim — so a worker whose lease had already expired and been reclaimed by
a SECOND worker could keep silently re-extending "its own" (no longer real) lease forever, and
`app/rag/corpus_review_job.py`'s processing loop never verified it still held the claim before
writing progress, proposals, or a terminal status. Reproduced live during review: a stale
worker successfully overwrote a legitimate new claimant's lease with zero rejection.

1. `mainai_jobs.lease_generation` — an integer bumped by exactly 1 on every claim AND every
   reclaim (`app/jobs/mainai_job_lease.py::claim_next_mainai_job()`). This is the fencing
   token every worker-driven mutation must now present alongside `worker_id` before it's
   allowed to touch a claimed job — see `app/rag/mainai_jobs_service.py`'s new
   `JobLeaseLostError` and the guarded-update helper it backs. `worker_id` ALONE was
   considered and rejected as the fencing key: `app/worker.py`'s `_worker_id()` can return the
   same value (`settings.worker_id` or the container hostname) across a process restart, so a
   worker_id-only check could not distinguish a genuinely stale execution from a legitimately
   restarted one reusing the same identity. A monotonically-bumped generation number can:
   every claim/reclaim is a NEW generation regardless of whether `locked_by` ends up being the
   same string as before.

2. `mainai_job_events.event_type` gains a new allowed value, `document_skipped` — the same
   founder re-review round asked corpus_review (app/rag/corpus_review_job.py) to stop silently
   folding "this document was deleted mid-run" / "this document has no reviewable content" /
   "the provider failed for this one document" into a plain progress-count increment with no
   durable record of WHICH outcome happened or why. Each now gets its own append-only event
   with a structured, closed-vocabulary reason — never raw exception text, same rule as every
   other event this table already carries.

3. Four new CHECK constraints closing the gap the review named directly: "does the database
   actually prevent impossible combinations" — previously nothing did, application code was
   the only thing standing between a bug (like the lease-fencing bug above) and a genuinely
   corrupted row (progress exceeding its own total, a job "completed" with no completion
   timestamp, a terminal job still carrying an active lease).
   - `ck_mainai_jobs_progress_current_le_total`: progress_current can never exceed
     progress_total once a total is known.
   - `ck_mainai_jobs_completed_at_matches_terminal_status`: `completed_at` (the one shared
     terminal timestamp column every terminal status uses — this schema was never given
     separate `failed_at`/`cancelled_at` columns, and adding them now with no reader that
     would ever use them differently from `completed_at` would be exactly the kind of
     speculative, unused surface this codebase's own conventions avoid) is set if and only if
     status is `completed`, `failed`, or `cancelled`.
   - `ck_mainai_jobs_started_at_set_once_claimed`: `started_at` is required for every status
     except `queued` — a job cannot be `running`, `paused`, or terminal without ever having
     been started.
   - `ck_mainai_jobs_retry_count_within_budget`: `retry_count` can never exceed `max_retries`
     (the existing `ck_mainai_jobs_retry_count_nonneg` from migration 0026 already covers the
     lower bound).

   Deliberately NOT added: a constraint tying `lease_expires_at`/`locked_by` to `status =
   'running'`. Considered, but a `running` job whose lease has genuinely expired (the normal,
   expected state right before a reclaim) still correctly has `locked_by`/`lease_expires_at`
   set to the PREVIOUS (now-stale) worker's values — that is not a corrupt row, it is exactly
   the state `claim_next_mainai_job()`'s reclaim query is designed to find. A CHECK enforcing
   "must have a currently-unexpired lease" would be actively wrong.

   Also deliberately NOT added: any constraint about a "parent/root job" relationship or about
   `output_refs`/result evidence being non-empty on completion — this schema has no
   parent/child job hierarchy at all (no such column exists, and nothing in
   docs/MAINAI_JOB_RUNTIME.md ever specified one), and a `corpus_review` job given zero
   documents legitimately completes with empty `output_refs` (nothing to review is not an
   error) — a NOT EMPTY constraint would be actively wrong there too.

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-06
"""

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE mainai_jobs ADD COLUMN lease_generation integer NOT NULL DEFAULT 0;

        ALTER TABLE mainai_jobs
            ADD CONSTRAINT ck_mainai_jobs_progress_current_le_total
            CHECK (progress_total IS NULL OR progress_current <= progress_total);

        ALTER TABLE mainai_jobs
            ADD CONSTRAINT ck_mainai_jobs_completed_at_matches_terminal_status
            CHECK (
                (status IN ('completed', 'failed', 'cancelled') AND completed_at IS NOT NULL)
                OR (status NOT IN ('completed', 'failed', 'cancelled') AND completed_at IS NULL)
            );

        ALTER TABLE mainai_jobs
            ADD CONSTRAINT ck_mainai_jobs_started_at_set_once_claimed
            CHECK (status = 'queued' OR started_at IS NOT NULL);

        ALTER TABLE mainai_jobs
            ADD CONSTRAINT ck_mainai_jobs_retry_count_within_budget
            CHECK (retry_count <= max_retries);

        ALTER TABLE mainai_job_events DROP CONSTRAINT ck_mainai_job_events_event_type;
        ALTER TABLE mainai_job_events
            ADD CONSTRAINT ck_mainai_job_events_event_type
            CHECK (
                event_type IN (
                    'created', 'claimed', 'heartbeat', 'phase_changed', 'progress_updated',
                    'cancel_requested', 'cancel_acknowledged', 'completed', 'failed',
                    'cancelled', 'retry_scheduled', 'document_skipped'
                )
            );
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE mainai_job_events DROP CONSTRAINT ck_mainai_job_events_event_type;
        ALTER TABLE mainai_job_events
            ADD CONSTRAINT ck_mainai_job_events_event_type
            CHECK (
                event_type IN (
                    'created', 'claimed', 'heartbeat', 'phase_changed', 'progress_updated',
                    'cancel_requested', 'cancel_acknowledged', 'completed', 'failed',
                    'cancelled', 'retry_scheduled'
                )
            );

        ALTER TABLE mainai_jobs DROP CONSTRAINT ck_mainai_jobs_retry_count_within_budget;
        ALTER TABLE mainai_jobs DROP CONSTRAINT ck_mainai_jobs_started_at_set_once_claimed;
        ALTER TABLE mainai_jobs DROP CONSTRAINT ck_mainai_jobs_completed_at_matches_terminal_status;
        ALTER TABLE mainai_jobs DROP CONSTRAINT ck_mainai_jobs_progress_current_le_total;
        ALTER TABLE mainai_jobs DROP COLUMN lease_generation;
    """)
