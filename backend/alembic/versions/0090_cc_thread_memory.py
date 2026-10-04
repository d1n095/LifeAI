"""Owner-scoped continuous-conversation memory, erasure, and workspace leases.

Revision ID: 0090_cc_thread_memory
Revises: 0089_continuous_conversation
Create Date: 2026-10-04

Alembic note: `alembic_version.version_num` is varchar(32). This child of
`0089_continuous_conversation` must stay uniquely named: the cousin founder-
sovereignty lane already used `0090_founder_sovereignty` on a different head.
"""

from alembic import op

revision = "0090_cc_thread_memory"
down_revision = "0089_continuous_conversation"
branch_labels = None
depends_on = None

OWNER_SCOPED_TABLES = (
    "founder_conversation_compactions",
    "founder_conversation_decisions",
    "founder_conversation_provenance",
    "founder_workspace_leases",
)


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE conversations
            ADD CONSTRAINT uq_conversations_id_user UNIQUE (id, user_id);

        ALTER TABLE founder_canonical_conversations
            DROP CONSTRAINT founder_canonical_conversations_conversation_id_fkey;
        ALTER TABLE founder_canonical_conversations
            ADD CONSTRAINT founder_canonical_conversations_conversation_owner_fkey
            FOREIGN KEY (conversation_id, owner_id)
            REFERENCES conversations (id, user_id)
            ON DELETE RESTRICT;

        ALTER TABLE founder_conversation_events
            DROP CONSTRAINT founder_conversation_events_conversation_id_fkey;
        ALTER TABLE founder_conversation_events
            ADD CONSTRAINT founder_conversation_events_conversation_owner_fkey
            FOREIGN KEY (conversation_id, owner_id)
            REFERENCES conversations (id, user_id)
            ON DELETE CASCADE;

        CREATE TABLE founder_conversation_compactions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            conversation_id uuid NOT NULL,
            summary_text text NOT NULL DEFAULT '',
            source_message_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            source_provenance jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT founder_conversation_compactions_conversation_owner_fkey
                FOREIGN KEY (conversation_id, owner_id)
                REFERENCES conversations (id, user_id)
                ON DELETE RESTRICT,
            CONSTRAINT ck_founder_conversation_compactions_ids CHECK (jsonb_typeof(source_message_ids) = 'array'),
            CONSTRAINT ck_founder_conversation_compactions_prov CHECK (jsonb_typeof(source_provenance) = 'array')
        );

        CREATE TABLE founder_conversation_decisions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            conversation_id uuid NOT NULL,
            message_id uuid REFERENCES messages(id) ON DELETE SET NULL,
            topic varchar(128) NOT NULL DEFAULT '',
            statement text NOT NULL DEFAULT '',
            identifiers jsonb NOT NULL DEFAULT '{}'::jsonb,
            superseded boolean NOT NULL DEFAULT false,
            superseded_by uuid REFERENCES founder_conversation_decisions(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT founder_conversation_decisions_conversation_owner_fkey
                FOREIGN KEY (conversation_id, owner_id)
                REFERENCES conversations (id, user_id)
                ON DELETE RESTRICT,
            CONSTRAINT ck_founder_conversation_decisions_identifiers CHECK (jsonb_typeof(identifiers) = 'object')
        );

        CREATE TABLE founder_conversation_provenance (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            conversation_id uuid NOT NULL,
            message_id uuid REFERENCES messages(id) ON DELETE SET NULL,
            kind varchar(32) NOT NULL DEFAULT '',
            value text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT founder_conversation_provenance_conversation_owner_fkey
                FOREIGN KEY (conversation_id, owner_id)
                REFERENCES conversations (id, user_id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_founder_conversation_provenance_pointer
                UNIQUE (owner_id, message_id, kind, value)
        );

        CREATE TABLE founder_workspace_leases (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            agent_key varchar(64) NOT NULL DEFAULT '',
            branch varchar(256) NOT NULL DEFAULT '',
            task_id uuid,
            execution_id uuid,
            worktree_path text NOT NULL DEFAULT '',
            mutability varchar(32) NOT NULL DEFAULT 'mutable_builder',
            shared_sha varchar(64),
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_founder_workspace_mutability
                CHECK (mutability IN ('mutable_builder', 'read_only_examiner')),
            CONSTRAINT uq_founder_workspace_worktree UNIQUE (worktree_path)
        );
        CREATE UNIQUE INDEX uq_founder_workspace_mutable_branch
            ON founder_workspace_leases (branch)
            WHERE mutability = 'mutable_builder';
        """
    )
    for table in OWNER_SCOPED_TABLES:
        op.execute(
            f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY {table}_isolation ON {table}
                USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
                WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
            """
        )
    op.execute(
        """
        CREATE FUNCTION founder_workspace_leases_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF NEW.mutability = 'mutable_builder' AND EXISTS (
                SELECT 1 FROM public.founder_workspace_leases
                WHERE branch = NEW.branch
                  AND mutability = 'read_only_examiner'
                  AND id IS DISTINCT FROM NEW.id
            ) THEN
                RAISE EXCEPTION 'workspace sharing forbidden: branch % is under examination', NEW.branch;
            END IF;
            IF NEW.mutability = 'read_only_examiner' AND EXISTS (
                SELECT 1 FROM public.founder_workspace_leases
                WHERE branch = NEW.branch
                  AND mutability = 'mutable_builder'
                  AND id IS DISTINCT FROM NEW.id
            ) THEN
                RAISE EXCEPTION 'workspace sharing forbidden: branch % has a mutable builder workspace', NEW.branch;
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER trg_founder_workspace_leases_guard
            BEFORE INSERT OR UPDATE ON founder_workspace_leases
            FOR EACH ROW EXECUTE FUNCTION founder_workspace_leases_guard();

        CREATE FUNCTION founder_canonical_conversations_guard_delete() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF current_setting('app.continuous_conversation_erasure_in_progress', true) IS DISTINCT FROM 'true' THEN
                RAISE EXCEPTION 'canonical conversation binding delete is governed';
            END IF;
            RETURN OLD;
        END;
        $$;
        CREATE TRIGGER trg_founder_canonical_conversations_guard_delete
            BEFORE DELETE ON founder_canonical_conversations
            FOR EACH ROW EXECUTE FUNCTION founder_canonical_conversations_guard_delete();

        CREATE FUNCTION conversations_guard_canonical_delete() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.founder_canonical_conversations
                WHERE conversation_id = OLD.id AND owner_id = OLD.user_id
            ) AND current_setting('app.continuous_conversation_erasure_in_progress', true) IS DISTINCT FROM 'true' THEN
                RAISE EXCEPTION 'deleting a canonical conversation is governed';
            END IF;
            RETURN OLD;
        END;
        $$;
        CREATE TRIGGER trg_conversations_guard_canonical_delete
            BEFORE DELETE ON conversations
            FOR EACH ROW EXECUTE FUNCTION conversations_guard_canonical_delete();

        CREATE FUNCTION erase_own_continuous_conversation_children() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE v_owner_id uuid;
        BEGIN
            v_owner_id := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner_id IS NULL THEN
                RAISE EXCEPTION 'erase_own_continuous_conversation_children requires an authenticated app.current_user_id session context.';
            END IF;
            PERFORM set_config('app.continuous_conversation_erasure_in_progress', 'true', true);
            DELETE FROM public.founder_conversation_provenance WHERE owner_id = v_owner_id;
            DELETE FROM public.founder_conversation_decisions WHERE owner_id = v_owner_id;
            DELETE FROM public.founder_conversation_compactions WHERE owner_id = v_owner_id;
            DELETE FROM public.founder_workspace_leases WHERE owner_id = v_owner_id;
            DELETE FROM public.founder_conversation_events WHERE owner_id = v_owner_id;
            DELETE FROM public.founder_canonical_conversations WHERE owner_id = v_owner_id;
        END;
        $$;
        REVOKE ALL ON FUNCTION founder_workspace_leases_guard() FROM PUBLIC;
        REVOKE ALL ON FUNCTION founder_canonical_conversations_guard_delete() FROM PUBLIC;
        REVOKE ALL ON FUNCTION conversations_guard_canonical_delete() FROM PUBLIC;
        REVOKE ALL ON FUNCTION erase_own_continuous_conversation_children() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION erase_own_continuous_conversation_children() TO mainai_app;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS erase_own_continuous_conversation_children();")
    op.execute("DROP TRIGGER IF EXISTS trg_conversations_guard_canonical_delete ON conversations;")
    op.execute("DROP FUNCTION IF EXISTS conversations_guard_canonical_delete();")
    op.execute("DROP TRIGGER IF EXISTS trg_founder_canonical_conversations_guard_delete ON founder_canonical_conversations;")
    op.execute("DROP FUNCTION IF EXISTS founder_canonical_conversations_guard_delete();")
    op.execute("DROP TRIGGER IF EXISTS trg_founder_workspace_leases_guard ON founder_workspace_leases;")
    op.execute("DROP FUNCTION IF EXISTS founder_workspace_leases_guard();")
    for table in reversed(OWNER_SCOPED_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute(
        """
        ALTER TABLE founder_conversation_events
            DROP CONSTRAINT IF EXISTS founder_conversation_events_conversation_owner_fkey;
        ALTER TABLE founder_conversation_events
            ADD CONSTRAINT founder_conversation_events_conversation_id_fkey
            FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE;
        ALTER TABLE founder_canonical_conversations
            DROP CONSTRAINT IF EXISTS founder_canonical_conversations_conversation_owner_fkey;
        ALTER TABLE founder_canonical_conversations
            ADD CONSTRAINT founder_canonical_conversations_conversation_id_fkey
            FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE RESTRICT;
        ALTER TABLE conversations DROP CONSTRAINT IF EXISTS uq_conversations_id_user;
        """
    )
