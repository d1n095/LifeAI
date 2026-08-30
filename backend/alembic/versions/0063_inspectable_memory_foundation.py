"""Stage A — Canonical Inspectable Memory Foundation (Cursor memory-frontier lane).

Lands the minimum durable infrastructure for MainAI's memory-truth invariant
(SAID != STORED != PLANNED != IMPLEMENTED != VERIFIED) WITHOUT creating a second
canonical memory store. Reuses founder_memory_notes / candidate_learning_signals /
work_candidates / engineering_lessons as the real tables; this migration only adds:

1. `memory_truth_claims` — small receipt table for claims MainAI makes about her own
   memory/work state ("I've saved that"), so a claim can be checked against reality.
2. `engineering_lessons.verification_status` — the one genuine truth-state gap on an
   existing status-bearing table (a named regression_test is not itself verification).
3. Registry widen so `candidate_learning_signal`, `work_candidate`, and `project_entity`
   can participate in active_context / memory_threads linking (same CHECK pattern as
   migrations 0049/0051).

Revision ID collision note: Claude PR #197 currently carries pending migrations also
numbered 0063/0064 on its unmerged branch. Those must renumber when #197 rebases onto
tip after this lands — Cursor does not touch #197's files or branch.

See docs/ACTIVE_WORK_CURSOR_MAINAI_MEMORY_FRONTIER.md and
docs/MAINAI_INSPECTABLE_MEMORY_FOUNDATION.md.
"""

