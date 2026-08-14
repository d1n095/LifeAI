"""Life Active Context Intelligence — deterministic selected-reference foundation."""

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE active_context_sets (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            label varchar(256), anchor_type varchar(48) NOT NULL, anchor_ref varchar(256) NOT NULL,
            subject_basis varchar(24) NOT NULL DEFAULT 'unknown', idempotency_key varchar(128) NOT NULL,
            created_at timestamp NOT NULL DEFAULT now(), refreshed_at timestamp,
            CONSTRAINT uq_active_context_sets_id_owner UNIQUE (id, owner_id),
            CONSTRAINT uq_active_context_sets_idempotency UNIQUE (owner_id, idempotency_key),
            CONSTRAINT ck_active_context_set_basis CHECK
                (subject_basis IN ('deterministic','manual','inferred','unknown')),
            CONSTRAINT ck_active_context_set_anchor CHECK (length(btrim(anchor_ref)) > 0),
            CONSTRAINT ck_active_context_set_anchor_type CHECK (anchor_type IN (
                'conversation','message','document','knowledge_version','knowledge_claim',
                'memory_source_unit','document_source_unit','message_source_unit','mainai_goal',
                'mainai_plan','mainai_task','mainai_job','mainai_checkpoint','mainai_recovery',
                'engineering_lesson','intelligence_execution','intelligence_evidence',
                'intelligence_interpretation','intelligence_idea','project','project_note','explicit_topic'
            )),
            CONSTRAINT ck_active_context_set_anchor_ref_shape CHECK (
                anchor_type = 'explicit_topic' OR anchor_ref ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            )
        );
        CREATE INDEX ix_active_context_sets_owner ON active_context_sets(owner_id);

        CREATE TABLE active_context_members (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            context_set_id uuid NOT NULL, owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            object_type varchar(48) NOT NULL, object_ref varchar(256) NOT NULL,
            inclusion_reason varchar(48) NOT NULL, relevance_basis varchar(24) NOT NULL,
            authority varchar(40) NOT NULL DEFAULT 'unknown', rank integer NOT NULL DEFAULT 0,
            state varchar(24) NOT NULL DEFAULT 'active',
            activation_path jsonb NOT NULL DEFAULT '[]'::jsonb,
            source_provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            added_at timestamp NOT NULL DEFAULT now(), last_activated_at timestamp NOT NULL DEFAULT now(),
            valid_from timestamp, valid_until timestamp, expires_at timestamp,
            CONSTRAINT uq_active_context_members_id_owner UNIQUE (id, owner_id),
            CONSTRAINT uq_active_context_member_ref UNIQUE (context_set_id, object_type, object_ref),
            CONSTRAINT fk_active_context_member_set_owner FOREIGN KEY (context_set_id, owner_id)
                REFERENCES active_context_sets(id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_active_context_member_basis CHECK
                (relevance_basis IN ('deterministic','manual','inferred','unknown')),
            CONSTRAINT ck_active_context_member_authority CHECK
                (authority IN ('founder','repeated_founder_preference','deterministic_source','inferred_pattern','ai_interpretation','unknown')),
            CONSTRAINT ck_active_context_member_state CHECK
                (state IN ('active','pinned','suppressed','stale','superseded')),
            CONSTRAINT ck_active_context_member_rank CHECK (rank >= 0),
            CONSTRAINT ck_active_context_member_path CHECK (jsonb_typeof(activation_path) = 'array'),
            CONSTRAINT ck_active_context_member_provenance CHECK (jsonb_typeof(source_provenance) = 'object'),
            CONSTRAINT ck_active_context_member_validity CHECK
                (valid_from IS NULL OR valid_until IS NULL OR valid_until >= valid_from),
            CONSTRAINT ck_active_context_member_ref_nonempty CHECK (length(btrim(object_ref)) > 0),
            CONSTRAINT ck_active_context_member_object_type CHECK (object_type IN (
                'conversation','message','document','knowledge_version','knowledge_claim',
                'memory_source_unit','document_source_unit','message_source_unit','mainai_goal',
                'mainai_plan','mainai_task','mainai_job','mainai_checkpoint','mainai_recovery',
                'engineering_lesson','intelligence_execution','intelligence_evidence',
                'intelligence_interpretation','intelligence_idea','project','project_note'
            )),
            CONSTRAINT ck_active_context_member_ref_shape CHECK (
                object_ref ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            )
        );
        CREATE INDEX ix_active_context_members_current
            ON active_context_members(context_set_id, state, rank, added_at);

        CREATE TABLE active_context_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            context_set_id uuid NOT NULL, owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            member_id uuid, action varchar(32) NOT NULL, actor_type varchar(32) NOT NULL,
            detail jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT fk_active_context_event_set_owner FOREIGN KEY (context_set_id, owner_id)
                REFERENCES active_context_sets(id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_active_context_event_member_owner FOREIGN KEY (member_id, owner_id)
                REFERENCES active_context_members(id, owner_id),
            CONSTRAINT ck_active_context_event_action CHECK
                (action IN ('created','refreshed','member_added','pinned','unpinned','suppressed','unsuppressed','state_changed')),
            CONSTRAINT ck_active_context_event_actor CHECK
                (actor_type IN ('founder','system','deterministic_resolver','unknown')),
            CONSTRAINT ck_active_context_event_detail CHECK (jsonb_typeof(detail) = 'object')
        );
        CREATE INDEX ix_active_context_events_set_time ON active_context_events(context_set_id, created_at);
    """)

    for table in ("active_context_sets", "active_context_members", "active_context_events"):
        op.execute(f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY {table}_isolation ON {table}
                USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
                WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
        """)

    op.execute("""
        CREATE FUNCTION active_context_events_deny_update() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION 'active_context_events is append-only: UPDATE is not permitted';
        END;
        $$;
        REVOKE ALL ON FUNCTION active_context_events_deny_update() FROM PUBLIC;
        CREATE TRIGGER trg_active_context_events_deny_update BEFORE UPDATE ON active_context_events
            FOR EACH ROW EXECUTE FUNCTION active_context_events_deny_update();
    """)


def downgrade() -> None:
    op.execute("DROP TABLE active_context_events;")
    op.execute("DROP FUNCTION active_context_events_deny_update();")
    op.execute("DROP TABLE active_context_members;")
    op.execute("DROP TABLE active_context_sets;")
