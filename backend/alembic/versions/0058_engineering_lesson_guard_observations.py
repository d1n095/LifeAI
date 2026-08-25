"""Record what happened to an applied EngineeringLesson's own regression guard.

Migration 0032 gave lessons a durable home, and the runtime sweep's own #134 finally gave them
a production WRITER (`lesson_from_verification.py`: exhausted, structured verification failure
-> lesson). `apply_lessons_to_verification_plan()` gives them a production READER. What has
never existed is any record of what happened NEXT:

    lesson recorded -> lesson applied to a plan -> ??? -> nothing ever looked back

Nothing in the system had ever observed what happened to a lesson's own `regression_test` on
the tasks it was actually applied to. That is the same STATE EXISTS != DRIVER EXISTS shape this
lane has been removing everywhere else, applied to learning itself.

GUARD EVIDENCE != LESSON EFFECTIVENESS. This table is deliberately NOT called effectiveness,
because the evidence available at task-finalize cannot support that claim. Read what applying
a lesson actually does today: `apply_lessons_to_verification_plan()` appends the lesson's
`regression_test` to the task's `verification_plan` as a `targeted_tests` step and records
`lessons_applied`. It does not change the implementation strategy, the constraints, or the work
itself. So when that step later passes, the only thing proven is:

    this lesson's named guard was exercised, and it held in this execution context

and specifically NOT:

    the lesson changed how the work was done, or caused the task to succeed

Calling the first one "effectiveness" would manufacture exactly the false causal confidence
this subsystem exists to avoid -- a learning system that tells itself flattering stories about
its own behavior has not learned anything. Every column, enum value and confidence level here
is therefore named for the OBSERVATION, not for a verdict about the lesson.

What a real effectiveness claim would require, and what is deliberately NOT built here: durable
provenance for HOW a lesson altered the later plan or execution (lesson -> changed planning
decision -> affected step/constraint/strategy -> execution -> comparable outcome). That edge
does not exist yet. When it does, these observations become one input to it rather than being
retrofitted into a claim they never supported.

Fail-closed about attribution even at this weaker level. A row is written only when the lesson
was durably recorded as APPLIED to that exact task at plan time (`lessons_applied`), and the
outcome is derived only from that lesson's OWN `regression_test` target as it appears in that
finalize's structured verification evidence. If the target is not in the evidence, the outcome
is `guard_not_exercised` -- an unrelated later success is never evidence about a lesson.

Owner-scoped RLS with composite owner-anchored FKs, not founder-wide like `engineering_lessons`
itself. The lesson is founder-wide, but an observation carries owner-scoped facts (`task_id`/
`goal_id`/`job_id` and a verification target path from that owner's plan), so it inherits the
sensitivity of its evidence, not of its subject. `lesson_id` is necessarily a bare FK --
`engineering_lessons` has no `owner_id` to anchor against -- which is the correct direction:
many owners' evidence may reference one founder-wide lesson. Every OTHER reference uses the
composite `(id, owner_id)` anchors migrations 0032/0027 already provide, so a bare FK can never
let `owner_id=A` cite owner B's task -- the defect class migration 0056 had to retrofit,
applied up-front here.

COLUMN-SPECIFIC `ON DELETE SET NULL (job_id)` on the job reference, not a plain `SET NULL`.
This was found by attacking the constraint rather than reading it: a plain composite
`ON DELETE SET NULL` nulls EVERY referencing column, and `owner_id` is `NOT NULL`, so deleting
a `mainai_jobs` row would have failed outright with a not-null violation -- an unrelated
observation would have been able to block job cleanup entirely, and the "observation survives
its job" intent would have been silently unreachable. Postgres 15+ column lists make the actual
intent expressible: drop only the job pointer, keep the owner, keep the observation. Verified
by `pg_constraint.confdelsetcols` and by a real delete regression test, never assumed.

Idempotency is structural: `uq_engineering_lesson_guard_observations_source_ref` on the
`(goal, task, job, lesson)` provenance string means a replayed/retried finalize cannot
duplicate an observation. The writer inserts with ON CONFLICT DO NOTHING rather than
select-then-insert, so a duplicate can never raise inside -- and thereby poison -- the caller's
own task-finalize transaction.
"""

from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE engineering_lesson_guard_observations (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            lesson_id uuid NOT NULL REFERENCES engineering_lessons(id) ON DELETE CASCADE,
            task_id uuid NOT NULL,
            goal_id uuid NOT NULL,
            job_id uuid,
            outcome varchar(40) NOT NULL,
            evidence_strength varchar(8) NOT NULL,
            relevance_reason text NOT NULL,
            guard_target varchar(256),
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            source_ref varchar(320) NOT NULL,
            observed_at timestamp NOT NULL DEFAULT now(),
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_engineering_lesson_guard_observations_source_ref UNIQUE (source_ref),
            CONSTRAINT fk_engineering_lesson_guard_observations_task_owner FOREIGN KEY (task_id, owner_id)
                REFERENCES mainai_tasks (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_engineering_lesson_guard_observations_goal_owner FOREIGN KEY (goal_id, owner_id)
                REFERENCES mainai_goals (id, owner_id) ON DELETE CASCADE,
            -- Column list is load-bearing: without it, SET NULL would also target the NOT NULL
            -- owner_id and every mainai_jobs delete would fail. See module docstring.
            CONSTRAINT fk_engineering_lesson_guard_observations_job_owner FOREIGN KEY (job_id, owner_id)
                REFERENCES mainai_jobs (id, owner_id) ON DELETE SET NULL (job_id),
            CONSTRAINT ck_engineering_lesson_guard_observations_outcome CHECK (outcome IN (
                'guard_held', 'guard_held_task_failed_elsewhere', 'guard_failed',
                'guard_unusable', 'guard_not_exercised', 'lesson_superseded'
            )),
            CONSTRAINT ck_engineering_lesson_guard_observations_strength CHECK (
                evidence_strength IN ('direct', 'partial', 'none')
            ),
            CONSTRAINT ck_engineering_lesson_guard_observations_evidence CHECK (jsonb_typeof(evidence) = 'object')
        );
        CREATE INDEX ix_engineering_lesson_guard_observations_lesson
            ON engineering_lesson_guard_observations(lesson_id, outcome);
        CREATE INDEX ix_engineering_lesson_guard_observations_owner_task
            ON engineering_lesson_guard_observations(owner_id, task_id);

        ALTER TABLE engineering_lesson_guard_observations ENABLE ROW LEVEL SECURITY;
        ALTER TABLE engineering_lesson_guard_observations FORCE ROW LEVEL SECURITY;
        CREATE POLICY engineering_lesson_guard_observations_isolation ON engineering_lesson_guard_observations
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    # No explicit GRANT/REVOKE here, matching every migration since 0004: mainai_app's baseline
    # access comes from ALTER DEFAULT PRIVILEGES, and the append-only narrowing (SELECT/INSERT
    # only, the same treatment mainai_task_events / capability_observation_events already get --
    # an observation records what was seen at one finalize and has no legitimate in-place
    # update) is applied AND verified on every boot by app/rls.py, wired in this SAME change
    # rather than deferred (see the Section 0033 P1 note that policy documents for what happens
    # when that step is skipped).

    # Account erasure: rows are removed by the composite FKs' ON DELETE CASCADE from
    # mainai_tasks/mainai_goals, which erase_own_mainai_execution_children() already deletes
    # for the calling owner -- no separate erasure function is needed here. Proven by a
    # cascade regression test rather than assumed.


def downgrade() -> None:
    op.execute("DROP TABLE engineering_lesson_guard_observations;")
