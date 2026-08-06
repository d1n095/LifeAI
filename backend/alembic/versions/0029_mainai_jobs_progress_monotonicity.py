"""MainAI Runtime Truthfulness and Durable Job Foundation — founder re-review round (PR #36),
fourth pass: `progress_current` can go backwards with no DB-level defense.

Migration 0028 added `ck_mainai_jobs_progress_current_le_total`
(progress_current <= progress_total) but nothing preventing `progress_current` from
DECREASING across an UPDATE — a plain CHECK constraint can only validate a single row's new
values, never compare against the row's previous value, so this needs a trigger, not another
CHECK. Application code (app/rag/corpus_review_job.py) only ever increments `progress_current`
within a single run, so this has never actually happened — but that is exactly the kind of
"nothing but application discipline stands between a future bug and a corrupted row" gap
migration 0028 itself was built to close for the other invariants, and the founder's review
asked for the same defense-in-depth here.

The one legitimate exception: `retry_job()` (app/rag/mainai_jobs_service.py) resets
`progress_current` to 0 on a genuine retry transition (`status: failed -> queued`) — this is
not a bug, it is the explicit, reviewed "start the next attempt's progress count over" reset.
The trigger allows a decrease ONLY when the row is transitioning OUT of `failed` INTO `queued`
with the new `progress_current` at exactly 0; any other decrease (in particular, any decrease
while `status` stays `running`, which is the class of bug this migration defends against) is
rejected. This is a BEFORE UPDATE trigger, so it runs (and can reject) atomically as part of
the same UPDATE statement `app/rag/mainai_jobs_service.py::_guarded_job_write()` already issues
— a stale worker's write is already rejected by the WHERE clause before this trigger would ever
see the row (zero rows matched means the trigger body never executes for that non-matching
row), so this is purely an ADDITIONAL check on writes that already passed the lease-fencing
gate, not a replacement for it.

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-06
"""

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION mainai_jobs_deny_progress_regression() RETURNS trigger AS $$
        BEGIN
            IF NEW.progress_current < OLD.progress_current THEN
                IF NOT (OLD.status = 'failed' AND NEW.status = 'queued' AND NEW.progress_current = 0) THEN
                    RAISE EXCEPTION
                        'mainai_jobs.progress_current cannot decrease (job %, % -> %) outside an explicit failed->queued retry reset to 0',
                        OLD.id, OLD.progress_current, NEW.progress_current;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER mainai_jobs_deny_progress_regression_trigger
            BEFORE UPDATE ON mainai_jobs
            FOR EACH ROW
            EXECUTE FUNCTION mainai_jobs_deny_progress_regression();
    """)


def downgrade() -> None:
    op.execute("""
        DROP TRIGGER mainai_jobs_deny_progress_regression_trigger ON mainai_jobs;
        DROP FUNCTION mainai_jobs_deny_progress_regression();
    """)
