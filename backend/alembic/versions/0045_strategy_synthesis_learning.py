"""Deterministic strategy synthesis recipes, lineage, conflicts, and materialization."""

from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

TABLES = (
    "strategy_synthesis_cases",
    "strategy_synthesis_inputs",
    "strategy_synthesis_components",
    "strategy_synthesis_conflicts",
    "strategy_synthesis_materializations",
    "strategy_synthesis_evaluation_links",
    "strategy_synthesis_lesson_links",
    "strategy_synthesis_events",
)


def upgrade() -> None:
    op.execute("""
    CREATE TABLE strategy_synthesis_cases (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      case_key varchar(128) NOT NULL, revision integer NOT NULL DEFAULT 1, predecessor_case_id uuid,
      predecessor_strategy_id uuid, candidate_strategy_key varchar(128) NOT NULL, candidate_strategy_version integer NOT NULL,
      task_id uuid, problem_id uuid, domain varchar(128), risk_level varchar(24) NOT NULL DEFAULT 'unknown',
      purpose text NOT NULL, improvement_dimensions jsonb NOT NULL DEFAULT '[]', quality_invariants jsonb NOT NULL DEFAULT '[]',
      applicability jsonb NOT NULL DEFAULT '{}', expected_benefits jsonb NOT NULL DEFAULT '[]', expected_tradeoffs jsonb NOT NULL DEFAULT '[]',
      status varchar(24) NOT NULL DEFAULT 'draft', next_component_sequence integer NOT NULL DEFAULT 1,
      provenance jsonb NOT NULL DEFAULT '{}', idempotency_key varchar(128) NOT NULL,
      created_at timestamp NOT NULL DEFAULT now(), updated_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_synthesis_cases_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_synthesis_case_revision UNIQUE(owner_id,case_key,revision),
      CONSTRAINT uq_synthesis_candidate_version UNIQUE(owner_id,candidate_strategy_key,candidate_strategy_version),
      CONSTRAINT uq_synthesis_case_idem UNIQUE(owner_id,idempotency_key),
      CONSTRAINT fk_synthesis_case_predecessor FOREIGN KEY(predecessor_case_id,owner_id) REFERENCES strategy_synthesis_cases(id,owner_id),
      CONSTRAINT fk_synthesis_case_strategy FOREIGN KEY(predecessor_strategy_id,owner_id) REFERENCES work_strategies(id,owner_id),
      CONSTRAINT fk_synthesis_case_task FOREIGN KEY(task_id,owner_id) REFERENCES mainai_tasks(id,owner_id),
      CONSTRAINT fk_synthesis_case_problem FOREIGN KEY(problem_id,owner_id) REFERENCES life_problems(id,owner_id),
      CONSTRAINT ck_synthesis_case_revision CHECK(revision>0 AND candidate_strategy_version>0 AND next_component_sequence>0),
      CONSTRAINT ck_synthesis_case_no_self CHECK(predecessor_case_id IS NULL OR predecessor_case_id<>id),
      CONSTRAINT ck_synthesis_case_risk CHECK(risk_level IN ('low','medium','high','critical','unknown')),
      CONSTRAINT ck_synthesis_case_status CHECK(status IN ('draft','ready','assembled','invalidated','cancelled','completed')),
      CONSTRAINT ck_synthesis_case_text CHECK(length(btrim(case_key))>0 AND length(btrim(candidate_strategy_key))>0 AND length(btrim(purpose))>0),
      CONSTRAINT ck_synthesis_case_json CHECK(jsonb_typeof(improvement_dimensions)='array' AND jsonb_typeof(quality_invariants)='array' AND jsonb_typeof(applicability)='object' AND jsonb_typeof(expected_benefits)='array' AND jsonb_typeof(expected_tradeoffs)='array' AND jsonb_typeof(provenance)='object')
    );
    CREATE TABLE strategy_synthesis_inputs (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      case_id uuid NOT NULL, source_kind varchar(48) NOT NULL, work_strategy_id uuid, strategy_execution_id uuid,
      intelligence_idea_id uuid, intelligence_evidence_id uuid, intelligence_execution_id uuid, comparison_id uuid,
      experiment_id uuid, engineering_lesson_id uuid REFERENCES engineering_lessons(id), solution_component_id uuid,
      assumption_id uuid, specialist_contribution_id uuid, learning_observation_id uuid, stopping_decision_id uuid,
      disposition varchar(24) NOT NULL DEFAULT 'unknown', reason text NOT NULL, basis varchar(24) NOT NULL DEFAULT 'unknown',
      supporting_evidence_id uuid, idempotency_key varchar(128) NOT NULL, created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_synthesis_inputs_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_synthesis_inputs_id_case_owner UNIQUE(id,case_id,owner_id),
      CONSTRAINT uq_synthesis_input_idem UNIQUE(owner_id,case_id,idempotency_key),
      CONSTRAINT fk_synthesis_input_case FOREIGN KEY(case_id,owner_id) REFERENCES strategy_synthesis_cases(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_synthesis_input_strategy FOREIGN KEY(work_strategy_id,owner_id) REFERENCES work_strategies(id,owner_id),
      CONSTRAINT fk_synthesis_input_binding FOREIGN KEY(strategy_execution_id,owner_id) REFERENCES work_strategy_executions(id,owner_id),
      CONSTRAINT fk_synthesis_input_idea FOREIGN KEY(intelligence_idea_id,owner_id) REFERENCES intelligence_ideas(id,owner_id),
      CONSTRAINT fk_synthesis_input_evidence FOREIGN KEY(intelligence_evidence_id,owner_id) REFERENCES intelligence_evidence(id,owner_id),
      CONSTRAINT fk_synthesis_input_execution FOREIGN KEY(intelligence_execution_id,owner_id) REFERENCES intelligence_executions(id,owner_id),
      CONSTRAINT fk_synthesis_input_comparison FOREIGN KEY(comparison_id,owner_id) REFERENCES strategy_comparisons(id,owner_id),
      CONSTRAINT fk_synthesis_input_experiment FOREIGN KEY(experiment_id,owner_id) REFERENCES strategy_experiments(id,owner_id),
      CONSTRAINT fk_synthesis_input_component FOREIGN KEY(solution_component_id,owner_id) REFERENCES life_solution_components(id,owner_id),
      CONSTRAINT fk_synthesis_input_assumption FOREIGN KEY(assumption_id,owner_id) REFERENCES life_problem_assumptions(id,owner_id),
      CONSTRAINT fk_synthesis_input_specialist FOREIGN KEY(specialist_contribution_id,owner_id) REFERENCES work_specialist_contributions(id,owner_id),
      CONSTRAINT fk_synthesis_input_learning FOREIGN KEY(learning_observation_id,owner_id) REFERENCES strategy_learning_observations(id,owner_id),
      CONSTRAINT fk_synthesis_input_stopping FOREIGN KEY(stopping_decision_id,owner_id) REFERENCES work_stopping_decisions(id,owner_id),
      CONSTRAINT fk_synthesis_input_support FOREIGN KEY(supporting_evidence_id,owner_id) REFERENCES intelligence_evidence(id,owner_id),
      CONSTRAINT ck_synthesis_input_kind CHECK(source_kind IN ('work_strategy','strategy_execution','intelligence_idea','intelligence_evidence','intelligence_execution','strategy_comparison','strategy_experiment','engineering_lesson','solution_component','assumption','specialist_contribution','learning_observation','stopping_decision')),
      CONSTRAINT ck_synthesis_input_one_source CHECK(num_nonnulls(work_strategy_id,strategy_execution_id,intelligence_idea_id,intelligence_evidence_id,intelligence_execution_id,comparison_id,experiment_id,engineering_lesson_id,solution_component_id,assumption_id,specialist_contribution_id,learning_observation_id,stopping_decision_id)=1),
      CONSTRAINT ck_synthesis_input_kind_matches CHECK(
        (source_kind='work_strategy' AND work_strategy_id IS NOT NULL) OR
        (source_kind='strategy_execution' AND strategy_execution_id IS NOT NULL) OR
        (source_kind='intelligence_idea' AND intelligence_idea_id IS NOT NULL) OR
        (source_kind='intelligence_evidence' AND intelligence_evidence_id IS NOT NULL) OR
        (source_kind='intelligence_execution' AND intelligence_execution_id IS NOT NULL) OR
        (source_kind='strategy_comparison' AND comparison_id IS NOT NULL) OR
        (source_kind='strategy_experiment' AND experiment_id IS NOT NULL) OR
        (source_kind='engineering_lesson' AND engineering_lesson_id IS NOT NULL) OR
        (source_kind='solution_component' AND solution_component_id IS NOT NULL) OR
        (source_kind='assumption' AND assumption_id IS NOT NULL) OR
        (source_kind='specialist_contribution' AND specialist_contribution_id IS NOT NULL) OR
        (source_kind='learning_observation' AND learning_observation_id IS NOT NULL) OR
        (source_kind='stopping_decision' AND stopping_decision_id IS NOT NULL)),
      CONSTRAINT ck_synthesis_input_disposition CHECK(disposition IN ('included','excluded','modified','alternative','deferred','unknown')),
      CONSTRAINT ck_synthesis_input_basis CHECK(basis IN ('manual','deterministic','inferred','unknown')),
      CONSTRAINT ck_synthesis_input_reason CHECK(length(btrim(reason))>0)
    );
    CREATE TABLE strategy_synthesis_components (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      case_id uuid NOT NULL, input_id uuid NOT NULL, sequence_number integer NOT NULL, component_kind varchar(48) NOT NULL DEFAULT 'unknown',
      description text NOT NULL, modification_intent text, disposition varchar(24) NOT NULL DEFAULT 'unknown',
      applicability jsonb NOT NULL DEFAULT '{}', method_payload jsonb NOT NULL DEFAULT '{}', assumption_id uuid, evidence_id uuid,
      reason text NOT NULL, basis varchar(24) NOT NULL DEFAULT 'unknown', idempotency_key varchar(128) NOT NULL,
      created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_synthesis_components_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_synthesis_components_id_case_owner UNIQUE(id,case_id,owner_id),
      CONSTRAINT uq_synthesis_component_sequence UNIQUE(owner_id,case_id,sequence_number) DEFERRABLE INITIALLY DEFERRED,
      CONSTRAINT uq_synthesis_component_idem UNIQUE(owner_id,case_id,idempotency_key),
      CONSTRAINT fk_synthesis_component_case FOREIGN KEY(case_id,owner_id) REFERENCES strategy_synthesis_cases(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_synthesis_component_input FOREIGN KEY(input_id,case_id,owner_id) REFERENCES strategy_synthesis_inputs(id,case_id,owner_id),
      CONSTRAINT fk_synthesis_component_assumption FOREIGN KEY(assumption_id,owner_id) REFERENCES life_problem_assumptions(id,owner_id),
      CONSTRAINT fk_synthesis_component_evidence FOREIGN KEY(evidence_id,owner_id) REFERENCES intelligence_evidence(id,owner_id),
      CONSTRAINT ck_synthesis_component_sequence CHECK(sequence_number>0),
      CONSTRAINT ck_synthesis_component_kind CHECK(component_kind IN ('repository_search','file_selection','code_navigation','diagnostic_step','implementation_sequence','test_sequence','verification_obligation','stopping_condition','escalation_condition','retry_strategy','concurrency_check','migration_review','security_review','simplification','specialist_handoff','other','unknown')),
      CONSTRAINT ck_synthesis_component_disposition CHECK(disposition IN ('included','excluded','modified','alternative','deferred','unknown')),
      CONSTRAINT ck_synthesis_component_basis CHECK(basis IN ('manual','deterministic','inferred','unknown')),
      CONSTRAINT ck_synthesis_component_text CHECK(length(btrim(description))>0 AND length(btrim(reason))>0),
      CONSTRAINT ck_synthesis_component_json CHECK(jsonb_typeof(applicability)='object' AND jsonb_typeof(method_payload)='object')
    );
    CREATE TABLE strategy_synthesis_conflicts (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      case_id uuid NOT NULL, left_component_id uuid, right_component_id uuid, assumption_id uuid,
      severity varchar(16) NOT NULL DEFAULT 'hard', status varchar(24) NOT NULL DEFAULT 'unresolved', description text NOT NULL,
      resolution_reason text, evidence_id uuid, idempotency_key varchar(128) NOT NULL,
      created_at timestamp NOT NULL DEFAULT now(), updated_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_synthesis_conflicts_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_synthesis_conflict_idem UNIQUE(owner_id,case_id,idempotency_key),
      CONSTRAINT fk_synthesis_conflict_case FOREIGN KEY(case_id,owner_id) REFERENCES strategy_synthesis_cases(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_synthesis_conflict_left FOREIGN KEY(left_component_id,case_id,owner_id) REFERENCES strategy_synthesis_components(id,case_id,owner_id),
      CONSTRAINT fk_synthesis_conflict_right FOREIGN KEY(right_component_id,case_id,owner_id) REFERENCES strategy_synthesis_components(id,case_id,owner_id),
      CONSTRAINT fk_synthesis_conflict_assumption FOREIGN KEY(assumption_id,owner_id) REFERENCES life_problem_assumptions(id,owner_id),
      CONSTRAINT fk_synthesis_conflict_evidence FOREIGN KEY(evidence_id,owner_id) REFERENCES intelligence_evidence(id,owner_id),
      CONSTRAINT ck_synthesis_conflict_subject CHECK(num_nonnulls(left_component_id,right_component_id,assumption_id)>=1),
      CONSTRAINT ck_synthesis_conflict_distinct CHECK(left_component_id IS NULL OR right_component_id IS NULL OR left_component_id<>right_component_id),
      CONSTRAINT ck_synthesis_conflict_severity CHECK(severity IN ('hard','soft','unknown')),
      CONSTRAINT ck_synthesis_conflict_status CHECK(status IN ('unresolved','resolved','accepted_risk','invalidated')),
      CONSTRAINT ck_synthesis_conflict_text CHECK(length(btrim(description))>0 AND (status='unresolved' OR resolution_reason IS NOT NULL))
    );
    CREATE TABLE strategy_synthesis_materializations (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      case_id uuid NOT NULL, strategy_id uuid NOT NULL, recipe_fingerprint varchar(64) NOT NULL,
      idempotency_key varchar(128) NOT NULL, created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_synthesis_materializations_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_synthesis_materialization_case UNIQUE(owner_id,case_id),
      CONSTRAINT uq_synthesis_materialization_strategy UNIQUE(owner_id,strategy_id),
      CONSTRAINT uq_synthesis_materialization_idem UNIQUE(owner_id,idempotency_key),
      CONSTRAINT fk_synthesis_materialization_case FOREIGN KEY(case_id,owner_id) REFERENCES strategy_synthesis_cases(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_synthesis_materialization_strategy FOREIGN KEY(strategy_id,owner_id) REFERENCES work_strategies(id,owner_id),
      CONSTRAINT ck_synthesis_materialization_fingerprint CHECK(recipe_fingerprint ~ '^[0-9a-f]{64}$')
    );
    CREATE TABLE strategy_synthesis_evaluation_links (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      materialization_id uuid NOT NULL, experiment_id uuid, comparison_id uuid, promotion_candidate_id uuid,
      relation varchar(32) NOT NULL, idempotency_key varchar(128) NOT NULL, created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_synthesis_evaluation_links_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_synthesis_evaluation_link_idem UNIQUE(owner_id,idempotency_key),
      CONSTRAINT fk_synthesis_evaluation_materialization FOREIGN KEY(materialization_id,owner_id) REFERENCES strategy_synthesis_materializations(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_synthesis_evaluation_experiment FOREIGN KEY(experiment_id,owner_id) REFERENCES strategy_experiments(id,owner_id),
      CONSTRAINT fk_synthesis_evaluation_comparison FOREIGN KEY(comparison_id,owner_id) REFERENCES strategy_comparisons(id,owner_id),
      CONSTRAINT fk_synthesis_evaluation_promotion FOREIGN KEY(promotion_candidate_id,owner_id) REFERENCES strategy_promotion_candidates(id,owner_id),
      CONSTRAINT ck_synthesis_evaluation_one_target CHECK(num_nonnulls(experiment_id,comparison_id,promotion_candidate_id)=1),
      CONSTRAINT ck_synthesis_evaluation_relation CHECK(relation IN ('experimented_by','evaluated_by','promotion_considered_by'))
    );
    CREATE TABLE strategy_synthesis_lesson_links (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      case_id uuid NOT NULL, component_id uuid, engineering_lesson_id uuid NOT NULL REFERENCES engineering_lessons(id),
      evidence_id uuid, relation varchar(32) NOT NULL, idempotency_key varchar(128) NOT NULL, created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_synthesis_lesson_links_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_synthesis_lesson_link_idem UNIQUE(owner_id,idempotency_key),
      CONSTRAINT fk_synthesis_lesson_case FOREIGN KEY(case_id,owner_id) REFERENCES strategy_synthesis_cases(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_synthesis_lesson_component FOREIGN KEY(component_id,case_id,owner_id) REFERENCES strategy_synthesis_components(id,case_id,owner_id),
      CONSTRAINT fk_synthesis_lesson_evidence FOREIGN KEY(evidence_id,owner_id) REFERENCES intelligence_evidence(id,owner_id),
      CONSTRAINT ck_synthesis_lesson_relation CHECK(relation IN ('failure_root_cause','interaction_failure','revision_rule','retained_strength','counterexample','unknown'))
    );
    CREATE TABLE strategy_synthesis_events (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      case_id uuid NOT NULL, conflict_id uuid, event_type varchar(32) NOT NULL, from_state varchar(24), to_state varchar(24),
      detail jsonb NOT NULL DEFAULT '{}', idempotency_key varchar(128) NOT NULL, created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_synthesis_events_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_synthesis_event_idem UNIQUE(owner_id,idempotency_key),
      CONSTRAINT fk_synthesis_event_case FOREIGN KEY(case_id,owner_id) REFERENCES strategy_synthesis_cases(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_synthesis_event_conflict FOREIGN KEY(conflict_id,owner_id) REFERENCES strategy_synthesis_conflicts(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT ck_synthesis_event_type CHECK(event_type IN ('created','input_added','component_added','recipe_reordered','conflict_recorded','conflict_state_changed','state_changed','materialized','evaluation_linked','lesson_linked')),
      CONSTRAINT ck_synthesis_event_detail CHECK(jsonb_typeof(detail)='object')
    );
    """)
    for column in (
        "work_strategy_id",
        "strategy_execution_id",
        "intelligence_idea_id",
        "intelligence_evidence_id",
        "intelligence_execution_id",
        "comparison_id",
        "experiment_id",
        "engineering_lesson_id",
        "solution_component_id",
        "assumption_id",
        "specialist_contribution_id",
        "learning_observation_id",
        "stopping_decision_id",
    ):
        op.execute(
            f"CREATE UNIQUE INDEX uq_synthesis_input_{column} ON strategy_synthesis_inputs(owner_id,case_id,{column}) WHERE {column} IS NOT NULL"
        )
    op.execute("""
    CREATE INDEX ix_synthesis_case_predecessor ON strategy_synthesis_cases(owner_id,predecessor_case_id) WHERE predecessor_case_id IS NOT NULL;
    CREATE INDEX ix_synthesis_case_strategy ON strategy_synthesis_cases(owner_id,predecessor_strategy_id) WHERE predecessor_strategy_id IS NOT NULL;
    CREATE INDEX ix_synthesis_case_task ON strategy_synthesis_cases(owner_id,task_id) WHERE task_id IS NOT NULL;
    CREATE INDEX ix_synthesis_case_problem ON strategy_synthesis_cases(owner_id,problem_id) WHERE problem_id IS NOT NULL;
    CREATE INDEX ix_synthesis_component_input ON strategy_synthesis_components(owner_id,input_id);
    CREATE INDEX ix_synthesis_conflict_open ON strategy_synthesis_conflicts(owner_id,case_id,severity) WHERE status='unresolved';
    CREATE INDEX ix_synthesis_evaluation_experiment ON strategy_synthesis_evaluation_links(owner_id,experiment_id) WHERE experiment_id IS NOT NULL;
    CREATE INDEX ix_synthesis_evaluation_comparison ON strategy_synthesis_evaluation_links(owner_id,comparison_id) WHERE comparison_id IS NOT NULL;
    CREATE INDEX ix_synthesis_evaluation_promotion ON strategy_synthesis_evaluation_links(owner_id,promotion_candidate_id) WHERE promotion_candidate_id IS NOT NULL;
    CREATE INDEX ix_synthesis_lesson_component ON strategy_synthesis_lesson_links(owner_id,component_id) WHERE component_id IS NOT NULL;
    """)
    for table in TABLES:
        op.execute(
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY; ALTER TABLE {table} FORCE ROW LEVEL SECURITY"
        )
        op.execute(
            f"CREATE POLICY {table}_isolation ON {table} USING (owner_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid) WITH CHECK (owner_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid)"
        )
    op.execute("""
    CREATE FUNCTION strategy_synthesis_deny_mutation() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
    BEGIN
      IF TG_OP='DELETE' AND current_setting('app.mainai_execution_erasure_in_progress',true)='on' THEN RETURN OLD; END IF;
      RAISE EXCEPTION 'strategy synthesis evidence is append-only';
    END $$;
    REVOKE ALL ON FUNCTION strategy_synthesis_deny_mutation() FROM PUBLIC;

    CREATE FUNCTION strategy_synthesis_case_update_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
    DECLARE open_hard integer; usable_components integer;
    BEGIN
      IF NEW.id<>OLD.id OR NEW.owner_id<>OLD.owner_id OR NEW.case_key<>OLD.case_key OR NEW.revision<>OLD.revision
         OR NEW.predecessor_case_id IS DISTINCT FROM OLD.predecessor_case_id OR NEW.predecessor_strategy_id IS DISTINCT FROM OLD.predecessor_strategy_id
         OR NEW.candidate_strategy_key<>OLD.candidate_strategy_key OR NEW.candidate_strategy_version<>OLD.candidate_strategy_version
         OR NEW.task_id IS DISTINCT FROM OLD.task_id OR NEW.problem_id IS DISTINCT FROM OLD.problem_id OR NEW.domain IS DISTINCT FROM OLD.domain
         OR NEW.risk_level<>OLD.risk_level OR NEW.purpose<>OLD.purpose OR NEW.improvement_dimensions<>OLD.improvement_dimensions
         OR NEW.quality_invariants<>OLD.quality_invariants OR NEW.applicability<>OLD.applicability
         OR NEW.expected_benefits<>OLD.expected_benefits OR NEW.expected_tradeoffs<>OLD.expected_tradeoffs
         OR NEW.provenance<>OLD.provenance OR NEW.idempotency_key<>OLD.idempotency_key OR NEW.created_at<>OLD.created_at
      THEN RAISE EXCEPTION 'strategy synthesis case definition is immutable'; END IF;
      IF NEW.next_component_sequence<>OLD.next_component_sequence AND NEW.next_component_sequence<>OLD.next_component_sequence+1
      THEN RAISE EXCEPTION 'synthesis component sequence counter may advance by one only'; END IF;
      IF NEW.status<>OLD.status AND NOT (
        (OLD.status='draft' AND NEW.status IN ('ready','invalidated','cancelled')) OR
        (OLD.status='ready' AND NEW.status IN ('assembled','invalidated','cancelled')) OR
        (OLD.status='assembled' AND NEW.status IN ('completed','invalidated'))
      ) THEN RAISE EXCEPTION 'invalid strategy synthesis transition'; END IF;
      IF NEW.status='ready' AND OLD.status<>'ready' THEN
        SELECT count(*) INTO open_hard FROM public.strategy_synthesis_conflicts c
          WHERE c.owner_id=NEW.owner_id AND c.case_id=NEW.id AND c.severity='hard' AND c.status='unresolved';
        SELECT count(*) INTO usable_components FROM public.strategy_synthesis_components c
          WHERE c.owner_id=NEW.owner_id AND c.case_id=NEW.id AND c.disposition IN ('included','modified');
        IF open_hard>0 THEN RAISE EXCEPTION 'unresolved hard synthesis conflict'; END IF;
        IF usable_components=0 THEN RAISE EXCEPTION 'synthesis requires an included or modified component'; END IF;
        IF jsonb_array_length(NEW.quality_invariants)=0 THEN RAISE EXCEPTION 'synthesis requires explicit quality invariants'; END IF;
      END IF;
      RETURN NEW;
    END $$;
    REVOKE ALL ON FUNCTION strategy_synthesis_case_update_guard() FROM PUBLIC;

    CREATE FUNCTION strategy_synthesis_component_update_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
    BEGIN
      IF NEW.id<>OLD.id OR NEW.owner_id<>OLD.owner_id OR NEW.case_id<>OLD.case_id OR NEW.input_id<>OLD.input_id
         OR NEW.component_kind<>OLD.component_kind OR NEW.description<>OLD.description OR NEW.modification_intent IS DISTINCT FROM OLD.modification_intent
         OR NEW.disposition<>OLD.disposition OR NEW.applicability<>OLD.applicability OR NEW.method_payload<>OLD.method_payload
         OR NEW.assumption_id IS DISTINCT FROM OLD.assumption_id OR NEW.evidence_id IS DISTINCT FROM OLD.evidence_id
         OR NEW.reason<>OLD.reason OR NEW.basis<>OLD.basis OR NEW.idempotency_key<>OLD.idempotency_key OR NEW.created_at<>OLD.created_at
      THEN RAISE EXCEPTION 'synthesis component content is immutable'; END IF;
      RETURN NEW;
    END $$;
    REVOKE ALL ON FUNCTION strategy_synthesis_component_update_guard() FROM PUBLIC;

    CREATE FUNCTION strategy_synthesis_conflict_update_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
    BEGIN
      IF NEW.id<>OLD.id OR NEW.owner_id<>OLD.owner_id OR NEW.case_id<>OLD.case_id
         OR NEW.left_component_id IS DISTINCT FROM OLD.left_component_id OR NEW.right_component_id IS DISTINCT FROM OLD.right_component_id
         OR NEW.assumption_id IS DISTINCT FROM OLD.assumption_id OR NEW.severity<>OLD.severity OR NEW.description<>OLD.description
         OR NEW.evidence_id IS DISTINCT FROM OLD.evidence_id OR NEW.idempotency_key<>OLD.idempotency_key OR NEW.created_at<>OLD.created_at
      THEN RAISE EXCEPTION 'synthesis conflict identity is immutable'; END IF;
      IF NEW.status<>OLD.status AND NOT (OLD.status='unresolved' AND NEW.status IN ('resolved','accepted_risk','invalidated'))
      THEN RAISE EXCEPTION 'invalid synthesis conflict transition'; END IF;
      RETURN NEW;
    END $$;
    REVOKE ALL ON FUNCTION strategy_synthesis_conflict_update_guard() FROM PUBLIC;

    CREATE TRIGGER trg_synthesis_case_update BEFORE UPDATE ON strategy_synthesis_cases FOR EACH ROW EXECUTE FUNCTION strategy_synthesis_case_update_guard();
    CREATE TRIGGER trg_synthesis_component_update BEFORE UPDATE ON strategy_synthesis_components FOR EACH ROW EXECUTE FUNCTION strategy_synthesis_component_update_guard();
    CREATE TRIGGER trg_synthesis_conflict_update BEFORE UPDATE ON strategy_synthesis_conflicts FOR EACH ROW EXECUTE FUNCTION strategy_synthesis_conflict_update_guard();
    """)
    for table in (
        "strategy_synthesis_inputs",
        "strategy_synthesis_materializations",
        "strategy_synthesis_evaluation_links",
        "strategy_synthesis_lesson_links",
        "strategy_synthesis_events",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION strategy_synthesis_deny_mutation()"
        )
    op.execute("""
    CREATE OR REPLACE FUNCTION erase_own_mainai_execution_children() RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE v_owner_id uuid;
    BEGIN
      v_owner_id:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
      IF v_owner_id IS NULL THEN RAISE EXCEPTION 'erase_own_mainai_execution_children requires an authenticated app.current_user_id session context.'; END IF;
      PERFORM set_config('app.mainai_execution_erasure_in_progress','on',true);
      DELETE FROM public.strategy_synthesis_cases WHERE owner_id=v_owner_id;
      DELETE FROM public.strategy_evaluation_events WHERE owner_id=v_owner_id;
      DELETE FROM public.strategy_learning_links WHERE owner_id=v_owner_id;
      DELETE FROM public.strategy_learning_observations WHERE owner_id=v_owner_id;
      DELETE FROM public.strategy_promotion_comparisons WHERE owner_id=v_owner_id;
      DELETE FROM public.strategy_experiment_comparisons WHERE owner_id=v_owner_id;
      DELETE FROM public.strategy_efficiency_deltas WHERE owner_id=v_owner_id;
      DELETE FROM public.strategy_quality_assessments WHERE owner_id=v_owner_id;
      DELETE FROM public.strategy_comparability_assessments WHERE owner_id=v_owner_id;
      DELETE FROM public.strategy_promotion_candidates WHERE owner_id=v_owner_id;
      DELETE FROM public.strategy_experiments WHERE owner_id=v_owner_id;
      DELETE FROM public.strategy_comparisons WHERE owner_id=v_owner_id;
      DELETE FROM public.work_strategy_lesson_links WHERE owner_id=v_owner_id;
      DELETE FROM public.work_specialist_contributions WHERE owner_id=v_owner_id;
      DELETE FROM public.work_stopping_decisions WHERE owner_id=v_owner_id;
      DELETE FROM public.work_verification_observations WHERE owner_id=v_owner_id;
      DELETE FROM public.work_verification_obligations WHERE owner_id=v_owner_id;
      DELETE FROM public.work_strategy_findings WHERE owner_id=v_owner_id;
      DELETE FROM public.work_efficiency_observations WHERE owner_id=v_owner_id;
      DELETE FROM public.work_trace_events WHERE owner_id=v_owner_id;
      DELETE FROM public.work_strategy_executions WHERE owner_id=v_owner_id;
      DELETE FROM public.work_strategies WHERE owner_id=v_owner_id;
      DELETE FROM public.intelligence_idea_lessons WHERE owner_id=v_owner_id;
      DELETE FROM public.intelligence_idea_links WHERE owner_id=v_owner_id;
      DELETE FROM public.intelligence_ideas WHERE owner_id=v_owner_id;
      DELETE FROM public.intelligence_interpretations WHERE owner_id=v_owner_id;
      DELETE FROM public.intelligence_evidence WHERE owner_id=v_owner_id;
      DELETE FROM public.intelligence_executions WHERE owner_id=v_owner_id;
      DELETE FROM public.mainai_checkpoints WHERE owner_id=v_owner_id;
      DELETE FROM public.mainai_task_events WHERE owner_id=v_owner_id;
      DELETE FROM public.mainai_recovery_events WHERE owner_id=v_owner_id;
      DELETE FROM public.mainai_recovery_records WHERE owner_id=v_owner_id;
      DELETE FROM public.mainai_task_worktrees WHERE owner_id=v_owner_id;
    END $$;
    REVOKE ALL ON FUNCTION erase_own_mainai_execution_children() FROM PUBLIC;
    """)


