"""Life Causal Diagnosis Interface.

Answers mission item 6 of "LIFE SELF-MODEL, ADAPTIVE COGNITION & CORPUS READINESS": Life must
be able to distinguish, using evidence rather than hard-coded labels, WHY a step failed --
code regression, concurrency/timing issue, stale state, environment/configuration issue,
external service failure, dependency failure, authorization/authority blocker, missing
capability/tool, or genuinely unknown cause -- and must never conflate an unproven hypothesis
with a proven cause. "A failed step does NOT automatically mean the code change is bad" is the
concrete behavior this migration exists to make representable.

`EngineeringLesson.root_cause` (migration 0032) already exists but is a single free-text field
written only AFTER a lesson is fully understood -- it has no concept of an intermediate,
unproven hypothesis, and no closed causal-category vocabulary at all. `RecoveryClassification`
(migration 0033) is a DIFFERENT, narrower taxonomy (how much of a dead agent's own work was
salvageable), not a general failure-cause taxonomy. Neither is duplicated or modified here;
`diagnosis_records` is a new, small, general-purpose layer that MAY later reference either as
evidence, but never replaces or rewrites them.

Same structural role `founder_memory_notes` (migration 0049) already plays: a mutable row per
diagnosis, `observation` immutable once created (the raw, factual observation -- what was
actually seen, never itself an interpretation), `epistemic_stage` distinguishing `observed` /
`hypothesis` / `proven_cause` / `ruled_out` as genuinely different epistemic states (never
silently promoted from one to the next without the caller's own explicit action),
`supersedes_diagnosis_id` (self-referential, no cycles) for a later diagnosis correcting an
earlier one while preserving both. `authority`/`basis` reuse migration 0042's exact
vocabularies, verbatim, never a third competing taxonomy.

Hard rule, structural, not just documented: `epistemic_stage='proven_cause'` REQUIRES a real
`proven_evidence_id` reference (a nullable FK to `intelligence_evidence`, migration 0038) --
enforced by CHECK constraint, not just caller discipline. A caller cannot mark a diagnosis
`proven_cause` on a guess; the database itself refuses the row.

Extends `app.active_context.service`'s central object-reference registry (already extended
once for `founder_memory_note` in migration 0049) with exactly one new entry,
`diagnosis_record` -- the same mechanism, never a new linking system. This requires widening
the SAME three CHECK constraints already widened twice
(`active_context_sets.anchor_type`/`active_context_members.object_type`/
`memory_thread_members.member_kind`) by one more value each."""

