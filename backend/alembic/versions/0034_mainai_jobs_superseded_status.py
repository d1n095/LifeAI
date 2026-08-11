"""MainAI V0.2 — `superseded` terminal status for `mainai_jobs` + `superseded_by_job_id`.

Founder review finding while building V0.2 takeover: `claim_next_mainai_job()`'s own claim
SQL (app/jobs/mainai_job_lease.py) already reclaims ANY `running` job whose lease has expired
— same `job_id`, `lease_generation` bumped by 1 — regardless of `job_type`. That is exactly
right for a job type like `corpus_review` (pure reads, safely blind-resumable), but WRONG for
`task_execution`: it is the one job type with real, semi-irreversible external side effects
(local git commits, GitHub pushes — see migration 0033's own `mainai_task_worktrees`), where
blindly handing a dead job back to the generic poll loop is exactly the unsafe "just resume
it" shortcut V0.2 exists to replace with a real inspect -> classify -> salvage gate
(app/mainai_execution/recovery_inspector.py / recovery_classifier.py / recovery_salvage.py).

This migration is the schema half of closing that gap (app/jobs/mainai_job_lease.py's own
`_CLAIM_SQL` is updated in the same commit to exclude `task_execution` from the expired-lease
reclaim branch — `task_execution` jobs stay eligible for a FRESH `queued` claim exactly as
before, they simply never come back to life via blind reclaim once `running`). Once excluded,
a dead `task_execution` job needs an honest terminal outcome once a takeover creates its
replacement — `superseded` is that outcome: the job did not fail and did not complete, it was
formally replaced by `superseded_by_job_id` (a fresh `mainai_jobs` row from a genuinely new
`dispatch_ready_task()` call, per `app/mainai_execution/recovery_takeover.py`). Sitting at
`running` forever with a dead lease would itself be untruthful (this codebase's own standing
rule: no fake/misleading status) and would keep confusing UIs/queries that treat `running` as
"someone is actively working on this right now".

`ck_mainai_jobs_completed_at_matches_terminal_status` (migration 0028) is extended to treat
`superseded` as terminal (requires `completed_at`), matching `completed`/`failed`/`cancelled`.
A new CHECK ties `status = 'superseded'` and `superseded_by_job_id IS NOT NULL` together in
both directions — one is never true without the other.

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-11
"""

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE mainai_jobs ADD COLUMN superseded_by_job_id uuid REFERENCES mainai_jobs(id) ON DELETE SET NULL;

        ALTER TABLE mainai_jobs DROP CONSTRAINT ck_mainai_jobs_status;
        ALTER TABLE mainai_jobs ADD CONSTRAINT ck_mainai_jobs_status CHECK (
            status IN ('queued', 'running', 'paused', 'completed', 'failed', 'cancelled', 'superseded')
        );

        ALTER TABLE mainai_jobs DROP CONSTRAINT ck_mainai_jobs_completed_at_matches_terminal_status;
        ALTER TABLE mainai_jobs ADD CONSTRAINT ck_mainai_jobs_completed_at_matches_terminal_status CHECK (
            (status IN ('completed', 'failed', 'cancelled', 'superseded') AND completed_at IS NOT NULL)
            OR (status NOT IN ('completed', 'failed', 'cancelled', 'superseded') AND completed_at IS NULL)
        );

        ALTER TABLE mainai_jobs ADD CONSTRAINT ck_mainai_jobs_superseded_by_matches_status CHECK (
            (status = 'superseded') = (superseded_by_job_id IS NOT NULL)
        );

        CREATE INDEX ix_mainai_jobs_superseded_by_job_id ON mainai_jobs USING btree (superseded_by_job_id);
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE mainai_jobs DROP CONSTRAINT ck_mainai_jobs_superseded_by_matches_status;

        ALTER TABLE mainai_jobs DROP CONSTRAINT ck_mainai_jobs_completed_at_matches_terminal_status;
        ALTER TABLE mainai_jobs ADD CONSTRAINT ck_mainai_jobs_completed_at_matches_terminal_status CHECK (
            (status IN ('completed', 'failed', 'cancelled') AND completed_at IS NOT NULL)
            OR (status NOT IN ('completed', 'failed', 'cancelled') AND completed_at IS NULL)
        );

        ALTER TABLE mainai_jobs DROP CONSTRAINT ck_mainai_jobs_status;
        ALTER TABLE mainai_jobs ADD CONSTRAINT ck_mainai_jobs_status CHECK (
            status IN ('queued', 'running', 'paused', 'completed', 'failed', 'cancelled')
        );

        DROP INDEX ix_mainai_jobs_superseded_by_job_id;
        ALTER TABLE mainai_jobs DROP COLUMN superseded_by_job_id;
    """)
