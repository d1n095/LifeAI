"""Deterministic strategy comparison, experimentation and governed promotion."""

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

TABLES = (
    "strategy_comparisons",
    "strategy_comparability_assessments",
    "strategy_quality_assessments",
    "strategy_efficiency_deltas",
    "strategy_experiments",
    "strategy_promotion_candidates",
    "strategy_experiment_comparisons",
    "strategy_promotion_comparisons",
    "strategy_learning_links",
    "strategy_learning_observations",
    "strategy_evaluation_events",
)


def upgrade() -> None:
    op.execute("""
    CREATE TABLE strategy_comparisons (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      baseline_binding_id uuid NOT NULL, challenger_binding_id uuid NOT NULL, problem_id uuid, task_id uuid,
      task_type varchar(128), domain varchar(128), risk_level varchar(24) NOT NULL DEFAULT 'unknown',
      comparison_basis varchar(24) NOT NULL DEFAULT 'unknown', provenance jsonb NOT NULL DEFAULT '{}',
      idempotency_key varchar(128) NOT NULL, created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_strategy_comparisons_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_strategy_comparison_idem UNIQUE(owner_id,idempotency_key),
      CONSTRAINT fk_strategy_comparison_baseline FOREIGN KEY(baseline_binding_id,owner_id) REFERENCES work_strategy_executions(id,owner_id),
      CONSTRAINT fk_strategy_comparison_challenger FOREIGN KEY(challenger_binding_id,owner_id) REFERENCES work_strategy_executions(id,owner_id),
      CONSTRAINT fk_strategy_comparison_problem FOREIGN KEY(problem_id,owner_id) REFERENCES life_problems(id,owner_id),
      CONSTRAINT fk_strategy_comparison_task FOREIGN KEY(task_id,owner_id) REFERENCES mainai_tasks(id,owner_id),
      CONSTRAINT ck_strategy_comparison_distinct CHECK(baseline_binding_id<>challenger_binding_id),
      CONSTRAINT ck_strategy_comparison_basis CHECK(comparison_basis IN ('manual','deterministic','inferred','unknown')),
      CONSTRAINT ck_strategy_comparison_risk CHECK(risk_level IN ('low','medium','high','critical','unknown')),
      CONSTRAINT ck_strategy_comparison_provenance CHECK(jsonb_typeof(provenance)='object')
    );
    CREATE TABLE strategy_comparability_assessments (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      comparison_id uuid NOT NULL, status varchar(32) NOT NULL DEFAULT 'unknown', dimensions jsonb NOT NULL DEFAULT '{}',
      reasons jsonb NOT NULL DEFAULT '[]', basis varchar(24) NOT NULL DEFAULT 'unknown', evidence_id uuid,
      supersedes_id uuid, idempotency_key varchar(128) NOT NULL, created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_strategy_comparability_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_strategy_comparability_idem UNIQUE(owner_id,comparison_id,idempotency_key),
      CONSTRAINT fk_strategy_comparability_comparison FOREIGN KEY(comparison_id,owner_id) REFERENCES strategy_comparisons(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_strategy_comparability_evidence FOREIGN KEY(evidence_id,owner_id) REFERENCES intelligence_evidence(id,owner_id),
      CONSTRAINT fk_strategy_comparability_supersedes FOREIGN KEY(supersedes_id,owner_id) REFERENCES strategy_comparability_assessments(id,owner_id),
      CONSTRAINT ck_strategy_comparability_status CHECK(status IN ('comparable','partially_comparable','not_comparable','unknown')),
      CONSTRAINT ck_strategy_comparability_basis CHECK(basis IN ('manual','deterministic','inferred','unknown')),
      CONSTRAINT ck_strategy_comparability_json CHECK(jsonb_typeof(dimensions)='object' AND jsonb_typeof(reasons)='array'),
      CONSTRAINT ck_strategy_comparability_no_self CHECK(supersedes_id IS NULL OR supersedes_id<>id)
    );
    CREATE TABLE strategy_quality_assessments (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      comparison_id uuid NOT NULL, subject varchar(16) NOT NULL, state varchar(32) NOT NULL DEFAULT 'unknown',
      required_count integer NOT NULL, passed_count integer NOT NULL, failed_count integer NOT NULL, missing_count integer NOT NULL,
      unresolved_regression boolean NOT NULL DEFAULT false, scope_violation boolean NOT NULL DEFAULT false,
      reason text NOT NULL, evidence_id uuid, idempotency_key varchar(128) NOT NULL, created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_strategy_quality_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_strategy_quality_idem UNIQUE(owner_id,comparison_id,idempotency_key),
      CONSTRAINT fk_strategy_quality_comparison FOREIGN KEY(comparison_id,owner_id) REFERENCES strategy_comparisons(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_strategy_quality_evidence FOREIGN KEY(evidence_id,owner_id) REFERENCES intelligence_evidence(id,owner_id),
      CONSTRAINT ck_strategy_quality_subject CHECK(subject IN ('baseline','challenger')),
      CONSTRAINT ck_strategy_quality_state CHECK(state IN ('quality_pass','quality_fail','verification_incomplete','regression_detected','invalid_comparison','unknown')),
      CONSTRAINT ck_strategy_quality_counts CHECK(required_count>=0 AND passed_count>=0 AND failed_count>=0 AND missing_count>=0),
      CONSTRAINT ck_strategy_quality_reason CHECK(length(btrim(reason))>0)
    );
    CREATE TABLE strategy_efficiency_deltas (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      comparison_id uuid NOT NULL, baseline_observation_id uuid NOT NULL, challenger_observation_id uuid NOT NULL,
      metric_type varchar(48) NOT NULL, unit varchar(24) NOT NULL, baseline_value numeric(18,4) NOT NULL,
      challenger_value numeric(18,4) NOT NULL, delta_value numeric(18,4) NOT NULL,
      idempotency_key varchar(128) NOT NULL, created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_strategy_efficiency_delta_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_strategy_efficiency_delta_idem UNIQUE(owner_id,comparison_id,idempotency_key),
      CONSTRAINT fk_strategy_efficiency_comparison FOREIGN KEY(comparison_id,owner_id) REFERENCES strategy_comparisons(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_strategy_efficiency_baseline FOREIGN KEY(baseline_observation_id,owner_id) REFERENCES work_efficiency_observations(id,owner_id),
      CONSTRAINT fk_strategy_efficiency_challenger FOREIGN KEY(challenger_observation_id,owner_id) REFERENCES work_efficiency_observations(id,owner_id),
      CONSTRAINT ck_strategy_efficiency_observations CHECK(baseline_observation_id<>challenger_observation_id AND length(btrim(unit))>0),
      CONSTRAINT ck_strategy_efficiency_delta CHECK(delta_value=challenger_value-baseline_value)
    );
    CREATE TABLE strategy_experiments (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      baseline_strategy_id uuid NOT NULL, challenger_strategy_id uuid NOT NULL, hypothesis text NOT NULL,
      intended_change text NOT NULL, expected_benefit text NOT NULL, quality_invariants jsonb NOT NULL DEFAULT '[]',
      scope jsonb NOT NULL DEFAULT '{}', applicability jsonb NOT NULL DEFAULT '{}', required_sample_count integer,
      state varchar(24) NOT NULL DEFAULT 'draft', failure_reason text, provenance jsonb NOT NULL DEFAULT '{}',
      idempotency_key varchar(128) NOT NULL, created_at timestamp NOT NULL DEFAULT now(), updated_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_strategy_experiments_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_strategy_experiment_idem UNIQUE(owner_id,idempotency_key),
      CONSTRAINT fk_strategy_experiment_baseline FOREIGN KEY(baseline_strategy_id,owner_id) REFERENCES work_strategies(id,owner_id),
      CONSTRAINT fk_strategy_experiment_challenger FOREIGN KEY(challenger_strategy_id,owner_id) REFERENCES work_strategies(id,owner_id),
      CONSTRAINT ck_strategy_experiment_distinct CHECK(baseline_strategy_id<>challenger_strategy_id),
      CONSTRAINT ck_strategy_experiment_state CHECK(state IN ('draft','ready','running','completed','failed','cancelled','invalidated')),
      CONSTRAINT ck_strategy_experiment_sample CHECK(required_sample_count IS NULL OR required_sample_count>0),
      CONSTRAINT ck_strategy_experiment_json CHECK(jsonb_typeof(quality_invariants)='array' AND jsonb_typeof(scope)='object' AND jsonb_typeof(applicability)='object' AND jsonb_typeof(provenance)='object')
    );
    CREATE TABLE strategy_promotion_candidates (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      strategy_id uuid NOT NULL, baseline_strategy_id uuid NOT NULL, applicable_context jsonb NOT NULL DEFAULT '{}',
      known_tradeoffs jsonb NOT NULL DEFAULT '[]', confidence_basis varchar(24) NOT NULL DEFAULT 'unknown',
      minimum_valid_comparisons integer, state varchar(32) NOT NULL DEFAULT 'candidate', provenance jsonb NOT NULL DEFAULT '{}',
      idempotency_key varchar(128) NOT NULL, created_at timestamp NOT NULL DEFAULT now(), updated_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_strategy_candidates_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_strategy_candidate_idem UNIQUE(owner_id,idempotency_key),
      CONSTRAINT fk_strategy_candidate_strategy FOREIGN KEY(strategy_id,owner_id) REFERENCES work_strategies(id,owner_id),
      CONSTRAINT fk_strategy_candidate_baseline FOREIGN KEY(baseline_strategy_id,owner_id) REFERENCES work_strategies(id,owner_id),
      CONSTRAINT ck_strategy_candidate_distinct CHECK(strategy_id<>baseline_strategy_id),
      CONSTRAINT ck_strategy_candidate_state CHECK(state IN ('candidate','insufficient_evidence','under_review','approved','rejected','superseded','invalidated')),
      CONSTRAINT ck_strategy_candidate_confidence CHECK(confidence_basis IN ('manual','deterministic','inferred','unknown')),
      CONSTRAINT ck_strategy_candidate_sample CHECK(minimum_valid_comparisons IS NULL OR minimum_valid_comparisons>0),
      CONSTRAINT ck_strategy_candidate_json CHECK(jsonb_typeof(applicable_context)='object' AND jsonb_typeof(known_tradeoffs)='array' AND jsonb_typeof(provenance)='object')
    );
    """)
    for table, parent, parent_fk in (
        ("strategy_experiment_comparisons", "strategy_experiments", "experiment_id"),
        (
            "strategy_promotion_comparisons",
            "strategy_promotion_candidates",
            "candidate_id",
        ),
    ):
        op.execute(f"""
        CREATE TABLE {table} (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          {parent_fk} uuid NOT NULL, comparison_id uuid NOT NULL, idempotency_key varchar(128) NOT NULL,
          created_at timestamp NOT NULL DEFAULT now(), CONSTRAINT uq_{table}_id_owner UNIQUE(id,owner_id),
          CONSTRAINT uq_{table}_pair UNIQUE(owner_id,{parent_fk},comparison_id),
          CONSTRAINT uq_{table}_idem UNIQUE(owner_id,idempotency_key),
          CONSTRAINT fk_{table}_parent FOREIGN KEY({parent_fk},owner_id) REFERENCES {parent}(id,owner_id) ON DELETE CASCADE,
          CONSTRAINT fk_{table}_comparison FOREIGN KEY(comparison_id,owner_id) REFERENCES strategy_comparisons(id,owner_id) ON DELETE CASCADE
        )""")
    op.execute("""
    CREATE TABLE strategy_learning_links (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      comparison_id uuid, experiment_id uuid, idea_id uuid, component_id uuid, assumption_id uuid,
      disposition varchar(32) NOT NULL DEFAULT 'unknown', relation varchar(32) NOT NULL DEFAULT 'unknown', evidence_id uuid,
      reason text NOT NULL, idempotency_key varchar(128) NOT NULL, created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_strategy_learning_links_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_strategy_learning_link_idem UNIQUE(owner_id,idempotency_key),
      CONSTRAINT fk_strategy_learning_comparison FOREIGN KEY(comparison_id,owner_id) REFERENCES strategy_comparisons(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_strategy_learning_experiment FOREIGN KEY(experiment_id,owner_id) REFERENCES strategy_experiments(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_strategy_learning_idea FOREIGN KEY(idea_id,owner_id) REFERENCES intelligence_ideas(id,owner_id),
      CONSTRAINT fk_strategy_learning_component FOREIGN KEY(component_id,owner_id) REFERENCES life_solution_components(id,owner_id),
      CONSTRAINT fk_strategy_learning_assumption FOREIGN KEY(assumption_id,owner_id) REFERENCES life_problem_assumptions(id,owner_id),
      CONSTRAINT fk_strategy_learning_evidence FOREIGN KEY(evidence_id,owner_id) REFERENCES intelligence_evidence(id,owner_id),
      CONSTRAINT ck_strategy_learning_parent CHECK(num_nonnulls(comparison_id,experiment_id)=1),
      CONSTRAINT ck_strategy_learning_subject CHECK(num_nonnulls(idea_id,component_id,assumption_id)=1),
      CONSTRAINT ck_strategy_learning_disposition CHECK(disposition IN ('useful','harmful','unproven','context_specific','accepted','rejected','deferred','unknown')),
      CONSTRAINT ck_strategy_learning_relation CHECK(relation IN ('contributed','supports','contradicts','invalidates','unresolved_contradiction','accepted_component','rejected_component','deferred_component','unknown')),
      CONSTRAINT ck_strategy_learning_reason CHECK(length(btrim(reason))>0)
    );
    CREATE TABLE strategy_learning_observations (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      comparison_id uuid NOT NULL, observation_type varchar(48) NOT NULL, trace_event_id uuid, related_trace_event_id uuid,
      stopping_decision_id uuid, numeric_value numeric(18,4), unit varchar(24), reason text NOT NULL,
      basis varchar(24) NOT NULL DEFAULT 'unknown', idempotency_key varchar(128) NOT NULL, created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_strategy_learning_observations_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_strategy_learning_observation_idem UNIQUE(owner_id,comparison_id,idempotency_key),
      CONSTRAINT fk_strategy_learning_observation_comparison FOREIGN KEY(comparison_id,owner_id) REFERENCES strategy_comparisons(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_strategy_learning_observation_trace FOREIGN KEY(trace_event_id,owner_id) REFERENCES work_trace_events(id,owner_id),
      CONSTRAINT fk_strategy_learning_observation_related_trace FOREIGN KEY(related_trace_event_id,owner_id) REFERENCES work_trace_events(id,owner_id),
      CONSTRAINT fk_strategy_learning_observation_stopping FOREIGN KEY(stopping_decision_id,owner_id) REFERENCES work_stopping_decisions(id,owner_id),
      CONSTRAINT ck_strategy_learning_observation_type CHECK(observation_type IN ('duplicate_search','repeated_zero_result','overly_broad_search','overly_narrow_search','unnecessary_file_open','missed_reference_path','successful_narrowing','dependency_first_navigation','tests_to_source_trace','useful_search_ordering','search_to_answer_latency','searches_before_first_hit','irrelevant_result_ratio','continuation_helpful','continuation_wasteful','stopping_premature','stopping_prevented_waste','strategy_switch_helpful','unknown')),
      CONSTRAINT ck_strategy_learning_observation_basis CHECK(basis IN ('manual','deterministic','inferred','unknown')),
      CONSTRAINT ck_strategy_learning_observation_value CHECK((numeric_value IS NULL AND unit IS NULL) OR (numeric_value IS NOT NULL AND unit IS NOT NULL AND numeric_value>=0)),
      CONSTRAINT ck_strategy_learning_observation_reason CHECK(length(btrim(reason))>0)
    );
    CREATE TABLE strategy_evaluation_events (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      experiment_id uuid, candidate_id uuid, event_type varchar(32) NOT NULL, from_state varchar(32), to_state varchar(32),
      detail jsonb NOT NULL DEFAULT '{}', idempotency_key varchar(128) NOT NULL, created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_strategy_evaluation_events_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_strategy_evaluation_event_idem UNIQUE(owner_id,idempotency_key),
      CONSTRAINT fk_strategy_event_experiment FOREIGN KEY(experiment_id,owner_id) REFERENCES strategy_experiments(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_strategy_event_candidate FOREIGN KEY(candidate_id,owner_id) REFERENCES strategy_promotion_candidates(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT ck_strategy_event_parent CHECK(num_nonnulls(experiment_id,candidate_id)=1),
      CONSTRAINT ck_strategy_event_type CHECK(event_type IN ('created','state_changed','comparison_linked','evidence_linked')),
      CONSTRAINT ck_strategy_event_detail CHECK(jsonb_typeof(detail)='object')
    );
    """)
    for table in TABLES:
        op.execute(
            f"CREATE INDEX ix_{table}_owner_created ON {table}(owner_id,created_at)"
        )
        op.execute(
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY; ALTER TABLE {table} FORCE ROW LEVEL SECURITY"
        )
        op.execute(
            f"CREATE POLICY {table}_isolation ON {table} USING (owner_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid) WITH CHECK (owner_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid)"
        )
    op.execute("""
    CREATE INDEX ix_strategy_comparison_baseline ON strategy_comparisons(owner_id,baseline_binding_id);
    CREATE INDEX ix_strategy_comparison_challenger ON strategy_comparisons(owner_id,challenger_binding_id);
    CREATE INDEX ix_strategy_efficiency_baseline ON strategy_efficiency_deltas(owner_id,baseline_observation_id);
    CREATE INDEX ix_strategy_efficiency_challenger ON strategy_efficiency_deltas(owner_id,challenger_observation_id);
    CREATE INDEX ix_strategy_learning_idea ON strategy_learning_links(owner_id,idea_id) WHERE idea_id IS NOT NULL;
    CREATE INDEX ix_strategy_learning_component ON strategy_learning_links(owner_id,component_id) WHERE component_id IS NOT NULL;
    CREATE INDEX ix_strategy_learning_assumption ON strategy_learning_links(owner_id,assumption_id) WHERE assumption_id IS NOT NULL;
    CREATE FUNCTION strategy_evaluation_deny_mutation() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
    BEGIN RAISE EXCEPTION 'strategy evaluation evidence is append-only'; END $$;
    REVOKE ALL ON FUNCTION strategy_evaluation_deny_mutation() FROM PUBLIC;
    CREATE FUNCTION strategy_evaluation_state_only() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
    DECLARE valid_count integer; passing_count integer;
    BEGIN
      IF NEW.id<>OLD.id OR NEW.owner_id<>OLD.owner_id OR NEW.idempotency_key<>OLD.idempotency_key
         OR NEW.created_at<>OLD.created_at THEN RAISE EXCEPTION 'strategy evaluation identity is immutable'; END IF;
      IF TG_TABLE_NAME='strategy_experiments' THEN
        IF NEW.baseline_strategy_id<>OLD.baseline_strategy_id OR NEW.challenger_strategy_id<>OLD.challenger_strategy_id
           OR NEW.hypothesis<>OLD.hypothesis OR NEW.intended_change<>OLD.intended_change
           OR NEW.expected_benefit<>OLD.expected_benefit OR NEW.quality_invariants<>OLD.quality_invariants
           OR NEW.scope<>OLD.scope OR NEW.applicability<>OLD.applicability
           OR NEW.required_sample_count IS DISTINCT FROM OLD.required_sample_count OR NEW.provenance<>OLD.provenance
        THEN RAISE EXCEPTION 'strategy experiment definition is immutable'; END IF;
        IF NOT ((OLD.state='draft' AND NEW.state IN ('ready','cancelled','invalidated'))
          OR (OLD.state='ready' AND NEW.state IN ('running','cancelled','invalidated'))
          OR (OLD.state='running' AND NEW.state IN ('completed','failed','cancelled','invalidated'))
          OR (OLD.state IN ('completed','failed') AND NEW.state='invalidated'))
        THEN RAISE EXCEPTION 'invalid strategy experiment transition'; END IF;
      ELSE
        IF NEW.strategy_id<>OLD.strategy_id OR NEW.baseline_strategy_id<>OLD.baseline_strategy_id
           OR NEW.applicable_context<>OLD.applicable_context OR NEW.known_tradeoffs<>OLD.known_tradeoffs
           OR NEW.confidence_basis<>OLD.confidence_basis
           OR NEW.minimum_valid_comparisons IS DISTINCT FROM OLD.minimum_valid_comparisons
           OR NEW.provenance<>OLD.provenance
        THEN RAISE EXCEPTION 'strategy promotion definition is immutable'; END IF;
        IF NOT ((OLD.state='candidate' AND NEW.state IN ('insufficient_evidence','under_review','rejected','invalidated'))
          OR (OLD.state='insufficient_evidence' AND NEW.state IN ('under_review','rejected','invalidated'))
          OR (OLD.state='under_review' AND NEW.state IN ('approved','rejected','insufficient_evidence','invalidated'))
          OR (OLD.state='approved' AND NEW.state IN ('superseded','invalidated')))
        THEN RAISE EXCEPTION 'invalid strategy promotion transition'; END IF;
        IF NEW.state='approved' THEN
          SELECT count(*), count(*) FILTER (WHERE q.state='quality_pass' AND NOT q.unresolved_regression AND NOT q.scope_violation)
          INTO valid_count, passing_count
          FROM public.strategy_promotion_comparisons l
          JOIN LATERAL (
            SELECT a.status FROM public.strategy_comparability_assessments a
            WHERE a.owner_id=NEW.owner_id AND a.comparison_id=l.comparison_id
            ORDER BY a.created_at DESC,a.id DESC LIMIT 1
          ) c ON c.status IN ('comparable','partially_comparable')
          LEFT JOIN LATERAL (
            SELECT q1.state,q1.unresolved_regression,q1.scope_violation FROM public.strategy_quality_assessments q1
            WHERE q1.owner_id=NEW.owner_id AND q1.comparison_id=l.comparison_id AND q1.subject='challenger'
            ORDER BY q1.created_at DESC,q1.id DESC LIMIT 1
          ) q ON true
          WHERE l.owner_id=NEW.owner_id AND l.candidate_id=NEW.id;
          IF valid_count < COALESCE(NEW.minimum_valid_comparisons,2) OR passing_count<>valid_count
          THEN RAISE EXCEPTION 'quality-safe promotion evidence threshold is not satisfied'; END IF;
        END IF;
      END IF;
      RETURN NEW;
    END $$;
    REVOKE ALL ON FUNCTION strategy_evaluation_state_only() FROM PUBLIC;
    CREATE TRIGGER trg_strategy_experiments_state BEFORE UPDATE ON strategy_experiments FOR EACH ROW EXECUTE FUNCTION strategy_evaluation_state_only();
    CREATE TRIGGER trg_strategy_candidates_state BEFORE UPDATE ON strategy_promotion_candidates FOR EACH ROW EXECUTE FUNCTION strategy_evaluation_state_only();
    """)
    for table in tuple(
        t
        for t in TABLES
        if t not in ("strategy_experiments", "strategy_promotion_candidates")
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION strategy_evaluation_deny_mutation()"
        )
    op.execute(
        "ALTER TABLE work_strategy_lesson_links DROP CONSTRAINT ck_work_lesson_relation"
    )
    op.execute(
        "ALTER TABLE work_strategy_lesson_links ADD CONSTRAINT ck_work_lesson_relation CHECK(relation IN ('candidate_pattern','verified_pattern','rejected_pattern','counterexample','superseded_pattern','unknown'))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE work_strategy_lesson_links DROP CONSTRAINT ck_work_lesson_relation"
    )
    op.execute(
        "ALTER TABLE work_strategy_lesson_links ADD CONSTRAINT ck_work_lesson_relation CHECK(relation IN ('candidate_pattern','verified_pattern','counterexample','superseded_pattern','unknown'))"
    )
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION strategy_evaluation_state_only()")
    op.execute("DROP FUNCTION strategy_evaluation_deny_mutation()")