def downgrade() -> None:
    op.execute("""
    CREATE OR REPLACE FUNCTION erase_own_mainai_execution_children() RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE v_owner_id uuid;
    BEGIN
      v_owner_id:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
      IF v_owner_id IS NULL THEN RAISE EXCEPTION 'erase_own_mainai_execution_children requires an authenticated app.current_user_id session context.'; END IF;
      PERFORM set_config('app.mainai_execution_erasure_in_progress','on',true);
      DELETE FROM public.intelligence_idea_lessons WHERE owner_id=v_owner_id;
      DELETE FROM public.intelligence_idea_links WHERE owner_id=v_owner_id;
      DELETE FROM public.intelligence_ideas WHERE owner_id=v_owner_id;
      DELETE FROM public.intelligence_interpretations WHERE owner_id=v_owner_id;
      DELETE FROM public.intelligence_evidence WHERE owner_id=v_owner_id;
      DELETE FROM public.intelligence_executions WHERE owner_id=v_owner_id;
      DELETE FROM public.mainai_checkpoints WHERE owner_id=v_owner_id;
      DELETE FROM public.mainai_task_events WHERE owner_id=v_owner_id;
      DELETE FROM public.mainai_recovery_events WHERE owner_id=v_owner_id;
      DELETE FROM public.mainai_recovery_records WHERE owner_id=v_owner_id;
      DELETE FROM public.mainai_task_worktrees WHERE owner_id=v_owner_id;
    END $$;
    REVOKE ALL ON FUNCTION erase_own_mainai_execution_children() FROM PUBLIC;
    """)
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION strategy_synthesis_conflict_update_guard()")
    op.execute("DROP FUNCTION strategy_synthesis_component_update_guard()")
    op.execute("DROP FUNCTION strategy_synthesis_case_update_guard()")
    op.execute("DROP FUNCTION strategy_synthesis_deny_mutation()")
