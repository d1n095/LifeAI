"""The lesson -> plan-decision PROVENANCE EDGE that migration 0058 explicitly deferred.

0058 built the guard-OBSERVATION table (what a lesson's regression test did at finalize)
and its own docstring drew the exact line this migration now builds:

    "What a real effectiveness claim would require, and what is deliberately NOT built
    here: durable provenance for HOW a lesson altered the later plan (lesson -> changed
    planning decision -> affected step/constraint/strategy -> execution -> comparable
    outcome). That edge does not exist yet. When it does, these observations become one
    input to it rather than being retrofitted into a claim they never supported."

Until now, "this lesson shaped that task's plan" lived only in a MainAITaskEvent detail
list (`lessons_applied`) — an event-history string, not a queryable edge. `record_lesson_
guard_observations_from_finalize()` re-derives it per finalize from that event, which works
for observing the guard but cannot answer the effectiveness question across plans: how many
plans did this lesson actually change, and what did its guard do where it did?

This table is the CHANGED-PLANNING-DECISION half, written where the change actually happens
(planner.create_plan, right after apply_lessons_to_verification_plan appended the lesson's
regression target to a task's verification_plan): one row per (lesson, task) that the lesson
really shaped. With it, the effectiveness rollup (lesson_guard_observations.py's
lesson_effectiveness()) joins DECISIONS to OBSERVATIONS: applied-count from here,
guard-held/failed counts from 0058 — the measured form of "recurring failures lead to
improvements in decision rules", without either inventing causality (the observation table's
own discipline stands: guard evidence is guard evidence) or letting an ineffective rule hide
(lessons whose guard FAILED on its merits surface as needs_reassessment for founder review).

Append-only by construction: a plan-time decision is history — it is never updated in
place, and a task's own re-plan writes NEW rows for the NEW plan version (the source_ref
carries the goal/task/lesson triple, and a re-planned task gets a new task row, so
idempotency on source_ref still protects against double-writes within one planning pass).

Owner anchoring and narrowing mirror migration 0058 exactly: composite (id, owner_id) FKs
against mainai_tasks/mainai_goals (0032/0027's anchors) so a bare FK can never cite another
owner's task; lesson_id stays a bare FK because engineering_lessons is deliberately
founder-wide; append-only narrowing (SELECT/INSERT only) applied AND verified on every boot
by app/rls.py, wired in this SAME change.
"""

from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE engineering_lesson_plan_decisions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            lesson_id uuid NOT NULL REFERENCES engineering_lessons(id) ON DELETE CASCADE,
            task_id uuid NOT NULL,
            goal_id uuid NOT NULL,
            decision_type varchar(40) NOT NULL,
            affected_step jsonb NOT NULL DEFAULT '{}'::jsonb,
            decided_at timestamp NOT NULL DEFAULT now(),
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_engineering_lesson_plan_decisions_source_ref UNIQUE (task_id, lesson_id),
            CONSTRAINT fk_engineering_lesson_plan_decisions_task_owner FOREIGN KEY (task_id, owner_id)
                REFERENCES mainai_tasks (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_engineering_lesson_plan_decisions_goal_owner FOREIGN KEY (goal_id, owner_id)
                REFERENCES mainai_goals (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_engineering_lesson_plan_decisions_type CHECK (decision_type IN (
                'verification_step_added'
            )),
            CONSTRAINT ck_engineering_lesson_plan_decisions_step CHECK (jsonb_typeof(affected_step) = 'object')
        );
        CREATE INDEX ix_engineering_lesson_plan_decisions_lesson
            ON engineering_lesson_plan_decisions(lesson_id);
        CREATE INDEX ix_engineering_lesson_plan_decisions_owner_task
            ON engineering_lesson_plan_decisions(owner_id, task_id);

        ALTER TABLE engineering_lesson_plan_decisions ENABLE ROW LEVEL SECURITY;
        ALTER TABLE engineering_lesson_plan_decisions FORCE ROW LEVEL SECURITY;
        CREATE POLICY engineering_lesson_plan_decisions_isolation ON engineering_lesson_plan_decisions
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    # No explicit GRANT/REVOKE here, matching every migration since 0004: the append-only
    # narrowing (SELECT/INSERT only — a plan-time decision is never rewritten) is applied AND
    # verified on every boot by app/rls.py, wired in this SAME change. Account erasure runs
    # through the composite FKs' ON DELETE CASCADE from mainai_tasks/mainai_goals, which
    # erase_own_mainai_execution_children() already deletes for the calling owner.


def downgrade() -> None:
    op.execute("DROP TABLE engineering_lesson_plan_decisions;")
