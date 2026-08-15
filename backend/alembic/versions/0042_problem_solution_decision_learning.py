"""Life problem/solution/decision/lesson intelligence foundation."""

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE life_problems (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      title varchar(256) NOT NULL, description text NOT NULL, status varchar(24) NOT NULL DEFAULT 'unknown',
      classification_basis varchar(24) NOT NULL DEFAULT 'unknown', authority varchar(40) NOT NULL DEFAULT 'unknown', provenance jsonb NOT NULL DEFAULT '{}',
      memory_thread_id uuid, life_intent_id uuid, mainai_task_id uuid, mainai_job_id uuid, project_id uuid REFERENCES projects(id) ON DELETE SET NULL, evidence_id uuid,
      supersedes_problem_id uuid, idempotency_key varchar(128) NOT NULL, created_at timestamp NOT NULL DEFAULT now(),
      updated_at timestamp NOT NULL DEFAULT now(), resolved_at timestamp,
      CONSTRAINT uq_life_problems_id_owner UNIQUE(id,owner_id), CONSTRAINT uq_life_problems_idem UNIQUE(owner_id,idempotency_key),
      CONSTRAINT fk_life_problem_thread FOREIGN KEY(memory_thread_id,owner_id) REFERENCES memory_threads(id,owner_id),
      CONSTRAINT fk_life_problem_intent FOREIGN KEY(life_intent_id,owner_id) REFERENCES life_intents(id,owner_id),
      CONSTRAINT fk_life_problem_task FOREIGN KEY(mainai_task_id,owner_id) REFERENCES mainai_tasks(id,owner_id),
      CONSTRAINT fk_life_problem_job FOREIGN KEY(mainai_job_id,owner_id) REFERENCES mainai_jobs(id,owner_id),
      CONSTRAINT fk_life_problem_evidence FOREIGN KEY(evidence_id,owner_id) REFERENCES intelligence_evidence(id,owner_id),
      CONSTRAINT fk_life_problem_supersedes FOREIGN KEY(supersedes_problem_id,owner_id) REFERENCES life_problems(id,owner_id),
      CONSTRAINT ck_life_problem_text CHECK(length(btrim(title))>0 AND length(btrim(description))>0),
      CONSTRAINT ck_life_problem_status CHECK(status IN ('open','investigating','blocked','resolved','partially_resolved','invalidated','superseded','unknown')),
      CONSTRAINT ck_life_problem_basis CHECK(classification_basis IN ('manual','deterministic','imported','inferred','ai_interpretation','unknown')),
      CONSTRAINT ck_life_problem_authority CHECK(authority IN ('founder','repeated_founder_preference','deterministic_source','inferred_pattern','ai_interpretation','unknown')),
      CONSTRAINT ck_life_problem_provenance CHECK(jsonb_typeof(provenance)='object'), CONSTRAINT ck_life_problem_no_self CHECK(supersedes_problem_id IS NULL OR supersedes_problem_id<>id)
    );
    CREATE TABLE life_problem_approaches (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE, problem_id uuid NOT NULL,
      description text NOT NULL, status varchar(32) NOT NULL DEFAULT 'unknown', basis varchar(24) NOT NULL DEFAULT 'unknown', provenance jsonb NOT NULL DEFAULT '{}',
      execution_id uuid, idea_id uuid, evidence_id uuid, intended_outcome text, rejection_reason text,
      idempotency_key varchar(128) NOT NULL, created_at timestamp NOT NULL DEFAULT now(), updated_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_life_approaches_id_owner UNIQUE(id,owner_id), CONSTRAINT uq_life_approach_idem UNIQUE(owner_id,problem_id,idempotency_key),
      CONSTRAINT fk_life_approach_problem FOREIGN KEY(problem_id,owner_id) REFERENCES life_problems(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_life_approach_execution FOREIGN KEY(execution_id,owner_id) REFERENCES intelligence_executions(id,owner_id),
      CONSTRAINT fk_life_approach_idea FOREIGN KEY(idea_id,owner_id) REFERENCES intelligence_ideas(id,owner_id),
      CONSTRAINT fk_life_approach_evidence FOREIGN KEY(evidence_id,owner_id) REFERENCES intelligence_evidence(id,owner_id),
      CONSTRAINT ck_life_approach_status CHECK(status IN ('proposed','tested','implemented','rejected','failed','partially_successful','successful','superseded','unknown')),
      CONSTRAINT ck_life_approach_basis CHECK(basis IN ('manual','deterministic','imported','inferred','ai_interpretation','unknown')),
      CONSTRAINT ck_life_approach_text CHECK(length(btrim(description))>0), CONSTRAINT ck_life_approach_provenance CHECK(jsonb_typeof(provenance)='object')
    );
    CREATE TABLE life_solution_components (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE, approach_id uuid NOT NULL,
      component_kind varchar(32) NOT NULL DEFAULT 'unknown', description text NOT NULL, basis varchar(24) NOT NULL DEFAULT 'unknown',
      provenance jsonb NOT NULL DEFAULT '{}', intelligence_idea_id uuid, idempotency_key varchar(128) NOT NULL, created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_life_components_id_owner UNIQUE(id,owner_id), CONSTRAINT uq_life_component_idem UNIQUE(owner_id,approach_id,idempotency_key),
      CONSTRAINT fk_life_component_approach FOREIGN KEY(approach_id,owner_id) REFERENCES life_problem_approaches(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_life_component_idea FOREIGN KEY(intelligence_idea_id,owner_id) REFERENCES intelligence_ideas(id,owner_id),
      CONSTRAINT ck_life_component_kind CHECK(component_kind IN ('architecture','algorithm','assumption','implementation_technique','security_observation','edge_case','test_strategy','simplification','dependency_choice','state_machine_design','race_condition_fix','ux_idea','operational_procedure','unknown')),
      CONSTRAINT ck_life_component_basis CHECK(basis IN ('manual','deterministic','imported','inferred','ai_interpretation','unknown')),
      CONSTRAINT ck_life_component_text CHECK(length(btrim(description))>0), CONSTRAINT ck_life_component_provenance CHECK(jsonb_typeof(provenance)='object')
    );
    CREATE TABLE life_component_evaluations (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE, component_id uuid NOT NULL,
      evaluation varchar(32) NOT NULL, evaluator_basis varchar(24) NOT NULL DEFAULT 'unknown', reason text NOT NULL,
      evidence_id uuid, evaluator_execution_id uuid, provenance jsonb NOT NULL DEFAULT '{}', idempotency_key varchar(128) NOT NULL, created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_life_evaluations_id_owner UNIQUE(id,owner_id), CONSTRAINT uq_life_evaluation_idem UNIQUE(owner_id,component_id,idempotency_key),
      CONSTRAINT fk_life_evaluation_component FOREIGN KEY(component_id,owner_id) REFERENCES life_solution_components(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_life_evaluation_evidence FOREIGN KEY(evidence_id,owner_id) REFERENCES intelligence_evidence(id,owner_id),
      CONSTRAINT fk_life_evaluation_execution FOREIGN KEY(evaluator_execution_id,owner_id) REFERENCES intelligence_executions(id,owner_id),
      CONSTRAINT ck_life_evaluation_value CHECK(evaluation IN ('verified_useful','useful_with_changes','context_specific','neutral','incorrect','unsafe','disproven','unverified','unknown')),
      CONSTRAINT ck_life_evaluation_basis CHECK(evaluator_basis IN ('manual','deterministic','imported','inferred','ai_interpretation','unknown')),
      CONSTRAINT ck_life_evaluation_reason CHECK(length(btrim(reason))>0), CONSTRAINT ck_life_evaluation_provenance CHECK(jsonb_typeof(provenance)='object')
    );
    CREATE TABLE life_problem_assumptions (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE, problem_id uuid NOT NULL,
      approach_id uuid, component_id uuid, statement text NOT NULL, status varchar(24) NOT NULL DEFAULT 'unknown', basis varchar(24) NOT NULL DEFAULT 'unknown',
      evidence_id uuid, provenance jsonb NOT NULL DEFAULT '{}', idempotency_key varchar(128) NOT NULL, created_at timestamp NOT NULL DEFAULT now(), updated_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_life_assumptions_id_owner UNIQUE(id,owner_id), CONSTRAINT uq_life_assumption_idem UNIQUE(owner_id,problem_id,idempotency_key),
      CONSTRAINT fk_life_assumption_problem FOREIGN KEY(problem_id,owner_id) REFERENCES life_problems(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_life_assumption_approach FOREIGN KEY(approach_id,owner_id) REFERENCES life_problem_approaches(id,owner_id),
      CONSTRAINT fk_life_assumption_component FOREIGN KEY(component_id,owner_id) REFERENCES life_solution_components(id,owner_id),
      CONSTRAINT fk_life_assumption_evidence FOREIGN KEY(evidence_id,owner_id) REFERENCES intelligence_evidence(id,owner_id),
      CONSTRAINT ck_life_assumption_status CHECK(status IN ('untested','supported','contradicted','disproven','superseded','unknown')),
      CONSTRAINT ck_life_assumption_basis CHECK(basis IN ('manual','deterministic','imported','inferred','ai_interpretation','unknown')),
      CONSTRAINT ck_life_assumption_text CHECK(length(btrim(statement))>0), CONSTRAINT ck_life_assumption_provenance CHECK(jsonb_typeof(provenance)='object')
    );
    CREATE TABLE life_problem_decisions (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE, problem_id uuid NOT NULL,
      decision text NOT NULL, status varchar(24) NOT NULL DEFAULT 'unknown', authority varchar(40) NOT NULL DEFAULT 'unknown', basis varchar(24) NOT NULL DEFAULT 'unknown',
      alternatives jsonb NOT NULL DEFAULT '[]', chosen_approach_id uuid, chosen_component_id uuid, supersedes_decision_id uuid,
      provenance jsonb NOT NULL DEFAULT '{}', idempotency_key varchar(128) NOT NULL, decided_at timestamp NOT NULL DEFAULT now(), updated_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_life_decisions_id_owner UNIQUE(id,owner_id), CONSTRAINT uq_life_decision_idem UNIQUE(owner_id,problem_id,idempotency_key),
      CONSTRAINT fk_life_decision_problem FOREIGN KEY(problem_id,owner_id) REFERENCES life_problems(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_life_decision_approach FOREIGN KEY(chosen_approach_id,owner_id) REFERENCES life_problem_approaches(id,owner_id),
      CONSTRAINT fk_life_decision_component FOREIGN KEY(chosen_component_id,owner_id) REFERENCES life_solution_components(id,owner_id),
      CONSTRAINT fk_life_decision_supersedes FOREIGN KEY(supersedes_decision_id,owner_id) REFERENCES life_problem_decisions(id,owner_id),
      CONSTRAINT ck_life_decision_status CHECK(status IN ('active','superseded','reversed','completed','unknown')),
      CONSTRAINT ck_life_decision_authority CHECK(authority IN ('founder','repeated_founder_preference','deterministic_source','inferred_pattern','ai_interpretation','unknown')),
      CONSTRAINT ck_life_decision_basis CHECK(basis IN ('manual','deterministic','imported','inferred','ai_interpretation','unknown')),
      CONSTRAINT ck_life_decision_alternatives CHECK(jsonb_typeof(alternatives)='array'), CONSTRAINT ck_life_decision_provenance CHECK(jsonb_typeof(provenance)='object'),
      CONSTRAINT ck_life_decision_no_self CHECK(supersedes_decision_id IS NULL OR supersedes_decision_id<>id)
    );
    CREATE TABLE life_approach_outcomes (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE, problem_id uuid NOT NULL, approach_id uuid NOT NULL,
      outcome varchar(24) NOT NULL DEFAULT 'unknown', observation text NOT NULL, evidence_id uuid, deterministic boolean NOT NULL DEFAULT false,
      provenance jsonb NOT NULL DEFAULT '{}', idempotency_key varchar(128) NOT NULL, observed_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_life_outcomes_id_owner UNIQUE(id,owner_id), CONSTRAINT uq_life_outcome_idem UNIQUE(owner_id,approach_id,idempotency_key),
      CONSTRAINT fk_life_outcome_problem FOREIGN KEY(problem_id,owner_id) REFERENCES life_problems(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_life_outcome_approach FOREIGN KEY(approach_id,owner_id) REFERENCES life_problem_approaches(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_life_outcome_evidence FOREIGN KEY(evidence_id,owner_id) REFERENCES intelligence_evidence(id,owner_id),
      CONSTRAINT ck_life_outcome_value CHECK(outcome IN ('succeeded','failed','partially_succeeded','inconclusive','cancelled','not_executed','unknown')),
      CONSTRAINT ck_life_outcome_text CHECK(length(btrim(observation))>0), CONSTRAINT ck_life_outcome_provenance CHECK(jsonb_typeof(provenance)='object')
    );
    CREATE TABLE life_problem_lesson_links (
      owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE, problem_id uuid NOT NULL, engineering_lesson_id uuid NOT NULL REFERENCES engineering_lessons(id) ON DELETE CASCADE,
      approach_id uuid, component_id uuid, outcome_id uuid, relation varchar(24) NOT NULL, created_at timestamp NOT NULL DEFAULT now(),
      PRIMARY KEY(owner_id,problem_id,engineering_lesson_id), CONSTRAINT fk_life_lesson_problem FOREIGN KEY(problem_id,owner_id) REFERENCES life_problems(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_life_lesson_approach FOREIGN KEY(approach_id,owner_id) REFERENCES life_problem_approaches(id,owner_id),
      CONSTRAINT fk_life_lesson_component FOREIGN KEY(component_id,owner_id) REFERENCES life_solution_components(id,owner_id),
      CONSTRAINT fk_life_lesson_outcome FOREIGN KEY(outcome_id,owner_id) REFERENCES life_approach_outcomes(id,owner_id),
      CONSTRAINT ck_life_lesson_relation CHECK(relation IN ('root_cause','correction','regression_proof','positive_pattern','reusable_rule','unknown'))
    );
    CREATE TABLE life_solution_selections (
      owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE, problem_id uuid NOT NULL, component_id uuid NOT NULL,
      role varchar(32) NOT NULL DEFAULT 'unknown', basis varchar(24) NOT NULL DEFAULT 'unknown', evidence_id uuid, created_at timestamp NOT NULL DEFAULT now(),
      PRIMARY KEY(owner_id,problem_id,component_id), CONSTRAINT fk_life_selection_problem FOREIGN KEY(problem_id,owner_id) REFERENCES life_problems(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_life_selection_component FOREIGN KEY(component_id,owner_id) REFERENCES life_solution_components(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_life_selection_evidence FOREIGN KEY(evidence_id,owner_id) REFERENCES intelligence_evidence(id,owner_id),
      CONSTRAINT ck_life_selection_basis CHECK(basis IN ('manual','deterministic','imported','inferred','ai_interpretation','unknown'))
    );
    CREATE TABLE life_solution_component_links (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      from_component_id uuid NOT NULL, to_component_id uuid NOT NULL, relation varchar(24) NOT NULL, evidence_id uuid, created_at timestamp NOT NULL DEFAULT now(),
      UNIQUE(owner_id,from_component_id,to_component_id,relation), CONSTRAINT fk_life_component_link_from FOREIGN KEY(from_component_id,owner_id) REFERENCES life_solution_components(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_life_component_link_to FOREIGN KEY(to_component_id,owner_id) REFERENCES life_solution_components(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_life_component_link_evidence FOREIGN KEY(evidence_id,owner_id) REFERENCES intelligence_evidence(id,owner_id),
      CONSTRAINT ck_life_component_link_relation CHECK(relation IN ('conflicts_with','incompatible_with','supports','derived_from')),
      CONSTRAINT ck_life_component_link_self CHECK(from_component_id<>to_component_id)
    );
    CREATE TABLE life_problem_events (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE, problem_id uuid NOT NULL,
      event_type varchar(32) NOT NULL, detail jsonb NOT NULL DEFAULT '{}', actor_type varchar(24) NOT NULL DEFAULT 'unknown', created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT fk_life_problem_event FOREIGN KEY(problem_id,owner_id) REFERENCES life_problems(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT ck_life_problem_event_type CHECK(event_type IN ('problem_created','problem_status_changed','approach_created','approach_status_changed','component_created','assumption_created','assumption_status_changed','decision_created','decision_status_changed','outcome_recorded','lesson_linked','component_selected')),
      CONSTRAINT ck_life_problem_event_actor CHECK(actor_type IN ('founder','system','deterministic_verifier','ai_interpretation','unknown')),
      CONSTRAINT ck_life_problem_event_detail CHECK(jsonb_typeof(detail)='object')
    );
    CREATE INDEX ix_life_problems_status ON life_problems(owner_id,status,updated_at);
    CREATE INDEX ix_life_approaches_problem ON life_problem_approaches(problem_id,status);
    CREATE INDEX ix_life_assumptions_open ON life_problem_assumptions(problem_id,status);
    CREATE INDEX ix_life_decisions_current ON life_problem_decisions(problem_id,status,decided_at);
    CREATE UNIQUE INDEX uq_life_decision_one_active ON life_problem_decisions(owner_id,problem_id) WHERE status='active';
    CREATE INDEX ix_life_problem_events_time ON life_problem_events(problem_id,created_at);
    """)
    tables = (
        "life_problems",
        "life_problem_approaches",
        "life_solution_components",
        "life_component_evaluations",
        "life_problem_assumptions",
        "life_problem_decisions",
        "life_approach_outcomes",
        "life_problem_lesson_links",
        "life_solution_selections",
        "life_solution_component_links",
        "life_problem_events",
    )
    for table in tables:
        op.execute(
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY; ALTER TABLE {table} FORCE ROW LEVEL SECURITY;"
        )
        op.execute(
            f"CREATE POLICY {table}_isolation ON {table} USING (owner_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid) WITH CHECK (owner_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid);"
        )
    op.execute("""
    CREATE FUNCTION life_problem_append_only_deny_update() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN RAISE EXCEPTION 'problem learning evidence is append-only'; END $$;
    REVOKE ALL ON FUNCTION life_problem_append_only_deny_update() FROM PUBLIC;
    CREATE TRIGGER trg_life_component_evaluation_immutable BEFORE UPDATE ON life_component_evaluations FOR EACH ROW EXECUTE FUNCTION life_problem_append_only_deny_update();
    CREATE TRIGGER trg_life_outcome_immutable BEFORE UPDATE ON life_approach_outcomes FOR EACH ROW EXECUTE FUNCTION life_problem_append_only_deny_update();
    CREATE TRIGGER trg_life_lesson_link_immutable BEFORE UPDATE ON life_problem_lesson_links FOR EACH ROW EXECUTE FUNCTION life_problem_append_only_deny_update();
    CREATE TRIGGER trg_life_selection_immutable BEFORE UPDATE ON life_solution_selections FOR EACH ROW EXECUTE FUNCTION life_problem_append_only_deny_update();
    CREATE TRIGGER trg_life_component_link_immutable BEFORE UPDATE ON life_solution_component_links FOR EACH ROW EXECUTE FUNCTION life_problem_append_only_deny_update();
    CREATE TRIGGER trg_life_problem_event_immutable BEFORE UPDATE ON life_problem_events FOR EACH ROW EXECUTE FUNCTION life_problem_append_only_deny_update();
    ALTER TABLE active_context_sets DROP CONSTRAINT ck_active_context_set_anchor_type;
    ALTER TABLE active_context_sets ADD CONSTRAINT ck_active_context_set_anchor_type CHECK(anchor_type IN ('conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit','document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job','mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence','intelligence_interpretation','intelligence_idea','project','project_note','memory_thread','life_intent','life_intent_blocker','life_problem','life_problem_approach','life_solution_component','life_problem_assumption','life_problem_decision','life_approach_outcome','explicit_topic'));
    ALTER TABLE active_context_members DROP CONSTRAINT ck_active_context_member_object_type;
    ALTER TABLE active_context_members ADD CONSTRAINT ck_active_context_member_object_type CHECK(object_type IN ('conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit','document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job','mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence','intelligence_interpretation','intelligence_idea','project','project_note','memory_thread','life_intent','life_intent_blocker','life_problem','life_problem_approach','life_solution_component','life_problem_assumption','life_problem_decision','life_approach_outcome'));
    ALTER TABLE memory_thread_members DROP CONSTRAINT ck_memory_thread_member_kind;
    ALTER TABLE memory_thread_members ADD CONSTRAINT ck_memory_thread_member_kind CHECK(member_kind IN ('conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit','document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job','mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence','intelligence_interpretation','intelligence_idea','project','project_note','active_context_set','life_intent','life_intent_blocker','life_problem','life_problem_approach','life_solution_component','life_problem_assumption','life_problem_decision','life_approach_outcome'));
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE active_context_sets DROP CONSTRAINT ck_active_context_set_anchor_type;
    ALTER TABLE active_context_sets ADD CONSTRAINT ck_active_context_set_anchor_type CHECK(anchor_type IN ('conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit','document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job','mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence','intelligence_interpretation','intelligence_idea','project','project_note','memory_thread','life_intent','life_intent_blocker','explicit_topic'));
    ALTER TABLE active_context_members DROP CONSTRAINT ck_active_context_member_object_type;
    ALTER TABLE active_context_members ADD CONSTRAINT ck_active_context_member_object_type CHECK(object_type IN ('conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit','document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job','mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence','intelligence_interpretation','intelligence_idea','project','project_note','memory_thread','life_intent','life_intent_blocker'));
    ALTER TABLE memory_thread_members DROP CONSTRAINT ck_memory_thread_member_kind;
    ALTER TABLE memory_thread_members ADD CONSTRAINT ck_memory_thread_member_kind CHECK(member_kind IN ('conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit','document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job','mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence','intelligence_interpretation','intelligence_idea','project','project_note','active_context_set','life_intent','life_intent_blocker'));
    DROP TABLE life_problem_events; DROP TABLE life_solution_component_links; DROP TABLE life_solution_selections;
    DROP TABLE life_problem_lesson_links; DROP TABLE life_approach_outcomes; DROP TABLE life_problem_decisions;
    DROP TABLE life_problem_assumptions; DROP TABLE life_component_evaluations; DROP TABLE life_solution_components;
    DROP TABLE life_problem_approaches; DROP TABLE life_problems; DROP FUNCTION life_problem_append_only_deny_update();
    """)