from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE diagnosis_records (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            observation text NOT NULL,
            hypothesis_category varchar(32) NOT NULL DEFAULT 'unknown',
            hypothesis_reasoning text,
            epistemic_stage varchar(16) NOT NULL DEFAULT 'observed',
            confidence numeric(5,4),
            authority varchar(40) NOT NULL DEFAULT 'unknown',
            basis varchar(24) NOT NULL DEFAULT 'unknown',
            proven_evidence_id uuid,
            supersedes_diagnosis_id uuid,
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            idempotency_key varchar(128) NOT NULL,
            observed_at timestamp NOT NULL DEFAULT now(),
            created_at timestamp NOT NULL DEFAULT now(),
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_diagnosis_records_id_owner UNIQUE (id, owner_id),
            CONSTRAINT uq_diagnosis_records_idem UNIQUE (owner_id, idempotency_key),
            CONSTRAINT fk_diagnosis_records_evidence_owner FOREIGN KEY (proven_evidence_id, owner_id)
                REFERENCES intelligence_evidence (id, owner_id) ON DELETE SET NULL,
            CONSTRAINT fk_diagnosis_records_supersedes FOREIGN KEY (supersedes_diagnosis_id, owner_id)
                REFERENCES diagnosis_records (id, owner_id),
            CONSTRAINT ck_diagnosis_records_category CHECK (hypothesis_category IN (
                'code_regression', 'concurrency_timing', 'stale_state', 'environment_configuration',
                'external_service_failure', 'dependency_failure', 'authorization_blocker',
                'missing_capability', 'unknown'
            )),
            CONSTRAINT ck_diagnosis_records_stage CHECK (epistemic_stage IN (
                'observed', 'hypothesis', 'proven_cause', 'ruled_out'
            )),
            CONSTRAINT ck_diagnosis_records_authority CHECK (authority IN (
                'founder', 'repeated_founder_preference', 'deterministic_source', 'inferred_pattern',
                'ai_interpretation', 'unknown'
            )),
            CONSTRAINT ck_diagnosis_records_basis CHECK (basis IN (
                'manual', 'deterministic', 'imported', 'inferred', 'ai_interpretation', 'unknown'
            )),
            CONSTRAINT ck_diagnosis_records_confidence CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
            CONSTRAINT ck_diagnosis_records_observation CHECK (length(btrim(observation)) > 0),
            CONSTRAINT ck_diagnosis_records_provenance CHECK (jsonb_typeof(provenance) = 'object'),
            CONSTRAINT ck_diagnosis_records_no_self CHECK (supersedes_diagnosis_id IS NULL OR supersedes_diagnosis_id <> id),
            -- The hard rule: a diagnosis can never be marked proven_cause without a real,
            -- owner-verified evidence reference. Never caller discipline alone.
            CONSTRAINT ck_diagnosis_records_proven_requires_evidence CHECK (
                epistemic_stage <> 'proven_cause' OR proven_evidence_id IS NOT NULL
            )
        );
        CREATE INDEX ix_diagnosis_records_owner_stage ON diagnosis_records(owner_id, epistemic_stage);
        CREATE INDEX ix_diagnosis_records_owner_category ON diagnosis_records(owner_id, hypothesis_category);
        CREATE INDEX ix_diagnosis_records_supersedes ON diagnosis_records(supersedes_diagnosis_id);
    """)

    op.execute("""
        ALTER TABLE diagnosis_records ENABLE ROW LEVEL SECURITY;
        ALTER TABLE diagnosis_records FORCE ROW LEVEL SECURITY;
        CREATE POLICY diagnosis_records_isolation ON diagnosis_records
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
            'life_problem_assumption','life_problem_decision','life_approach_outcome','founder_memory_note',
            'diagnosis_record','explicit_topic'
        ));
        ALTER TABLE active_context_members DROP CONSTRAINT ck_active_context_member_object_type;
        ALTER TABLE active_context_members ADD CONSTRAINT ck_active_context_member_object_type CHECK(object_type IN (
            'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit',
            'document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job',
            'mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence',
            'intelligence_interpretation','intelligence_idea','project','project_note','memory_thread','life_intent',
            'life_intent_blocker','life_problem','life_problem_approach','life_solution_component',
            'life_problem_assumption','life_problem_decision','life_approach_outcome','founder_memory_note',
            'diagnosis_record'
        ));
        ALTER TABLE memory_thread_members DROP CONSTRAINT ck_memory_thread_member_kind;
        ALTER TABLE memory_thread_members ADD CONSTRAINT ck_memory_thread_member_kind CHECK(member_kind IN (
            'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit',
            'document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job',
            'mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence',
            'intelligence_interpretation','intelligence_idea','project','project_note','active_context_set',
            'life_intent','life_intent_blocker','life_problem','life_problem_approach','life_solution_component',
            'life_problem_assumption','life_problem_decision','life_approach_outcome','founder_memory_note',
            'diagnosis_record'
        ));
    """)

    op.execute("""
        CREATE FUNCTION erase_own_diagnosis_children() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE v_owner_id uuid;
        BEGIN
            v_owner_id := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner_id IS NULL THEN
                RAISE EXCEPTION 'erase_own_diagnosis_children requires an authenticated app.current_user_id session context.';
            END IF;
            DELETE FROM public.diagnosis_records WHERE owner_id = v_owner_id;
        END;
        $$;
        REVOKE ALL ON FUNCTION erase_own_diagnosis_children() FROM PUBLIC;
    """)


def downgrade() -> None:
    op.execute("""
        DROP FUNCTION erase_own_diagnosis_children();

        ALTER TABLE memory_thread_members DROP CONSTRAINT ck_memory_thread_member_kind;
        ALTER TABLE memory_thread_members ADD CONSTRAINT ck_memory_thread_member_kind CHECK(member_kind IN (
            'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit',
            'document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job',
            'mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence',
            'intelligence_interpretation','intelligence_idea','project','project_note','active_context_set',
            'life_intent','life_intent_blocker','life_problem','life_problem_approach','life_solution_component',
            'life_problem_assumption','life_problem_decision','life_approach_outcome','founder_memory_note'
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
        ALTER TABLE active_context_sets DROP CONSTRAINT ck_active_context_set_anchor_type;
        ALTER TABLE active_context_sets ADD CONSTRAINT ck_active_context_set_anchor_type CHECK(anchor_type IN (
            'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit',
            'document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job',
            'mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence',
            'intelligence_interpretation','intelligence_idea','project','project_note','memory_thread','life_intent',
            'life_intent_blocker','life_problem','life_problem_approach','life_solution_component',
            'life_problem_assumption','life_problem_decision','life_approach_outcome','founder_memory_note','explicit_topic'
        ));

        DROP TABLE diagnosis_records;
    """)
