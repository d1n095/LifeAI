"""Life Founder/User Memory foundation.

Answers the "Grundarminne" gap `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §2/§4.3 (P6) named
and left designed-but-not-built, and the same gap `docs/LIFE_REQUIREMENT_TRACEABILITY.md` §8
confirmed still missing. Builds the smallest durable layer that lets Life distinguish and link
founder/user facts from project facts, world/system facts, and Life's own self-model facts,
without collapsing any of them into another.

`founder_memory_notes` plays the SAME structural role `life_problem_decisions` (migration 0042)
already plays for problem-solving decisions -- a mutable row (status transitions:
active -> superseded/disputed) with a self-referential `supersedes_note_id`, the SAME
`authority`/`basis` closed vocabularies migration 0042 already established, reused verbatim,
never a second competing provenance taxonomy. `content` is never rewritten in place; a
correction always INSERTs a new row and supersedes the old one -- old rows are never deleted,
matching `life_problem_decisions`' own "superseded, not erased" doctrine. No append-only
companion event table is added: exactly like `life_problem_decisions` itself (as opposed to its
satellite `life_problem_events` log), the row-level supersession chain IS the history.

Hard rule, structural, not just documented: there is no `note_type` value, no column, and no
vocabulary anywhere in this migration or the module it supports for emotional/psychological
state -- the same "no hidden diagnosis" doctrine `app.context.resolver` already established and
is tested for (`test_never_infers_emotional_or_psychological_state`). If a future caller wants
that signal, it may only ever come from the founder's own explicit words captured verbatim in
`content` with `authority='founder'`/`basis='manual'` -- never inferred by this module.

Extends the SAME central object-reference registry `app.active_context.service._require_ref()`
already established (reused across `memory_threads`, `work_intelligence`, `life_intents`,
`problem_learning`) with exactly one new entry, `founder_memory_note` -- never a second,
competing linking mechanism. This requires widening the SAME three CHECK constraints migration
0042 last widened (`active_context_sets.anchor_type`, `active_context_members.object_type`,
`memory_thread_members.member_kind`) by exactly one value each -- the established, already-used
pattern for adding a new linkable entity type, not a new mechanism.
"""