from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE engineering_lessons
            ADD COLUMN verification_status varchar(40) NOT NULL DEFAULT 'unverified';
        ALTER TABLE engineering_lessons
            ADD CONSTRAINT ck_engineering_lessons_verification_status CHECK (
                verification_status IN ('unverified', 'verified_by_regression_test', 'disputed')
            );
    """)

    op.execute("""
        CREATE TABLE memory_truth_claims (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            claim_text text NOT NULL,
            claimed_state varchar(24) NOT NULL,
            target_kind varchar(64) NOT NULL,
            target_id uuid,
            verified_at timestamptz,
            verified_result boolean,
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            idempotency_key varchar(128) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_memory_truth_claims_idem UNIQUE (owner_id, idempotency_key),
            CONSTRAINT ck_memory_truth_claims_claim_text CHECK (length(btrim(claim_text)) > 0),
            CONSTRAINT ck_memory_truth_claims_claimed_state CHECK (claimed_state IN (
                'said', 'stored', 'planned', 'implemented', 'verified'
            )),
            CONSTRAINT ck_memory_truth_claims_target_kind CHECK (target_kind IN (
                'founder_memory_note', 'candidate_learning_signal', 'work_candidate',
                'engineering_lesson', 'project_entity', 'mainai_task', 'mainai_goal',
                'memory_truth_claim'
            )),
            -- target_id may be null ONLY for SAID (raw utterance / not yet durable).
            CONSTRAINT ck_memory_truth_claims_said_or_target CHECK (
                claimed_state = 'said' OR target_id IS NOT NULL
            ),
            CONSTRAINT ck_memory_truth_claims_provenance CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE INDEX ix_memory_truth_claims_owner_state ON memory_truth_claims(owner_id, claimed_state);
        CREATE INDEX ix_memory_truth_claims_target ON memory_truth_claims(owner_id, target_kind, target_id);
    """)

    op.execute("""
        ALTER TABLE memory_truth_claims ENABLE ROW LEVEL SECURITY;
        ALTER TABLE memory_truth_claims FORCE ROW LEVEL SECURITY;
        CREATE POLICY memory_truth_claims_isolation ON memory_truth_claims
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    op.execute("""
        CREATE FUNCTION erase_own_memory_truth_claim_children() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE v_owner_id uuid;
        BEGIN
            v_owner_id := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner_id IS NULL THEN
                RAISE EXCEPTION 'erase_own_memory_truth_claim_children requires an authenticated app.current_user_id session context.';
            END IF;
            DELETE FROM public.memory_truth_claims WHERE owner_id = v_owner_id;
        END;
        $$;
        REVOKE ALL ON FUNCTION erase_own_memory_truth_claim_children() FROM PUBLIC;
    """)

    # Registry widen — same three CHECKs migrations 0049/0051 already established.
    op.execute("""
        ALTER TABLE active_context_sets DROP CONSTRAINT ck_active_context_set_anchor_type;
        ALTER TABLE active_context_sets ADD CONSTRAINT ck_active_context_set_anchor_type CHECK(anchor_type IN (
            'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit',
            'document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job',
            'mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence',
            'intelligence_interpretation','intelligence_idea','project','project_note','memory_thread','life_intent',
            'life_intent_blocker','life_problem','life_problem_approach','life_solution_component',
            'life_problem_assumption','life_problem_decision','life_approach_outcome','founder_memory_note',
            'diagnosis_record','capability_record','candidate_learning_signal','work_candidate','project_entity',
            'explicit_topic'
        ));
        ALTER TABLE active_context_members DROP CONSTRAINT ck_active_context_member_object_type;
        ALTER TABLE active_context_members ADD CONSTRAINT ck_active_context_member_object_type CHECK(object_type IN (
            'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit',
            'document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job',
            'mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence',
            'intelligence_interpretation','intelligence_idea','project','project_note','memory_thread','life_intent',
            'life_intent_blocker','life_problem','life_problem_approach','life_solution_component',
            'life_problem_assumption','life_problem_decision','life_approach_outcome','founder_memory_note',
            'diagnosis_record','capability_record','candidate_learning_signal','work_candidate','project_entity'
        ));
        ALTER TABLE memory_thread_members DROP CONSTRAINT ck_memory_thread_member_kind;
        ALTER TABLE memory_thread_members ADD CONSTRAINT ck_memory_thread_member_kind CHECK(member_kind IN (
            'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit',
            'document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job',
            'mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence',
            'intelligence_interpretation','intelligence_idea','project','project_note','active_context_set',
            'life_intent','life_intent_blocker','life_problem','life_problem_approach','life_solution_component',
            'life_problem_assumption','life_problem_decision','life_approach_outcome','founder_memory_note',
            'diagnosis_record','capability_record','candidate_learning_signal','work_candidate','project_entity'
        ));
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE memory_thread_members DROP CONSTRAINT ck_memory_thread_member_kind;
        ALTER TABLE memory_thread_members ADD CONSTRAINT ck_memory_thread_member_kind CHECK(member_kind IN (
            'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit',
            'document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job',
            'mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence',
            'intelligence_interpretation','intelligence_idea','project','project_note','active_context_set',
            'life_intent','life_intent_blocker','life_problem','life_problem_approach','life_solution_component',
            'life_problem_assumption','life_problem_decision','life_approach_outcome','founder_memory_note',
            'diagnosis_record','capability_record'
        ));
        ALTER TABLE active_context_members DROP CONSTRAINT ck_active_context_member_object_type;
        ALTER TABLE active_context_members ADD CONSTRAINT ck_active_context_member_object_type CHECK(object_type IN (
            'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit',
            'document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job',
            'mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence',
            'intelligence_interpretation','intelligence_idea','project','project_note','memory_thread','life_intent',
            'life_intent_blocker','life_problem','life_problem_approach','life_solution_component',
            'life_problem_assumption','life_problem_decision','life_approach_outcome','founder_memory_note',
            'diagnosis_record','capability_record'
        ));
        ALTER TABLE active_context_sets DROP CONSTRAINT ck_active_context_set_anchor_type;
        ALTER TABLE active_context_sets ADD CONSTRAINT ck_active_context_set_anchor_type CHECK(anchor_type IN (
            'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit',
            'document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job',
            'mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence',
            'intelligence_interpretation','intelligence_idea','project','project_note','memory_thread','life_intent',
            'life_intent_blocker','life_problem','life_problem_approach','life_solution_component',
            'life_problem_assumption','life_problem_decision','life_approach_outcome','founder_memory_note',
            'diagnosis_record','capability_record','explicit_topic'
        ));
    """)
    op.execute("DROP FUNCTION erase_own_memory_truth_claim_children();")
    op.execute("DROP TABLE memory_truth_claims;")
    op.execute("""
        ALTER TABLE engineering_lessons DROP CONSTRAINT ck_engineering_lessons_verification_status;
        ALTER TABLE engineering_lessons DROP COLUMN verification_status;
    """)
