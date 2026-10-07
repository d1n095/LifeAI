"""Governed entity registry, erasure receipts, owner-bound refs, compaction checkpoints.

Revision ID: 0091_cc_p1_authority
Revises: 0090_cc_thread_memory
Create Date: 2026-10-07

Isolated continuous-conversation child of `0090_cc_thread_memory`. Do not compose or
renumber against the founder-sovereignty sibling head (`0090_founder_sovereignty` →
`0091_fs_p1_authority`). Alembic `version_num` is varchar(32).
"""

from alembic import op

revision = "0091_cc_p1_authority"
down_revision = "0090_cc_thread_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE messages
            ADD CONSTRAINT uq_messages_id_conversation UNIQUE (id, conversation_id);

        ALTER TABLE founder_conversation_provenance
            DROP CONSTRAINT IF EXISTS founder_conversation_provenance_message_id_fkey;
        ALTER TABLE founder_conversation_provenance
            ADD CONSTRAINT founder_conversation_provenance_message_conversation_fkey
            FOREIGN KEY (message_id, conversation_id)
            REFERENCES messages (id, conversation_id)
            ON DELETE RESTRICT;

        ALTER TABLE founder_conversation_decisions
            DROP CONSTRAINT IF EXISTS founder_conversation_decisions_message_id_fkey;
        ALTER TABLE founder_conversation_decisions
            ADD CONSTRAINT founder_conversation_decisions_message_conversation_fkey
            FOREIGN KEY (message_id, conversation_id)
            REFERENCES messages (id, conversation_id)
            ON DELETE RESTRICT;

        ALTER TABLE founder_conversation_decisions
            ADD COLUMN IF NOT EXISTS decision_key varchar(128) NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS value text NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS status varchar(32) NOT NULL DEFAULT 'active',
            ADD COLUMN IF NOT EXISTS effective_at timestamptz NOT NULL DEFAULT now(),
            ADD COLUMN IF NOT EXISTS source_turn_id uuid;
        ALTER TABLE founder_conversation_decisions
            DROP CONSTRAINT IF EXISTS ck_founder_conversation_decision_status;
        ALTER TABLE founder_conversation_decisions
            ADD CONSTRAINT ck_founder_conversation_decision_status
            CHECK (status IN ('active', 'historical'));
        UPDATE founder_conversation_decisions
            SET decision_key = CASE WHEN decision_key = '' THEN topic ELSE decision_key END,
                status = CASE WHEN superseded THEN 'historical' ELSE 'active' END,
                source_turn_id = COALESCE(source_turn_id, message_id),
                effective_at = COALESCE(effective_at, created_at);

        ALTER TABLE founder_conversation_compactions
            ADD COLUMN IF NOT EXISTS layer varchar(16) NOT NULL DEFAULT 'l0',
            ADD COLUMN IF NOT EXISTS superseded boolean NOT NULL DEFAULT false,
            ADD COLUMN IF NOT EXISTS superseded_by uuid,
            ADD COLUMN IF NOT EXISTS covering_through_message_id uuid,
            ADD COLUMN IF NOT EXISTS covering_through_created_at timestamptz;
        ALTER TABLE founder_conversation_compactions
            DROP CONSTRAINT IF EXISTS ck_founder_conversation_compaction_layer;
        ALTER TABLE founder_conversation_compactions
            ADD CONSTRAINT ck_founder_conversation_compaction_layer
            CHECK (layer IN ('l0', 'l1'));

        CREATE TABLE founder_conversation_checkpoints (
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            conversation_id uuid NOT NULL,
            last_processed_message_id uuid,
            last_processed_created_at timestamptz,
            last_processed_count integer NOT NULL DEFAULT 0,
            messages_scanned integer NOT NULL DEFAULT 0,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (owner_id, conversation_id),
            CONSTRAINT founder_conversation_checkpoints_conversation_owner_fkey
                FOREIGN KEY (conversation_id, owner_id)
                REFERENCES conversations (id, user_id)
                ON DELETE RESTRICT
        );

        CREATE TABLE governed_entity_records (
            entity_key varchar(128) PRIMARY KEY,
            repository varchar(256) NOT NULL,
            branch varchar(256) NOT NULL DEFAULT '',
            artifact_role varchar(64) NOT NULL,
            state varchar(64) NOT NULL,
            source varchar(128) NOT NULL,
            detail text NOT NULL DEFAULT '',
            observed_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_governed_entity_artifact_role
                CHECK (artifact_role IN (
                    'frozen_candidate', 'parent_sha', 'current_branch_tip',
                    'examined_lane_sha', 'unspecified'
                ))
        );

        CREATE TABLE governed_artifact_certifications (
            entity_key varchar(128) PRIMARY KEY
                REFERENCES governed_entity_records(entity_key) ON DELETE CASCADE,
            sha varchar(40) NOT NULL,
            source varchar(128) NOT NULL,
            certified_at timestamptz NOT NULL,
            immutable boolean NOT NULL DEFAULT true,
            detail text NOT NULL DEFAULT '',
            CONSTRAINT ck_governed_artifact_sha CHECK (sha ~ '^[0-9a-f]{40}$')
        );

        CREATE TABLE governed_repository_observations (
            repository varchar(256) NOT NULL,
            branch varchar(256) NOT NULL,
            sha varchar(40) NOT NULL,
            source varchar(128) NOT NULL,
            observed_at timestamptz NOT NULL,
            PRIMARY KEY (repository, branch),
            CONSTRAINT ck_governed_observation_sha CHECK (sha ~ '^[0-9a-f]{40}$')
        );

        ALTER TABLE founder_conversation_checkpoints ENABLE ROW LEVEL SECURITY;
        ALTER TABLE founder_conversation_checkpoints FORCE ROW LEVEL SECURITY;
        CREATE POLICY founder_conversation_checkpoints_isolation ON founder_conversation_checkpoints
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION restore_governed_entity_registry_seed() RETURNS void
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            INSERT INTO public.governed_entity_records
                (entity_key, repository, branch, artifact_role, state, source, detail, observed_at)
            VALUES
                (
                    'founder_alpha_frozen',
                    'd1n095/LifeAI',
                    'codex/founder-alpha-final-composed-candidate',
                    'frozen_candidate',
                    'frozen',
                    'governed_artifact_certification',
                    'Frozen Founder Alpha candidate. Not the current checkout and not a later child tip.',
                    TIMESTAMPTZ '2026-10-03 00:00:00+00'
                ),
                (
                    'continuous_conversation_parent',
                    'd1n095/LifeAI',
                    'cursor/mainai-continuous-conversation-foundation',
                    'parent_sha',
                    'parent',
                    'governed_artifact_certification',
                    'Continuous Conversation foundation parent. Not Founder Alpha and not the P1 child.',
                    TIMESTAMPTZ '2026-10-03 00:00:00+00'
                ),
                (
                    'founder_sovereignty',
                    'd1n095/LifeAI',
                    'cursor/mainai-founder-sovereignty-family-delegation',
                    'examined_lane_sha',
                    'frozen_lane',
                    'governed_artifact_certification',
                    'Founder Sovereignty examined SHA. Workspace sharing with this lane is forbidden.',
                    TIMESTAMPTZ '2026-10-03 00:00:00+00'
                ),
                (
                    'continuous_conversation_p1',
                    'd1n095/LifeAI',
                    'cursor/mainai-continuous-conversation-p1-fix',
                    'current_branch_tip',
                    'current_child',
                    'github_ref',
                    'Current Continuous Conversation P1 fix branch tip. Resolved from GitHub, never from checkout.',
                    NULL
                )
            ON CONFLICT (entity_key) DO UPDATE SET
                repository = EXCLUDED.repository,
                branch = EXCLUDED.branch,
                artifact_role = EXCLUDED.artifact_role,
                state = EXCLUDED.state,
                source = EXCLUDED.source,
                detail = EXCLUDED.detail,
                updated_at = now();

            INSERT INTO public.governed_artifact_certifications
                (entity_key, sha, source, certified_at, immutable, detail)
            VALUES
                (
                    'founder_alpha_frozen',
                    '2fbe20aacf1203fc0e16d216ef55b666b2181619',
                    'founder_alpha_certification',
                    TIMESTAMPTZ '2026-10-03 00:00:00+00',
                    true,
                    'Immutable Founder Alpha frozen-candidate certification.'
                ),
                (
                    'continuous_conversation_parent',
                    '691490edd82fa4bff6188f4f038f7579ee4f3df5',
                    'continuous_conversation_parent_certification',
                    TIMESTAMPTZ '2026-10-03 00:00:00+00',
                    true,
                    'Certified Continuous Conversation foundation parent artifact.'
                ),
                (
                    'founder_sovereignty',
                    'ffbdb6328b8ff594caf2eea71c77c32ef96e516b',
                    'founder_sovereignty_certification',
                    TIMESTAMPTZ '2026-10-03 00:00:00+00',
                    true,
                    'Certified Founder Sovereignty examined artifact as of this isolated lane fork.'
                )
            ON CONFLICT (entity_key) DO UPDATE SET
                sha = EXCLUDED.sha,
                source = EXCLUDED.source,
                certified_at = EXCLUDED.certified_at,
                immutable = EXCLUDED.immutable,
                detail = EXCLUDED.detail;
        END;
        $$;

        SELECT public.restore_governed_entity_registry_seed();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION continuous_conversation_erasure_authorized()
        RETURNS boolean
        LANGUAGE plpgsql
        STABLE
        SET search_path = pg_catalog
        AS $$
        DECLARE
            v_owner_id uuid := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
        BEGIN
            IF v_owner_id IS NULL THEN
                RETURN false;
            END IF;
            RETURN EXISTS (
                SELECT 1
                FROM public.account_erasure_operations op
                JOIN public.account_erasure_reauth_receipts rr
                    ON rr.receipt_id = op.reauth_receipt_id
                WHERE op.owner_id = v_owner_id
                  AND op.status = 'active'
                  AND op.phase IN ('personal_recall_erasure', 'personal_data_erasure')
                  AND rr.owner_id = v_owner_id
                  AND rr.consumed_by_operation_id = op.operation_id
                  AND rr.consumed_at IS NOT NULL
                  AND public.account_erasure_receipt_session_current(v_owner_id, rr.access_jti)
            );
        END;
        $$;

        CREATE OR REPLACE FUNCTION erase_own_continuous_conversation_children() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE v_owner_id uuid;
        BEGIN
            v_owner_id := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner_id IS NULL THEN
                RAISE EXCEPTION 'erase_own_continuous_conversation_children requires an authenticated app.current_user_id session context.';
            END IF;
            IF NOT public.continuous_conversation_erasure_authorized() THEN
                RAISE EXCEPTION 'erase_own_continuous_conversation_children requires a governed account-erasure operation with current reauth receipt';
            END IF;
            PERFORM set_config('app.continuous_conversation_erasure_in_progress', 'true', true);
            DELETE FROM public.founder_conversation_checkpoints WHERE owner_id = v_owner_id;
            DELETE FROM public.founder_conversation_provenance WHERE owner_id = v_owner_id;
            DELETE FROM public.founder_conversation_decisions WHERE owner_id = v_owner_id;
            DELETE FROM public.founder_conversation_compactions WHERE owner_id = v_owner_id;
            DELETE FROM public.founder_workspace_leases WHERE owner_id = v_owner_id;
            DELETE FROM public.founder_conversation_events WHERE owner_id = v_owner_id;
            DELETE FROM public.founder_canonical_conversations WHERE owner_id = v_owner_id;
        END;
        $$;

        CREATE OR REPLACE FUNCTION founder_canonical_conversations_guard_delete() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF NOT public.continuous_conversation_erasure_authorized() THEN
                RAISE EXCEPTION 'canonical conversation binding delete is governed';
            END IF;
            RETURN OLD;
        END;
        $$;

        CREATE OR REPLACE FUNCTION conversations_guard_canonical_delete() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.founder_canonical_conversations
                WHERE conversation_id = OLD.id AND owner_id = OLD.user_id
            ) AND NOT public.continuous_conversation_erasure_authorized() THEN
                RAISE EXCEPTION 'deleting a canonical conversation is governed';
            END IF;
            RETURN OLD;
        END;
        $$;

        REVOKE ALL ON FUNCTION restore_governed_entity_registry_seed() FROM PUBLIC;
        REVOKE ALL ON FUNCTION continuous_conversation_erasure_authorized() FROM PUBLIC;
        REVOKE ALL ON FUNCTION erase_own_continuous_conversation_children() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION erase_own_continuous_conversation_children() TO mainai_app;
        GRANT EXECUTE ON FUNCTION continuous_conversation_erasure_authorized() TO mainai_app;

        REVOKE ALL ON governed_entity_records FROM PUBLIC;
        REVOKE ALL ON governed_artifact_certifications FROM PUBLIC;
        REVOKE ALL ON governed_repository_observations FROM PUBLIC;
        GRANT SELECT, INSERT, UPDATE ON governed_entity_records TO mainai_app;
        GRANT SELECT ON governed_artifact_certifications TO mainai_app;
        GRANT SELECT, INSERT, UPDATE ON governed_repository_observations TO mainai_app;
        GRANT SELECT, INSERT, UPDATE ON founder_conversation_checkpoints TO mainai_app;
        GRANT UPDATE ON founder_conversation_compactions TO mainai_app;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS restore_governed_entity_registry_seed();")
    op.execute("DROP FUNCTION IF EXISTS continuous_conversation_erasure_authorized();")
    op.execute("DROP TABLE IF EXISTS governed_repository_observations;")
    op.execute("DROP TABLE IF EXISTS governed_artifact_certifications;")
    op.execute("DROP TABLE IF EXISTS governed_entity_records;")
    op.execute("DROP TABLE IF EXISTS founder_conversation_checkpoints;")
    op.execute(
        """
        ALTER TABLE founder_conversation_compactions
            DROP COLUMN IF EXISTS covering_through_created_at,
            DROP COLUMN IF EXISTS covering_through_message_id,
            DROP COLUMN IF EXISTS superseded_by,
            DROP COLUMN IF EXISTS superseded,
            DROP COLUMN IF EXISTS layer;
        ALTER TABLE founder_conversation_decisions
            DROP COLUMN IF EXISTS source_turn_id,
            DROP COLUMN IF EXISTS effective_at,
            DROP COLUMN IF EXISTS status,
            DROP COLUMN IF EXISTS value,
            DROP COLUMN IF EXISTS decision_key;
        ALTER TABLE founder_conversation_decisions
            DROP CONSTRAINT IF EXISTS founder_conversation_decisions_message_conversation_fkey;
        ALTER TABLE founder_conversation_decisions
            ADD CONSTRAINT founder_conversation_decisions_message_id_fkey
            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE SET NULL;
        ALTER TABLE founder_conversation_provenance
            DROP CONSTRAINT IF EXISTS founder_conversation_provenance_message_conversation_fkey;
        ALTER TABLE founder_conversation_provenance
            ADD CONSTRAINT founder_conversation_provenance_message_id_fkey
            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE SET NULL;
        ALTER TABLE messages DROP CONSTRAINT IF EXISTS uq_messages_id_conversation;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION erase_own_continuous_conversation_children() RETURNS void
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
        CREATE OR REPLACE FUNCTION founder_canonical_conversations_guard_delete() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF current_setting('app.continuous_conversation_erasure_in_progress', true) IS DISTINCT FROM 'true' THEN
                RAISE EXCEPTION 'canonical conversation binding delete is governed';
            END IF;
            RETURN OLD;
        END;
        $$;
        CREATE OR REPLACE FUNCTION conversations_guard_canonical_delete() RETURNS trigger
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
        """
    )
