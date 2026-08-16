"""Life Intelligence Governance & Meta-Learning — observation-first foundation.

Extends the existing MainAI execution runtime with immutable identity snapshots, raw
observations, separately stored interpretations, and idea-level learning.  It adds no queue,
router, leaderboard, lesson store, provenance graph, or provider dependency.
"""

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


TABLES = (
    "intelligence_executions",
    "intelligence_evidence",
    "intelligence_interpretations",
    "intelligence_ideas",
    "intelligence_idea_links",
    "intelligence_idea_lessons",
)


def upgrade() -> None:
    op.execute("""
        ALTER TABLE mainai_task_events ADD CONSTRAINT uq_mainai_task_events_id_owner UNIQUE (id, owner_id);
        ALTER TABLE mainai_recovery_records ADD CONSTRAINT uq_mainai_recovery_records_id_owner UNIQUE (id, owner_id);
    """)
    op.execute("""
        CREATE TABLE intelligence_executions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            task_id uuid NOT NULL,
            job_id uuid,
            provider varchar(64), model varchar(128), model_version varchar(128),
            agent_identity varchar(128), execution_environment varchar(128),
            available_tools jsonb NOT NULL DEFAULT '[]'::jsonb,
            capabilities jsonb NOT NULL DEFAULT '[]'::jsonb,
            work_strategy_id varchar(128),
            role varchar(24) NOT NULL DEFAULT 'unknown',
            participation_mode varchar(24) NOT NULL DEFAULT 'unknown',
            domain varchar(128), task_type varchar(128), context jsonb NOT NULL DEFAULT '{}'::jsonb,
            classification_basis varchar(24) NOT NULL DEFAULT 'unknown',
            idempotency_key varchar(128),
            occurred_at timestamp NOT NULL DEFAULT now(), created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_intelligence_executions_id_owner UNIQUE (id, owner_id),
            CONSTRAINT uq_intelligence_executions_idempotency UNIQUE (owner_id, idempotency_key),
            CONSTRAINT fk_intelligence_executions_task_owner FOREIGN KEY (task_id, owner_id)
                REFERENCES mainai_tasks(id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_intelligence_executions_job_owner FOREIGN KEY (job_id, owner_id)
                REFERENCES mainai_jobs(id, owner_id),
            CONSTRAINT ck_intelligence_execution_role CHECK
                (role IN ('builder','reviewer','challenger','verifier','planner','unknown')),
            CONSTRAINT ck_intelligence_participation CHECK
                (participation_mode IN ('primary','shadow','parallel','challenger','reviewer','unknown')),
            CONSTRAINT ck_intelligence_execution_basis CHECK
                (classification_basis IN ('deterministic','manual','inferred','unknown')),
            CONSTRAINT ck_intelligence_execution_arrays CHECK
                (jsonb_typeof(available_tools) = 'array' AND jsonb_typeof(capabilities) = 'array'),
            CONSTRAINT ck_intelligence_execution_context CHECK (jsonb_typeof(context) = 'object')
        );
        CREATE INDEX ix_intelligence_executions_task ON intelligence_executions(task_id);
        CREATE INDEX ix_intelligence_executions_identity_time
            ON intelligence_executions(provider, model, model_version, occurred_at);

        CREATE TABLE intelligence_evidence (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            execution_id uuid NOT NULL, observer_execution_id uuid,
            task_event_id uuid, recovery_record_id uuid,
            evidence_kind varchar(48) NOT NULL,
            review_kind varchar(32) NOT NULL DEFAULT 'unknown',
            deterministic boolean NOT NULL DEFAULT false,
            payload jsonb NOT NULL, source_type varchar(48) NOT NULL, source_ref varchar(256) NOT NULL,
            idempotency_key varchar(128),
            observed_at timestamp NOT NULL DEFAULT now(), created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_intelligence_evidence_id_owner UNIQUE (id, owner_id),
            CONSTRAINT uq_intelligence_evidence_idempotency UNIQUE (owner_id, execution_id, idempotency_key),
            CONSTRAINT fk_intelligence_evidence_execution_owner FOREIGN KEY (execution_id, owner_id)
                REFERENCES intelligence_executions(id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_intelligence_evidence_observer_owner FOREIGN KEY (observer_execution_id, owner_id)
                REFERENCES intelligence_executions(id, owner_id),
            CONSTRAINT fk_intelligence_evidence_event_owner FOREIGN KEY (task_event_id, owner_id)
                REFERENCES mainai_task_events(id, owner_id),
            CONSTRAINT fk_intelligence_evidence_recovery_owner FOREIGN KEY (recovery_record_id, owner_id)
                REFERENCES mainai_recovery_records(id, owner_id),
            CONSTRAINT ck_intelligence_evidence_review CHECK
                (review_kind IN ('self_review','independent_model','deterministic_tool','founder','unknown')),
            CONSTRAINT ck_intelligence_evidence_payload CHECK (jsonb_typeof(payload) = 'object'),
            CONSTRAINT ck_intelligence_evidence_review_actor CHECK (
                (review_kind <> 'self_review' OR observer_execution_id IS NULL OR observer_execution_id = execution_id)
                AND (review_kind <> 'independent_model' OR observer_execution_id IS NOT NULL AND observer_execution_id <> execution_id)
                AND (review_kind <> 'deterministic_tool' OR deterministic)
            )
        );
        CREATE INDEX ix_intelligence_evidence_execution ON intelligence_evidence(execution_id, observed_at);

        CREATE TABLE intelligence_interpretations (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            evidence_id uuid NOT NULL, interpretation_kind varchar(48) NOT NULL,
            payload jsonb NOT NULL, method varchar(128) NOT NULL,
            classification_basis varchar(24) NOT NULL, confidence numeric(5,4),
            supersedes_id uuid, idempotency_key varchar(128), created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_intelligence_interpretations_id_owner UNIQUE (id, owner_id),
            CONSTRAINT uq_intelligence_interpretations_idempotency UNIQUE (owner_id, evidence_id, idempotency_key),
            CONSTRAINT fk_intelligence_interpretation_evidence_owner FOREIGN KEY (evidence_id, owner_id)
                REFERENCES intelligence_evidence(id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_intelligence_interpretation_supersedes_owner FOREIGN KEY (supersedes_id, owner_id)
                REFERENCES intelligence_interpretations(id, owner_id),
            CONSTRAINT ck_intelligence_interpretation_basis CHECK
                (classification_basis IN ('deterministic','manual','inferred','unknown')),
            CONSTRAINT ck_intelligence_interpretation_payload CHECK (jsonb_typeof(payload) = 'object'),
            CONSTRAINT ck_intelligence_interpretation_confidence CHECK
                (confidence IS NULL OR confidence BETWEEN 0 AND 1),
            CONSTRAINT ck_intelligence_interpretation_not_self_supersede CHECK (supersedes_id IS NULL OR supersedes_id <> id)
        );

        CREATE TABLE intelligence_ideas (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            execution_id uuid NOT NULL, evidence_id uuid,
            idea_kind varchar(32) NOT NULL, content text NOT NULL,
            disposition varchar(24) NOT NULL DEFAULT 'unknown', disposition_reason text,
            classification_basis varchar(24) NOT NULL DEFAULT 'unknown', confidence numeric(5,4),
            idempotency_key varchar(128), created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_intelligence_ideas_id_owner UNIQUE (id, owner_id),
            CONSTRAINT uq_intelligence_ideas_idempotency UNIQUE (owner_id, execution_id, idempotency_key),
            CONSTRAINT fk_intelligence_idea_execution_owner FOREIGN KEY (execution_id, owner_id)
                REFERENCES intelligence_executions(id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_intelligence_idea_evidence_owner FOREIGN KEY (evidence_id, owner_id)
                REFERENCES intelligence_evidence(id, owner_id),
            CONSTRAINT ck_intelligence_idea_kind CHECK
                (idea_kind IN ('idea','proposal','approach','risk','test_strategy','architectural_pattern','optimization','assumption')),
            CONSTRAINT ck_intelligence_idea_disposition CHECK
                (disposition IN ('accepted','rejected','deferred','unknown')),
            CONSTRAINT ck_intelligence_idea_basis CHECK
                (classification_basis IN ('deterministic','manual','inferred','unknown')),
            CONSTRAINT ck_intelligence_idea_confidence CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
            CONSTRAINT ck_intelligence_idea_reason CHECK
                (disposition NOT IN ('accepted','rejected') OR disposition_reason IS NOT NULL)
        );
        CREATE INDEX ix_intelligence_ideas_execution ON intelligence_ideas(execution_id);

        CREATE TABLE intelligence_idea_links (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            from_idea_id uuid NOT NULL, to_idea_id uuid NOT NULL, relation varchar(32) NOT NULL,
            evidence_id uuid, created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_intelligence_idea_links UNIQUE (owner_id, from_idea_id, to_idea_id, relation),
            CONSTRAINT fk_intelligence_idea_link_from_owner FOREIGN KEY (from_idea_id, owner_id)
                REFERENCES intelligence_ideas(id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_intelligence_idea_link_to_owner FOREIGN KEY (to_idea_id, owner_id)
                REFERENCES intelligence_ideas(id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_intelligence_idea_link_evidence_owner FOREIGN KEY (evidence_id, owner_id)
                REFERENCES intelligence_evidence(id, owner_id),
            CONSTRAINT ck_intelligence_idea_link_relation CHECK
                (relation IN ('reuses','contradicts','depends_on_assumption','combined_into')),
            CONSTRAINT ck_intelligence_idea_link_not_self CHECK (from_idea_id <> to_idea_id)
        );

        CREATE TABLE intelligence_idea_lessons (
            idea_id uuid NOT NULL, engineering_lesson_id uuid NOT NULL REFERENCES engineering_lessons(id) ON DELETE CASCADE,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            relation varchar(32) NOT NULL, created_at timestamp NOT NULL DEFAULT now(),
            PRIMARY KEY (idea_id, engineering_lesson_id),
            CONSTRAINT fk_intelligence_idea_lesson_owner FOREIGN KEY (idea_id, owner_id)
                REFERENCES intelligence_ideas(id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_intelligence_idea_lesson_relation CHECK
                (relation IN ('failure','root_cause','reusable_lesson','process_improvement'))
        );
    """)

    for table in TABLES:
        op.execute(f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY {table}_isolation ON {table}
                USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
                WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
        """)

    op.execute("""
        CREATE FUNCTION intelligence_governance_deny_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND current_setting('app.mainai_execution_erasure_in_progress', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION '% is append-only: % is not permitted', TG_TABLE_NAME, TG_OP;
        END;
        $$;
        REVOKE ALL ON FUNCTION intelligence_governance_deny_mutation() FROM PUBLIC;
    """)
    for table in TABLES:
        op.execute(f"""CREATE TRIGGER trg_{table}_deny_mutation BEFORE UPDATE OR DELETE ON {table}
                       FOR EACH ROW EXECUTE FUNCTION intelligence_governance_deny_mutation();""")

    op.execute("""
        CREATE OR REPLACE FUNCTION erase_own_mainai_execution_children() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE v_owner_id uuid;
        BEGIN
            v_owner_id := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner_id IS NULL THEN
                RAISE EXCEPTION 'erase_own_mainai_execution_children requires an authenticated app.current_user_id session context.';
            END IF;
            PERFORM set_config('app.mainai_execution_erasure_in_progress', 'on', true);
            DELETE FROM public.intelligence_idea_lessons WHERE owner_id = v_owner_id;
            DELETE FROM public.intelligence_idea_links WHERE owner_id = v_owner_id;
            DELETE FROM public.intelligence_ideas WHERE owner_id = v_owner_id;
            DELETE FROM public.intelligence_interpretations WHERE owner_id = v_owner_id;
            DELETE FROM public.intelligence_evidence WHERE owner_id = v_owner_id;
            DELETE FROM public.intelligence_executions WHERE owner_id = v_owner_id;
            DELETE FROM public.mainai_checkpoints WHERE owner_id = v_owner_id;
            DELETE FROM public.mainai_task_events WHERE owner_id = v_owner_id;
            DELETE FROM public.mainai_recovery_events WHERE owner_id = v_owner_id;
            DELETE FROM public.mainai_recovery_records WHERE owner_id = v_owner_id;
            DELETE FROM public.mainai_task_worktrees WHERE owner_id = v_owner_id;
        END;
        $$;
        REVOKE ALL ON FUNCTION erase_own_mainai_execution_children() FROM PUBLIC;
    """)


def downgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION erase_own_mainai_execution_children() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE v_owner_id uuid;
        BEGIN
            v_owner_id := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner_id IS NULL THEN
                RAISE EXCEPTION 'erase_own_mainai_execution_children requires an authenticated app.current_user_id session context.';
            END IF;
            PERFORM set_config('app.mainai_execution_erasure_in_progress', 'on', true);
            DELETE FROM public.mainai_checkpoints WHERE owner_id = v_owner_id;
            DELETE FROM public.mainai_task_events WHERE owner_id = v_owner_id;
            DELETE FROM public.mainai_recovery_events WHERE owner_id = v_owner_id;
            DELETE FROM public.mainai_recovery_records WHERE owner_id = v_owner_id;
            DELETE FROM public.mainai_task_worktrees WHERE owner_id = v_owner_id;
        END;
        $$;
        REVOKE ALL ON FUNCTION erase_own_mainai_execution_children() FROM PUBLIC;
    """)
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table};")
    op.execute("DROP FUNCTION intelligence_governance_deny_mutation();")
    op.execute("ALTER TABLE mainai_recovery_records DROP CONSTRAINT uq_mainai_recovery_records_id_owner;")
    op.execute("ALTER TABLE mainai_task_events DROP CONSTRAINT uq_mainai_task_events_id_owner;")
