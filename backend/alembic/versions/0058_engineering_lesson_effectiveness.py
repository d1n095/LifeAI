"""Close the EngineeringLesson learning loop's missing back-edge.

Migration 0032 gave lessons a durable home, and the runtime sweep's own #134 finally gave
them a production WRITER (`lesson_from_verification.py`: exhausted, structured verification
failure -> lesson). `apply_lessons_to_verification_plan()` gives them a production READER
(plan-time regression-target injection, recorded as `lessons_applied` on the task's `created`
MainAITaskEvent). What has never existed is the edge that closes the loop:

    lesson recorded -> lesson applied to a plan -> ??? -> was applying it worth anything?

Until now nothing in the system ever observed what happened to a lesson's own named
`regression_test` on the tasks it was actually applied to. A lesson's `confidence` could only
ever be whatever its writer asserted at birth; no later evidence could reach it. That is the
same STATE EXISTS != DRIVER EXISTS shape this lane has been removing everywhere else, applied
to learning itself.

SIGNAL PRODUCER != TRUTH WRITER, the same discipline migrations 0053/0054/0055/0057 already
established: `engineering_lesson_effectiveness` rows are APPEND-ONLY OBSERVATIONS. They never
rewrite `engineering_lessons.confidence`/`evidence`/`root_cause`/`status`, and nothing in this
migration or the service on top of it promotes an observation into a truth claim about a
lesson. Aggregating them into a founder-reviewable confidence signal is a deliberately
separate, later step.

Fail-closed about CAUSALITY, which is the whole risk in a table like this. An unrelated later
success is NOT evidence a lesson worked. A row is written only when the lesson was durably
recorded as APPLIED to that exact task at plan time (`lessons_applied`), and the outcome is
derived only from that lesson's OWN `regression_test` target as it appears in that finalize's
structured verification evidence. If the lesson's target is not in the evidence, the outcome
is `insufficient_evidence` -- never `reinforced`.

Owner-scoped RLS with composite owner-anchored FKs, not founder-wide like `engineering_lessons`
itself. The lesson is founder-wide, but an effectiveness ROW carries owner-scoped facts
(`task_id`/`goal_id`/`job_id` and a verification target path from that owner's plan), so it
inherits the sensitivity of its evidence, not of its subject. `lesson_id` is necessarily a bare
FK -- `engineering_lessons` has no `owner_id` to anchor against -- which is the correct
direction: many owners' evidence may reference one founder-wide lesson. Every OTHER reference
uses the composite `(id, owner_id)` anchors migrations 0032/0027 already provide
(`uq_mainai_tasks_id_owner_id`, `uq_mainai_goals_id_owner_id`, `uq_mainai_jobs_id_owner_id`),
so a bare FK can never let `owner_id=A` cite owner B's task -- the same defect class migration
0056 had to retrofit, applied up-front here.

Idempotency is structural: `uq_engineering_lesson_effectiveness_source_ref` on the
`(goal, task, job, lesson)` provenance string means a replayed/retried finalize cannot inflate
a lesson's evidence by re-observing the same outcome. The writer inserts with ON CONFLICT DO
NOTHING rather than select-then-insert, so a duplicate can never raise inside -- and thereby
poison -- the caller's own task-finalize transaction.
"""

from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE engineering_lesson_effectiveness (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            lesson_id uuid NOT NULL REFERENCES engineering_lessons(id) ON DELETE CASCADE,
            task_id uuid NOT NULL,
            goal_id uuid NOT NULL,
            job_id uuid,
            outcome varchar(24) NOT NULL,
            attribution_confidence varchar(8) NOT NULL,
            relevance_reason text NOT NULL,
            verification_target varchar(256),
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            source_ref varchar(320) NOT NULL,
            observed_at timestamp NOT NULL DEFAULT now(),
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_engineering_lesson_effectiveness_source_ref UNIQUE (source_ref),
            CONSTRAINT fk_engineering_lesson_effectiveness_task_owner FOREIGN KEY (task_id, owner_id)
                REFERENCES mainai_tasks (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_engineering_lesson_effectiveness_goal_owner FOREIGN KEY (goal_id, owner_id)
                REFERENCES mainai_goals (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_engineering_lesson_effectiveness_job_owner FOREIGN KEY (job_id, owner_id)
                REFERENCES mainai_jobs (id, owner_id) ON DELETE SET NULL,
            CONSTRAINT ck_engineering_lesson_effectiveness_outcome CHECK (outcome IN (
                'reinforced', 'weakened', 'contradicted', 'context_specific',
                'insufficient_evidence', 'superseded'
            )),
            CONSTRAINT ck_engineering_lesson_effectiveness_confidence CHECK (
                attribution_confidence IN ('high', 'medium', 'low')
            ),
            CONSTRAINT ck_engineering_lesson_effectiveness_evidence CHECK (jsonb_typeof(evidence) = 'object')
        );
        CREATE INDEX ix_engineering_lesson_effectiveness_lesson ON engineering_lesson_effectiveness(lesson_id, outcome);
        CREATE INDEX ix_engineering_lesson_effectiveness_owner_task ON engineering_lesson_effectiveness(owner_id, task_id);

        ALTER TABLE engineering_lesson_effectiveness ENABLE ROW LEVEL SECURITY;
        ALTER TABLE engineering_lesson_effectiveness FORCE ROW LEVEL SECURITY;
        CREATE POLICY engineering_lesson_effectiveness_isolation ON engineering_lesson_effectiveness
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    # No explicit GRANT/REVOKE here, matching every migration since 0004: mainai_app's baseline
    # access comes from ALTER DEFAULT PRIVILEGES, and the append-only narrowing (SELECT/INSERT
    # only, the same treatment mainai_task_events / capability_observation_events already get --
    # an effectiveness OBSERVATION records what was seen at one finalize and has no legitimate
    # in-place update) is applied AND verified on every boot by app/rls.py, wired in this SAME
    # change rather than deferred (see the Section 0033 P1 note that policy documents for what
    # happens when that step is skipped).

    # Account erasure: rows are removed by the composite FKs' ON DELETE CASCADE from
    # mainai_tasks/mainai_goals, which erase_own_mainai_execution_children() already deletes
    # for the calling owner -- no separate erasure function is needed here.


def downgrade() -> None:
    op.execute("DROP TABLE engineering_lesson_effectiveness;")
