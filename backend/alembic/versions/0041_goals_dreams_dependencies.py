"""Life goals, dreams, dependencies and feasibility foundation."""

from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE life_intents (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      title varchar(256) NOT NULL, intent_kind varchar(24) NOT NULL DEFAULT 'unknown', state varchar(24) NOT NULL DEFAULT 'unknown',
      classification_basis varchar(24) NOT NULL DEFAULT 'unknown', authority varchar(40) NOT NULL DEFAULT 'unknown',
      provenance jsonb NOT NULL DEFAULT '{}', mainai_goal_id uuid, memory_thread_id uuid, idempotency_key varchar(128) NOT NULL,
      created_at timestamp NOT NULL DEFAULT now(), updated_at timestamp NOT NULL DEFAULT now(), completed_at timestamp,
      CONSTRAINT uq_life_intents_id_owner UNIQUE(id,owner_id), CONSTRAINT uq_life_intents_idem UNIQUE(owner_id,idempotency_key),
      CONSTRAINT fk_life_intent_goal_owner FOREIGN KEY(mainai_goal_id,owner_id) REFERENCES mainai_goals(id,owner_id),
      CONSTRAINT fk_life_intent_thread_owner FOREIGN KEY(memory_thread_id,owner_id) REFERENCES memory_threads(id,owner_id),
      CONSTRAINT ck_life_intent_title CHECK(length(btrim(title))>0),
      CONSTRAINT ck_life_intent_kind CHECK(intent_kind IN ('goal','dream','need','intention','milestone','obligation','unknown')),
      CONSTRAINT ck_life_intent_state CHECK(state IN ('active','blocked','waiting','future','completed','abandoned','superseded','unknown')),
      CONSTRAINT ck_life_intent_basis CHECK(classification_basis IN ('manual','deterministic','inferred','unknown')),
      CONSTRAINT ck_life_intent_authority CHECK(authority IN ('founder','repeated_founder_preference','deterministic_source','inferred_pattern','ai_interpretation','unknown')),
      CONSTRAINT ck_life_intent_provenance CHECK(jsonb_typeof(provenance)='object')
    );
    CREATE TABLE life_intent_blockers (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE, intent_id uuid NOT NULL,
      category varchar(32) NOT NULL DEFAULT 'unknown', status varchar(24) NOT NULL DEFAULT 'active', description text NOT NULL,
      basis varchar(24) NOT NULL DEFAULT 'unknown', reference_kind varchar(48), reference_id varchar(256), provenance jsonb NOT NULL DEFAULT '{}',
      idempotency_key varchar(128) NOT NULL, recorded_at timestamp NOT NULL DEFAULT now(), resolved_at timestamp, resolution_reason text,
      CONSTRAINT uq_life_blockers_id_owner UNIQUE(id,owner_id), CONSTRAINT uq_life_blocker_idem UNIQUE(owner_id,intent_id,idempotency_key),
      CONSTRAINT fk_life_blocker_intent_owner FOREIGN KEY(intent_id,owner_id) REFERENCES life_intents(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT ck_life_blocker_category CHECK(category IN ('money','time','knowledge','permission','person','hardware','software_capability','external_service','external_review','dependency','health','location','legal_regulatory','unknown')),
      CONSTRAINT ck_life_blocker_status CHECK(status IN ('active','resolved','superseded','invalidated')),
      CONSTRAINT ck_life_blocker_basis CHECK(basis IN ('manual','deterministic','inferred','unknown')),
      CONSTRAINT ck_life_blocker_description CHECK(length(btrim(description))>0),
      CONSTRAINT ck_life_blocker_reference CHECK((reference_kind IS NULL)=(reference_id IS NULL) AND (reference_id IS NULL OR reference_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')),
      CONSTRAINT ck_life_blocker_provenance CHECK(jsonb_typeof(provenance)='object')
    );
    CREATE TABLE life_intent_dependencies (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      from_intent_id uuid NOT NULL, to_intent_id uuid NOT NULL, relationship_type varchar(24) NOT NULL,
      basis varchar(24) NOT NULL DEFAULT 'unknown', provenance jsonb NOT NULL DEFAULT '{}', idempotency_key varchar(128) NOT NULL,
      created_at timestamp NOT NULL DEFAULT now(), CONSTRAINT uq_life_dependencies_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_life_dependency UNIQUE(owner_id,from_intent_id,to_intent_id,relationship_type),
      CONSTRAINT uq_life_dependency_idem UNIQUE(owner_id,idempotency_key),
      CONSTRAINT fk_life_dependency_from FOREIGN KEY(from_intent_id,owner_id) REFERENCES life_intents(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_life_dependency_to FOREIGN KEY(to_intent_id,owner_id) REFERENCES life_intents(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT ck_life_dependency_type CHECK(relationship_type IN ('requires','blocks','enables','milestone_of','supports','conflicts_with','supersedes')),
      CONSTRAINT ck_life_dependency_basis CHECK(basis IN ('manual','deterministic','inferred','unknown')),
      CONSTRAINT ck_life_dependency_self CHECK(from_intent_id<>to_intent_id),
      CONSTRAINT ck_life_dependency_provenance CHECK(jsonb_typeof(provenance)='object')
    );
    CREATE TABLE life_intent_events (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      intent_id uuid NOT NULL, blocker_id uuid, event_type varchar(32) NOT NULL, actor_type varchar(24) NOT NULL DEFAULT 'unknown',
      detail jsonb NOT NULL DEFAULT '{}', created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT fk_life_event_intent_owner FOREIGN KEY(intent_id,owner_id) REFERENCES life_intents(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_life_event_blocker_owner FOREIGN KEY(blocker_id,owner_id) REFERENCES life_intent_blockers(id,owner_id),
      CONSTRAINT ck_life_event_type CHECK(event_type IN ('intent_created','state_changed','blocker_added','blocker_resolved','blocker_superseded','blocker_invalidated','dependency_added')),
      CONSTRAINT ck_life_event_actor CHECK(actor_type IN ('founder','system','deterministic_resolver','unknown')),
      CONSTRAINT ck_life_event_detail CHECK(jsonb_typeof(detail)='object')
    );
    CREATE INDEX ix_life_intents_actionable ON life_intents(owner_id,state,updated_at);
    CREATE INDEX ix_life_blockers_active ON life_intent_blockers(intent_id,status);
    CREATE INDEX ix_life_dependencies_from ON life_intent_dependencies(from_intent_id,relationship_type);
    CREATE INDEX ix_life_events_time ON life_intent_events(intent_id,created_at);
    """)
    for table in (
        "life_intents",
        "life_intent_blockers",
        "life_intent_dependencies",
        "life_intent_events",
    ):
        op.execute(
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY; ALTER TABLE {table} FORCE ROW LEVEL SECURITY;"
        )
        op.execute(
            f"CREATE POLICY {table}_isolation ON {table} USING (owner_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid) WITH CHECK (owner_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid);"
        )
    op.execute("""
      CREATE FUNCTION life_intent_append_only_deny_update() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN RAISE EXCEPTION 'life intent evidence is append-only'; END $$;
      REVOKE ALL ON FUNCTION life_intent_append_only_deny_update() FROM PUBLIC;
      CREATE TRIGGER trg_life_dependency_immutable BEFORE UPDATE ON life_intent_dependencies FOR EACH ROW EXECUTE FUNCTION life_intent_append_only_deny_update();
      CREATE TRIGGER trg_life_event_immutable BEFORE UPDATE ON life_intent_events FOR EACH ROW EXECUTE FUNCTION life_intent_append_only_deny_update();
      ALTER TABLE active_context_sets DROP CONSTRAINT ck_active_context_set_anchor_type;
      ALTER TABLE active_context_sets ADD CONSTRAINT ck_active_context_set_anchor_type CHECK(anchor_type IN ('conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit','document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job','mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence','intelligence_interpretation','intelligence_idea','project','project_note','memory_thread','life_intent','life_intent_blocker','explicit_topic'));
      ALTER TABLE active_context_members DROP CONSTRAINT ck_active_context_member_object_type;
      ALTER TABLE active_context_members ADD CONSTRAINT ck_active_context_member_object_type CHECK(object_type IN ('conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit','document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job','mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence','intelligence_interpretation','intelligence_idea','project','project_note','memory_thread','life_intent','life_intent_blocker'));
      ALTER TABLE memory_thread_members DROP CONSTRAINT ck_memory_thread_member_kind;
      ALTER TABLE memory_thread_members ADD CONSTRAINT ck_memory_thread_member_kind CHECK(member_kind IN ('conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit','document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job','mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence','intelligence_interpretation','intelligence_idea','project','project_note','active_context_set','life_intent','life_intent_blocker'));
    """)


def downgrade() -> None:
    op.execute("""
      ALTER TABLE active_context_sets DROP CONSTRAINT ck_active_context_set_anchor_type;
      ALTER TABLE active_context_sets ADD CONSTRAINT ck_active_context_set_anchor_type CHECK(anchor_type IN ('conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit','document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job','mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence','intelligence_interpretation','intelligence_idea','project','project_note','memory_thread','explicit_topic'));
      ALTER TABLE active_context_members DROP CONSTRAINT ck_active_context_member_object_type;
      ALTER TABLE active_context_members ADD CONSTRAINT ck_active_context_member_object_type CHECK(object_type IN ('conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit','document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job','mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence','intelligence_interpretation','intelligence_idea','project','project_note','memory_thread'));
      ALTER TABLE memory_thread_members DROP CONSTRAINT ck_memory_thread_member_kind;
      ALTER TABLE memory_thread_members ADD CONSTRAINT ck_memory_thread_member_kind CHECK(member_kind IN ('conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit','document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job','mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence','intelligence_interpretation','intelligence_idea','project','project_note','active_context_set'));
      DROP TABLE life_intent_events; DROP TABLE life_intent_dependencies; DROP TABLE life_intent_blockers; DROP TABLE life_intents;
      DROP FUNCTION life_intent_append_only_deny_update();
    """)