from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE founder_memory_notes (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            note_type varchar(24) NOT NULL DEFAULT 'unknown',
            content text NOT NULL,
            status varchar(24) NOT NULL DEFAULT 'active',
            authority varchar(40) NOT NULL DEFAULT 'unknown',
            basis varchar(24) NOT NULL DEFAULT 'unknown',
            confidence numeric(5,4),
            source text,
            supersedes_note_id uuid,
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            idempotency_key varchar(128) NOT NULL,
            observed_at timestamp NOT NULL DEFAULT now(),
            valid_from timestamp,
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_founder_memory_notes_id_owner UNIQUE (id, owner_id),
            CONSTRAINT uq_founder_memory_notes_idem UNIQUE (owner_id, idempotency_key),
            CONSTRAINT fk_founder_memory_notes_supersedes FOREIGN KEY (supersedes_note_id, owner_id)
                REFERENCES founder_memory_notes (id, owner_id),
            CONSTRAINT ck_founder_memory_notes_type CHECK (note_type IN (
                'decision', 'correction', 'preference', 'goal', 'recurring_pattern', 'observation', 'unknown'
            )),
            CONSTRAINT ck_founder_memory_notes_status CHECK (status IN ('active', 'superseded', 'disputed', 'unknown')),
            CONSTRAINT ck_founder_memory_notes_authority CHECK (authority IN (
                'founder', 'repeated_founder_preference', 'deterministic_source', 'inferred_pattern',
                'ai_interpretation', 'unknown'
            )),
            CONSTRAINT ck_founder_memory_notes_basis CHECK (basis IN (
                'manual', 'deterministic', 'imported', 'inferred', 'ai_interpretation', 'unknown'
            )),
            CONSTRAINT ck_founder_memory_notes_confidence CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
            CONSTRAINT ck_founder_memory_notes_content CHECK (length(btrim(content)) > 0),
            CONSTRAINT ck_founder_memory_notes_provenance CHECK (jsonb_typeof(provenance) = 'object'),
            CONSTRAINT ck_founder_memory_notes_no_self CHECK (supersedes_note_id IS NULL OR supersedes_note_id <> id)
        );
        CREATE INDEX ix_founder_memory_notes_owner_type_status ON founder_memory_notes(owner_id, note_type, status);
        CREATE INDEX ix_founder_memory_notes_supersedes ON founder_memory_notes(supersedes_note_id);
    """)

    op.execute("""
        ALTER TABLE founder_memory_notes ENABLE ROW LEVEL SECURITY;
        ALTER TABLE founder_memory_notes FORCE ROW LEVEL SECURITY;
        CREATE POLICY founder_memory_notes_isolation ON founder_memory_notes
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    op.execute("""
        ALTER TABLE active_context_sets DROP CONSTRAINT ck_active_context_set_anchor_type;
        ALTER TABLE active_context_sets ADD CONSTRAINT ck_active_context_set_anchor_type CHECK(anchor_type IN (
            'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit',
            'document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job',
            'mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence',
            'intelligence_interpretation','intelligence_idea','project','project_note','memory_thread','life_intent',
            'life_intent_blocker','life_problem','life_problem_approach','life_solution_component',
            'life_problem_assumption','life_problem_decision','life_approach_outcome','founder_memory_note','explicit_topic'
        ));
        ALTER TABLE active_context_members DROP CONSTRAINT ck_active_context_member_object_type;
        ALTER TABLE active_context_members ADD CONSTRAINT ck_active_context_member_object_type CHECK(object_type IN (
            'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit',
            'document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job',
            'mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence',
            'intelligence_interpretation','intelligence_idea','project','project_note','memory_thread','life_intent',
            'life_intent_blocker','life_problem','life_problem_approach','life_solution_component',
            'life_problem_assumption','life_problem_decision','life_approach_outcome','founder_memory_note'
        ));
        ALTER TABLE memory_thread_members DROP CONSTRAINT ck_memory_thread_member_kind;
        ALTER TABLE memory_thread_members ADD CONSTRAINT ck_memory_thread_member_kind CHECK(member_kind IN (
            'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit',
            'document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job',
            'mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence',
            'intelligence_interpretation','intelligence_idea','project','project_note','active_context_set',
            'life_intent','life_intent_blocker','life_problem','life_problem_approach','life_solution_component',
            'life_problem_assumption','life_problem_decision','life_approach_outcome','founder_memory_note'
        ));
    """)

    op.execute("""
        CREATE FUNCTION erase_own_founder_memory_children() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE v_owner_id uuid;
        BEGIN
            v_owner_id := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner_id IS NULL THEN
                RAISE EXCEPTION 'erase_own_founder_memory_children requires an authenticated app.current_user_id session context.';
            END IF;
            DELETE FROM public.founder_memory_notes WHERE owner_id = v_owner_id;
        END;
        $$;
        REVOKE ALL ON FUNCTION erase_own_founder_memory_children() FROM PUBLIC;
    """)


def downgrade() -> None:
    op.execute("""
        DROP FUNCTION erase_own_founder_memory_children();

        ALTER TABLE memory_thread_members DROP CONSTRAINT ck_memory_thread_member_kind;
        ALTER TABLE memory_thread_members ADD CONSTRAINT ck_memory_thread_member_kind CHECK(member_kind IN (
            'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit',
            'document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job',
            'mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence',
            'intelligence_interpretation','intelligence_idea','project','project_note','active_context_set',
            'life_intent','life_intent_blocker','life_problem','life_problem_approach','life_solution_component',
            'life_problem_assumption','life_problem_decision','life_approach_outcome'
        ));
        ALTER TABLE active_context_members DROP CONSTRAINT ck_active_context_member_object_type;
        ALTER TABLE active_context_members ADD CONSTRAINT ck_active_context_member_object_type CHECK(object_type IN (
            'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit',
            'document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job',
            'mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence',
            'intelligence_interpretation','intelligence_idea','project','project_note','memory_thread','life_intent',
            'life_intent_blocker','life_problem','life_problem_approach','life_solution_component',
            'life_problem_assumption','life_problem_decision','life_approach_outcome'
        ));
        ALTER TABLE active_context_sets DROP CONSTRAINT ck_active_context_set_anchor_type;
        ALTER TABLE active_context_sets ADD CONSTRAINT ck_active_context_set_anchor_type CHECK(anchor_type IN (
            'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit',
            'document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job',
            'mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence',
            'intelligence_interpretation','intelligence_idea','project','project_note','memory_thread','life_intent',
            'life_intent_blocker','life_problem','life_problem_approach','life_solution_component',
            'life_problem_assumption','life_problem_decision','life_approach_outcome','explicit_topic'
        ));

        DROP TABLE founder_memory_notes;
    """)
