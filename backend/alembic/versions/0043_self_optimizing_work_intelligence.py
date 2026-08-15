"""Deterministic work-strategy, trace, quality-guardrail and efficiency evidence."""

from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


TABLES = (
    "work_strategies",
    "work_strategy_executions",
    "work_trace_events",
    "work_efficiency_observations",
    "work_strategy_findings",
    "work_verification_obligations",
    "work_verification_observations",
    "work_stopping_decisions",
    "work_specialist_contributions",
    "work_strategy_lesson_links",
)


def upgrade() -> None:
    op.execute("""
    CREATE TABLE work_strategies (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      strategy_key varchar(128) NOT NULL, version integer NOT NULL, work_category varchar(64) NOT NULL DEFAULT 'unknown',
      ordered_phases jsonb NOT NULL DEFAULT '[]', tool_sequence jsonb NOT NULL DEFAULT '[]', methods jsonb NOT NULL DEFAULT '{}',
      environment_assumptions jsonb NOT NULL DEFAULT '{}', classification_basis varchar(24) NOT NULL DEFAULT 'unknown',
      provenance jsonb NOT NULL DEFAULT '{}', predecessor_id uuid, idempotency_key varchar(128) NOT NULL,
      created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_work_strategies_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_work_strategy_version UNIQUE(owner_id,strategy_key,version),
      CONSTRAINT uq_work_strategy_idem UNIQUE(owner_id,idempotency_key),
      CONSTRAINT fk_work_strategy_predecessor FOREIGN KEY(predecessor_id,owner_id) REFERENCES work_strategies(id,owner_id),
      CONSTRAINT ck_work_strategy_version CHECK(version>0),
      CONSTRAINT ck_work_strategy_key CHECK(length(btrim(strategy_key))>0),
      CONSTRAINT ck_work_strategy_basis CHECK(classification_basis IN ('manual','deterministic','inferred','unknown')),
      CONSTRAINT ck_work_strategy_json CHECK(jsonb_typeof(ordered_phases)='array' AND jsonb_typeof(tool_sequence)='array' AND jsonb_typeof(methods)='object' AND jsonb_typeof(environment_assumptions)='object' AND jsonb_typeof(provenance)='object'),
      CONSTRAINT ck_work_strategy_no_self CHECK(predecessor_id IS NULL OR predecessor_id<>id)
    );
    CREATE TABLE work_strategy_executions (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      strategy_id uuid NOT NULL, execution_id uuid NOT NULL, problem_id uuid, approach_id uuid,
      basis varchar(24) NOT NULL DEFAULT 'unknown', provenance jsonb NOT NULL DEFAULT '{}', idempotency_key varchar(128) NOT NULL,
      next_trace_sequence integer NOT NULL DEFAULT 1, created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_work_strategy_executions_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_work_strategy_execution UNIQUE(owner_id,execution_id),
      CONSTRAINT uq_work_strategy_execution_idem UNIQUE(owner_id,idempotency_key),
      CONSTRAINT fk_work_execution_strategy FOREIGN KEY(strategy_id,owner_id) REFERENCES work_strategies(id,owner_id),
      CONSTRAINT fk_work_execution_execution FOREIGN KEY(execution_id,owner_id) REFERENCES intelligence_executions(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_work_execution_problem FOREIGN KEY(problem_id,owner_id) REFERENCES life_problems(id,owner_id),
      CONSTRAINT fk_work_execution_approach FOREIGN KEY(approach_id,owner_id) REFERENCES life_problem_approaches(id,owner_id),
      CONSTRAINT ck_work_execution_basis CHECK(basis IN ('manual','deterministic','inferred','unknown')),
      CONSTRAINT ck_work_execution_provenance CHECK(jsonb_typeof(provenance)='object'),
      CONSTRAINT ck_work_execution_sequence CHECK(next_trace_sequence>0)
    );
    CREATE TABLE work_trace_events (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      strategy_execution_id uuid NOT NULL, sequence_number integer NOT NULL, action_type varchar(48) NOT NULL,
      tool_identity varchar(128), target_type varchar(48), target_ref varchar(512), action_detail jsonb NOT NULL DEFAULT '{}',
      result varchar(32) NOT NULL DEFAULT 'unknown', duration_ms integer, items_count integer, bytes_count bigint,
      evidence_id uuid, usage_log_id uuid REFERENCES usage_log(id) ON DELETE SET NULL, basis varchar(24) NOT NULL DEFAULT 'unknown',
      provenance jsonb NOT NULL DEFAULT '{}', idempotency_key varchar(128) NOT NULL,
      occurred_at timestamp NOT NULL DEFAULT now(), created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_work_trace_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_work_trace_sequence UNIQUE(owner_id,strategy_execution_id,sequence_number),
      CONSTRAINT uq_work_trace_idem UNIQUE(owner_id,strategy_execution_id,idempotency_key),
      CONSTRAINT fk_work_trace_execution FOREIGN KEY(strategy_execution_id,owner_id) REFERENCES work_strategy_executions(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_work_trace_evidence FOREIGN KEY(evidence_id,owner_id) REFERENCES intelligence_evidence(id,owner_id),
      CONSTRAINT ck_work_trace_action CHECK(action_type IN ('repository_map_inspected','symbol_searched','file_read','dependency_followed','git_history_inspected','test_selected','focused_test_run','full_suite_run','migration_test','static_analysis','reviewer_invoked','finding_reproduced','change_attempted','attempt_reverted','failure_encountered','environment_issue_identified','verification_passed','verification_failed','continued_search','stopped_search','work_escalated','unknown')),
      CONSTRAINT ck_work_trace_result CHECK(result IN ('succeeded','failed','partial','no_result','cancelled','deferred','unknown')),
      CONSTRAINT ck_work_trace_basis CHECK(basis IN ('manual','deterministic','inferred','unknown')),
      CONSTRAINT ck_work_trace_counts CHECK(sequence_number>0 AND (duration_ms IS NULL OR duration_ms>=0) AND (items_count IS NULL OR items_count>=0) AND (bytes_count IS NULL OR bytes_count>=0)),
      CONSTRAINT ck_work_trace_json CHECK(jsonb_typeof(action_detail)='object' AND jsonb_typeof(provenance)='object')
    );
    CREATE TABLE work_efficiency_observations (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      strategy_execution_id uuid NOT NULL, trace_event_id uuid, metric_type varchar(48) NOT NULL,
      numeric_value numeric(18,4) NOT NULL, unit varchar(24) NOT NULL, evidence_id uuid,
      basis varchar(24) NOT NULL DEFAULT 'unknown', provenance jsonb NOT NULL DEFAULT '{}', idempotency_key varchar(128) NOT NULL,
      observed_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_work_efficiency_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_work_efficiency_idem UNIQUE(owner_id,strategy_execution_id,idempotency_key),
      CONSTRAINT fk_work_efficiency_execution FOREIGN KEY(strategy_execution_id,owner_id) REFERENCES work_strategy_executions(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_work_efficiency_trace FOREIGN KEY(trace_event_id,owner_id) REFERENCES work_trace_events(id,owner_id),
      CONSTRAINT fk_work_efficiency_evidence FOREIGN KEY(evidence_id,owner_id) REFERENCES intelligence_evidence(id,owner_id),
      CONSTRAINT ck_work_efficiency_metric CHECK(metric_type IN ('wall_clock_duration','active_duration','search_operations','files_inspected','repeated_reads','failed_attempts','edits','reverts','focused_test_runs','full_suite_runs','unnecessary_full_suite_runs','provider_calls','tool_calls','prompt_tokens','completion_tokens','monetary_cost','retry_count','reviewer_count','rework_count','time_to_first_correct_hypothesis','time_to_verified_solution','unknown')),
      CONSTRAINT ck_work_efficiency_value CHECK(numeric_value>=0 AND length(btrim(unit))>0),
      CONSTRAINT ck_work_efficiency_basis CHECK(basis IN ('manual','deterministic','inferred','unknown')),
      CONSTRAINT ck_work_efficiency_provenance CHECK(jsonb_typeof(provenance)='object')
    );
    CREATE TABLE work_strategy_findings (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      strategy_execution_id uuid NOT NULL, trace_event_id uuid, finding_type varchar(48) NOT NULL, description text NOT NULL,
      justified boolean NOT NULL DEFAULT false, evidence_id uuid, basis varchar(24) NOT NULL DEFAULT 'unknown',
      provenance jsonb NOT NULL DEFAULT '{}', idempotency_key varchar(128) NOT NULL, observed_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_work_findings_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_work_finding_idem UNIQUE(owner_id,strategy_execution_id,idempotency_key),
      CONSTRAINT fk_work_finding_execution FOREIGN KEY(strategy_execution_id,owner_id) REFERENCES work_strategy_executions(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_work_finding_trace FOREIGN KEY(trace_event_id,owner_id) REFERENCES work_trace_events(id,owner_id),
      CONSTRAINT fk_work_finding_evidence FOREIGN KEY(evidence_id,owner_id) REFERENCES intelligence_evidence(id,owner_id),
      CONSTRAINT ck_work_finding_type CHECK(finding_type IN ('repeated_file_reads','low_yield_broad_search','full_suite_too_early','repeated_failed_approach','wrong_subsystem','unnecessary_provider_call','duplicated_review','excessive_context','missing_early_reproduction','poor_search_term','premature_implementation','excessive_analysis','justified_expense','other','unknown')),
      CONSTRAINT ck_work_finding_text CHECK(length(btrim(description))>0),
      CONSTRAINT ck_work_finding_basis CHECK(basis IN ('manual','deterministic','inferred','unknown')),
      CONSTRAINT ck_work_finding_provenance CHECK(jsonb_typeof(provenance)='object')
    );
    CREATE TABLE work_verification_obligations (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      strategy_execution_id uuid NOT NULL, requirement_kind varchar(48) NOT NULL, description text NOT NULL,
      required boolean NOT NULL DEFAULT true, source_task_id uuid, basis varchar(24) NOT NULL DEFAULT 'unknown',
      provenance jsonb NOT NULL DEFAULT '{}', idempotency_key varchar(128) NOT NULL, created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_work_obligations_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_work_obligation_idem UNIQUE(owner_id,strategy_execution_id,idempotency_key),
      CONSTRAINT fk_work_obligation_execution FOREIGN KEY(strategy_execution_id,owner_id) REFERENCES work_strategy_executions(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_work_obligation_task FOREIGN KEY(source_task_id,owner_id) REFERENCES mainai_tasks(id,owner_id),
      CONSTRAINT ck_work_obligation_kind CHECK(requirement_kind IN ('focused_tests','full_suite','migration_roundtrip','static_analysis','security_review','independent_review','founder_approval','other','unknown')),
      CONSTRAINT ck_work_obligation_text CHECK(length(btrim(description))>0),
      CONSTRAINT ck_work_obligation_basis CHECK(basis IN ('manual','deterministic','inferred','unknown')),
      CONSTRAINT ck_work_obligation_provenance CHECK(jsonb_typeof(provenance)='object')
    );
    CREATE TABLE work_verification_observations (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      obligation_id uuid NOT NULL, status varchar(32) NOT NULL DEFAULT 'unknown', reason text NOT NULL,
      evidence_id uuid, idempotency_key varchar(128) NOT NULL, observed_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_work_verification_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_work_verification_idem UNIQUE(owner_id,obligation_id,idempotency_key),
      CONSTRAINT fk_work_verification_obligation FOREIGN KEY(obligation_id,owner_id) REFERENCES work_verification_obligations(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_work_verification_evidence FOREIGN KEY(evidence_id,owner_id) REFERENCES intelligence_evidence(id,owner_id),
      CONSTRAINT ck_work_verification_status CHECK(status IN ('performed_passed','performed_failed','missing','waived','unknown')),
      CONSTRAINT ck_work_verification_reason CHECK(length(btrim(reason))>0),
      CONSTRAINT ck_work_verification_evidence_required CHECK(status NOT IN ('performed_passed','performed_failed') OR evidence_id IS NOT NULL)
    );
    CREATE TABLE work_stopping_decisions (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      strategy_execution_id uuid NOT NULL, decision_type varchar(32) NOT NULL, reason text NOT NULL,
      subsequent_outcome varchar(32), evidence_id uuid, basis varchar(24) NOT NULL DEFAULT 'unknown',
      idempotency_key varchar(128) NOT NULL, decided_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_work_stopping_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_work_stopping_idem UNIQUE(owner_id,strategy_execution_id,idempotency_key),
      CONSTRAINT fk_work_stopping_execution FOREIGN KEY(strategy_execution_id,owner_id) REFERENCES work_strategy_executions(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_work_stopping_evidence FOREIGN KEY(evidence_id,owner_id) REFERENCES intelligence_evidence(id,owner_id),
      CONSTRAINT ck_work_stopping_type CHECK(decision_type IN ('continued_search','stopped_search','escalated_to_review','ran_more_tests','sufficient_evidence','insufficient_evidence','blocked','deferred','unknown')),
      CONSTRAINT ck_work_stopping_outcome CHECK(subsequent_outcome IS NULL OR subsequent_outcome IN ('helpful','neutral','harmful','unknown')),
      CONSTRAINT ck_work_stopping_reason CHECK(length(btrim(reason))>0),
      CONSTRAINT ck_work_stopping_basis CHECK(basis IN ('manual','deterministic','inferred','unknown'))
    );
    CREATE TABLE work_specialist_contributions (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      strategy_execution_id uuid NOT NULL, specialist_execution_id uuid NOT NULL, purpose text NOT NULL,
      contribution varchar(32) NOT NULL DEFAULT 'unknown', evidence_available_before jsonb NOT NULL DEFAULT '{}',
      evidence_id uuid, duration_ms integer, idempotency_key varchar(128) NOT NULL, created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_work_specialist_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_work_specialist_idem UNIQUE(owner_id,strategy_execution_id,idempotency_key),
      CONSTRAINT fk_work_specialist_subject FOREIGN KEY(strategy_execution_id,owner_id) REFERENCES work_strategy_executions(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_work_specialist_execution FOREIGN KEY(specialist_execution_id,owner_id) REFERENCES intelligence_executions(id,owner_id),
      CONSTRAINT fk_work_specialist_evidence FOREIGN KEY(evidence_id,owner_id) REFERENCES intelligence_evidence(id,owner_id),
      CONSTRAINT ck_work_specialist_contribution CHECK(contribution IN ('unique_finding','duplicate_finding','confirmed_finding','false_positive','rework_saved','rework_caused','no_contribution','unknown')),
      CONSTRAINT ck_work_specialist_purpose CHECK(length(btrim(purpose))>0),
      CONSTRAINT ck_work_specialist_duration CHECK(duration_ms IS NULL OR duration_ms>=0),
      CONSTRAINT ck_work_specialist_before CHECK(jsonb_typeof(evidence_available_before)='object')
    );
    CREATE TABLE work_strategy_lesson_links (
      owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE, strategy_id uuid NOT NULL,
      engineering_lesson_id uuid NOT NULL REFERENCES engineering_lessons(id) ON DELETE CASCADE,
      relation varchar(32) NOT NULL, evidence_id uuid, created_at timestamp NOT NULL DEFAULT now(),
      PRIMARY KEY(owner_id,strategy_id,engineering_lesson_id),
      CONSTRAINT fk_work_lesson_strategy FOREIGN KEY(strategy_id,owner_id) REFERENCES work_strategies(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_work_lesson_evidence FOREIGN KEY(evidence_id,owner_id) REFERENCES intelligence_evidence(id,owner_id),
      CONSTRAINT ck_work_lesson_relation CHECK(relation IN ('candidate_pattern','verified_pattern','counterexample','superseded_pattern','unknown'))
    );

    CREATE INDEX ix_work_strategies_owner_category ON work_strategies(owner_id,work_category,strategy_key,version);
    CREATE INDEX ix_work_strategy_predecessor ON work_strategies(predecessor_id) WHERE predecessor_id IS NOT NULL;
    CREATE INDEX ix_work_executions_strategy ON work_strategy_executions(owner_id,strategy_id,created_at);
    CREATE INDEX ix_work_executions_problem ON work_strategy_executions(problem_id) WHERE problem_id IS NOT NULL;
    CREATE INDEX ix_work_executions_approach ON work_strategy_executions(approach_id) WHERE approach_id IS NOT NULL;
    CREATE INDEX ix_work_trace_order ON work_trace_events(strategy_execution_id,sequence_number);
    CREATE INDEX ix_work_trace_evidence ON work_trace_events(evidence_id) WHERE evidence_id IS NOT NULL;
    CREATE INDEX ix_work_trace_usage ON work_trace_events(usage_log_id) WHERE usage_log_id IS NOT NULL;
    CREATE INDEX ix_work_efficiency_trace ON work_efficiency_observations(trace_event_id) WHERE trace_event_id IS NOT NULL;
    CREATE INDEX ix_work_efficiency_evidence ON work_efficiency_observations(owner_id,evidence_id) WHERE evidence_id IS NOT NULL;
    CREATE INDEX ix_work_findings_trace ON work_strategy_findings(trace_event_id) WHERE trace_event_id IS NOT NULL;
    CREATE INDEX ix_work_findings_evidence ON work_strategy_findings(owner_id,evidence_id) WHERE evidence_id IS NOT NULL;
    CREATE INDEX ix_work_obligations_task ON work_verification_obligations(source_task_id) WHERE source_task_id IS NOT NULL;
    CREATE INDEX ix_work_verification_evidence ON work_verification_observations(owner_id,evidence_id) WHERE evidence_id IS NOT NULL;
    CREATE INDEX ix_work_stopping_execution ON work_stopping_decisions(strategy_execution_id,decided_at);
    CREATE INDEX ix_work_stopping_evidence ON work_stopping_decisions(owner_id,evidence_id) WHERE evidence_id IS NOT NULL;
    CREATE INDEX ix_work_specialist_execution ON work_specialist_contributions(owner_id,specialist_execution_id);
    CREATE INDEX ix_work_specialist_evidence ON work_specialist_contributions(owner_id,evidence_id) WHERE evidence_id IS NOT NULL;
    CREATE INDEX ix_work_lesson_strategy ON work_strategy_lesson_links(strategy_id);
    CREATE INDEX ix_work_lesson_evidence ON work_strategy_lesson_links(owner_id,evidence_id) WHERE evidence_id IS NOT NULL;
    """)
    for table in TABLES:
        op.execute(
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY; ALTER TABLE {table} FORCE ROW LEVEL SECURITY"
        )
        op.execute(
            f"CREATE POLICY {table}_isolation ON {table} USING (owner_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid) WITH CHECK (owner_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid)"
        )
    op.execute("""
    CREATE FUNCTION work_intelligence_deny_mutation() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
    BEGIN RAISE EXCEPTION 'work intelligence evidence is append-only'; END $$;
    REVOKE ALL ON FUNCTION work_intelligence_deny_mutation() FROM PUBLIC;
    CREATE FUNCTION work_strategy_execution_counter_only() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
    BEGIN
      IF NEW.id<>OLD.id OR NEW.owner_id<>OLD.owner_id OR NEW.strategy_id<>OLD.strategy_id OR NEW.execution_id<>OLD.execution_id
         OR NEW.problem_id IS DISTINCT FROM OLD.problem_id OR NEW.approach_id IS DISTINCT FROM OLD.approach_id
         OR NEW.basis<>OLD.basis OR NEW.provenance<>OLD.provenance OR NEW.idempotency_key<>OLD.idempotency_key
         OR NEW.created_at<>OLD.created_at OR NEW.next_trace_sequence<>OLD.next_trace_sequence+1 THEN
        RAISE EXCEPTION 'work strategy execution identity is immutable';
      END IF;
      RETURN NEW;
    END $$;
    REVOKE ALL ON FUNCTION work_strategy_execution_counter_only() FROM PUBLIC;
    CREATE TRIGGER trg_work_strategy_execution_counter BEFORE UPDATE ON work_strategy_executions FOR EACH ROW EXECUTE FUNCTION work_strategy_execution_counter_only();
    """)
    for table in tuple(t for t in TABLES if t != "work_strategy_executions"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION work_intelligence_deny_mutation()"
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION work_strategy_execution_counter_only()")
    op.execute("DROP FUNCTION work_intelligence_deny_mutation()")
