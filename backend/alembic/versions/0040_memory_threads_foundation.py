"""Life Memory Threads — cross-conversation continuity foundation."""

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


TYPES = "'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit','document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job','mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence','intelligence_interpretation','intelligence_idea','project','project_note','active_context_set'"


def upgrade() -> None:
    op.execute(f"""
    CREATE TABLE memory_threads (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      manual_label varchar(256), system_label varchar(256), classification_basis varchar(24) NOT NULL DEFAULT 'unknown',
      state varchar(24) NOT NULL DEFAULT 'active', idempotency_key varchar(128) NOT NULL,
      created_at timestamp NOT NULL DEFAULT now(), last_activity_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_memory_threads_id_owner UNIQUE(id, owner_id), CONSTRAINT uq_memory_threads_idem UNIQUE(owner_id,idempotency_key),
      CONSTRAINT ck_memory_thread_basis CHECK(classification_basis IN ('manual','deterministic','inferred','unknown')),
      CONSTRAINT ck_memory_thread_state CHECK(state IN ('active','dormant','completed','superseded','archived')),
      CONSTRAINT ck_memory_thread_label CHECK(manual_label IS NULL OR length(btrim(manual_label)) > 0),
      CONSTRAINT ck_memory_thread_system_label CHECK(system_label IS NULL OR length(btrim(system_label)) > 0)
    );
    CREATE TABLE memory_thread_members (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), thread_id uuid NOT NULL, owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      member_kind varchar(48) NOT NULL, member_ref_id varchar(256) NOT NULL, membership_basis varchar(40) NOT NULL DEFAULT 'unknown',
      classification_basis varchar(24) NOT NULL DEFAULT 'unknown', state varchar(24) NOT NULL DEFAULT 'active', provenance jsonb NOT NULL DEFAULT '{{}}',
      source_occurred_at timestamp, source_time_basis varchar(24) NOT NULL DEFAULT 'unknown', idempotency_key varchar(128),
      added_at timestamp NOT NULL DEFAULT now(), last_seen_at timestamp NOT NULL DEFAULT now(), valid_from timestamp, valid_until timestamp,
      CONSTRAINT uq_memory_thread_members_id_owner UNIQUE(id,owner_id), CONSTRAINT uq_memory_thread_member UNIQUE(thread_id,member_kind,member_ref_id),
      CONSTRAINT uq_memory_thread_member_idem UNIQUE(owner_id,thread_id,idempotency_key),
      CONSTRAINT fk_memory_thread_member_owner FOREIGN KEY(thread_id,owner_id) REFERENCES memory_threads(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT ck_memory_thread_member_kind CHECK(member_kind IN ({TYPES})),
      CONSTRAINT ck_memory_thread_member_ref CHECK(member_ref_id ~* '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'),
      CONSTRAINT ck_memory_thread_membership_basis CHECK(membership_basis IN ('founder_added','deterministic_relationship','imported_structure','same_project','same_goal','continuation','explicit_reference','inferred','unknown')),
      CONSTRAINT ck_memory_thread_member_classification CHECK(classification_basis IN ('manual','deterministic','inferred','unknown')),
      CONSTRAINT ck_memory_thread_member_state CHECK(state IN ('active','inactive','superseded')),
      CONSTRAINT ck_memory_thread_member_provenance CHECK(jsonb_typeof(provenance)='object'),
      CONSTRAINT ck_memory_thread_source_time_basis CHECK(source_time_basis IN ('source','unknown')),
      CONSTRAINT ck_memory_thread_member_validity CHECK(valid_from IS NULL OR valid_until IS NULL OR valid_until >= valid_from)
    );
    CREATE INDEX ix_memory_thread_members_timeline ON memory_thread_members(thread_id,state,source_occurred_at,added_at);
    CREATE TABLE memory_thread_relationships (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      from_thread_id uuid NOT NULL, to_thread_id uuid NOT NULL, relationship_type varchar(24) NOT NULL,
      basis varchar(24) NOT NULL DEFAULT 'unknown', provenance jsonb NOT NULL DEFAULT '{{}}', idempotency_key varchar(128), created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT uq_memory_thread_relationships_id_owner UNIQUE(id,owner_id),
      CONSTRAINT uq_memory_thread_relation UNIQUE(owner_id,from_thread_id,to_thread_id,relationship_type),
      CONSTRAINT uq_memory_thread_relation_idem UNIQUE(owner_id,idempotency_key),
      CONSTRAINT fk_memory_thread_relation_from FOREIGN KEY(from_thread_id,owner_id) REFERENCES memory_threads(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_memory_thread_relation_to FOREIGN KEY(to_thread_id,owner_id) REFERENCES memory_threads(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT ck_memory_thread_relation_type CHECK(relationship_type IN ('related','parent','child','branch','continuation','supersedes','merged_into')),
      CONSTRAINT ck_memory_thread_relation_basis CHECK(basis IN ('manual','deterministic','inferred','unknown')),
      CONSTRAINT ck_memory_thread_relation_self CHECK(from_thread_id <> to_thread_id), CONSTRAINT ck_memory_thread_relation_provenance CHECK(jsonb_typeof(provenance)='object')
    );
    CREATE TABLE memory_thread_events (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), thread_id uuid NOT NULL, owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      member_id uuid, relationship_id uuid, event_type varchar(32) NOT NULL, actor_type varchar(24) NOT NULL DEFAULT 'unknown',
      detail jsonb NOT NULL DEFAULT '{{}}', created_at timestamp NOT NULL DEFAULT now(),
      CONSTRAINT fk_memory_thread_event_owner FOREIGN KEY(thread_id,owner_id) REFERENCES memory_threads(id,owner_id) ON DELETE CASCADE,
      CONSTRAINT fk_memory_thread_event_member FOREIGN KEY(member_id,owner_id) REFERENCES memory_thread_members(id,owner_id),
      CONSTRAINT fk_memory_thread_event_relation FOREIGN KEY(relationship_id,owner_id) REFERENCES memory_thread_relationships(id,owner_id),
      CONSTRAINT ck_memory_thread_event_type CHECK(event_type IN ('thread_created','member_added','member_deactivated','label_changed','state_changed','relation_added','merged','branched','superseded')),
      CONSTRAINT ck_memory_thread_event_actor CHECK(actor_type IN ('founder','system','deterministic_resolver','unknown')),
      CONSTRAINT ck_memory_thread_event_detail CHECK(jsonb_typeof(detail)='object')
    );
    CREATE INDEX ix_memory_thread_events_time ON memory_thread_events(thread_id,created_at);
    """)
    for table in (
        "memory_threads",
        "memory_thread_members",
        "memory_thread_relationships",
        "memory_thread_events",
    ):
        op.execute(
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY; ALTER TABLE {table} FORCE ROW LEVEL SECURITY;"
        )
        op.execute(
            f"CREATE POLICY {table}_isolation ON {table} USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid) WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);"
        )
    op.execute("""
      CREATE FUNCTION memory_thread_immutable_deny_update() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN RAISE EXCEPTION 'memory thread audit/relationship is append-only'; END $$;
      REVOKE ALL ON FUNCTION memory_thread_immutable_deny_update() FROM PUBLIC;
      CREATE TRIGGER trg_memory_thread_relationship_immutable BEFORE UPDATE ON memory_thread_relationships FOR EACH ROW EXECUTE FUNCTION memory_thread_immutable_deny_update();
      CREATE TRIGGER trg_memory_thread_event_immutable BEFORE UPDATE ON memory_thread_events FOR EACH ROW EXECUTE FUNCTION memory_thread_immutable_deny_update();
      ALTER TABLE active_context_sets DROP CONSTRAINT ck_active_context_set_anchor_type;
      ALTER TABLE active_context_sets ADD CONSTRAINT ck_active_context_set_anchor_type CHECK(anchor_type IN ('conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit','document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job','mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence','intelligence_interpretation','intelligence_idea','project','project_note','memory_thread','explicit_topic'));
      ALTER TABLE active_context_members DROP CONSTRAINT ck_active_context_member_object_type;
      ALTER TABLE active_context_members ADD CONSTRAINT ck_active_context_member_object_type CHECK(object_type IN ('conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit','document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job','mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence','intelligence_interpretation','intelligence_idea','project','project_note','memory_thread'));
    """)


def downgrade() -> None:
    op.execute("""
      ALTER TABLE active_context_sets DROP CONSTRAINT ck_active_context_set_anchor_type;
      ALTER TABLE active_context_sets ADD CONSTRAINT ck_active_context_set_anchor_type CHECK(anchor_type IN ('conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit','document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job','mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence','intelligence_interpretation','intelligence_idea','project','project_note','explicit_topic'));
      ALTER TABLE active_context_members DROP CONSTRAINT ck_active_context_member_object_type;
      ALTER TABLE active_context_members ADD CONSTRAINT ck_active_context_member_object_type CHECK(object_type IN ('conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit','document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job','mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence','intelligence_interpretation','intelligence_idea','project','project_note'));
      DROP TABLE memory_thread_events; DROP TABLE memory_thread_relationships; DROP TABLE memory_thread_members; DROP TABLE memory_threads;
      DROP FUNCTION memory_thread_immutable_deny_update();
    """)
