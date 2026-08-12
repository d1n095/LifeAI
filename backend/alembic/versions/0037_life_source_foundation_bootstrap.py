"""Life Source Foundation Bootstrap — corpus manifest, S1C (messages as memory sources).

Per docs/LIFE_SOURCE_FOUNDATION_BOOTSTRAP.md (founder mandate, 2026-08-11), built as the
narrowly-scoped, AI-independent intake foundation needed before the founder's full external
corpus can be imported and a final Canonical Life Architecture Recovery attempted. This
migration does NOT touch ChatGPT-specific structure (chatgpt_export_imports is deliberately
deferred until a real export sample is available, per the founder's own explicit instruction)
and does NOT change any existing table's data, only adds.

Two independent pieces:

1. **source_import_batches / source_import_batch_failures** — a new, ordinary, owner-scoped
   tracking pair (no immutability machinery needed; this is operational progress data, not
   evidence) that answers "prove the whole corpus was processed" per the bootstrap doc's §E/§P.
2. **S1C — message_source_units** — already fully designed in
   docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8/§8 before this mandate existed ("S1C: kräver
   S1B, egen PR" — S1B/its RLS step are already merged, migrations 0030/0031). This migration
   builds exactly what was already approved in principle: `Message` becomes a second
   `memory_source_units` subtype, following `document_source_units`' exact exclusive-arc/
   composite-FK/immutability pattern (migration 0019) — not a new pattern. The one place this
   subtype is MORE informative than `document_source_units`: a message's `role` column already
   records real authorship (user vs. assistant vs. system) at write time, unlike a document
   (whose `source_role` is permanently `unknown` per S1A's own documented reasoning) — so
   `trg_msgsu_validate_fields` below verifies `source_role` against the message's own `role`
   instead of forcing `unknown`.

`documents.source_import_batch_id` is a third, independent, nullable addition — links an
ingested document back to the corpus-import batch it arrived in, for completeness accounting.
Existing rows get NULL (unaffected — they were never part of a tracked batch).

No existing table's CHECK constraints, triggers, or RLS policies are altered except
`ck_msu_source_kind` (extended, additively, to allow 'message' alongside the three existing
values — a constraint replace, not a destructive rewrite; every existing row keeps its
existing, still-valid value).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- source_import_batches / source_import_batch_failures ---------------------------
    op.execute("""
        CREATE TABLE source_import_batches (
            id uuid NOT NULL PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id),

            label text NOT NULL,
            source_description text,
            status varchar(16) NOT NULL DEFAULT 'discovering',

            discovered_files integer NOT NULL DEFAULT 0,
            discovered_archives integer NOT NULL DEFAULT 0,
            discovered_conversations integer NOT NULL DEFAULT 0,
            discovered_messages integer NOT NULL DEFAULT 0,
            discovered_attachments integer NOT NULL DEFAULT 0,
            discovered_bytes bigint NOT NULL DEFAULT 0,

            stored_originals_done integer NOT NULL DEFAULT 0,
            stored_originals_total integer NOT NULL DEFAULT 0,
            parsed_done integer NOT NULL DEFAULT 0,
            parsed_total integer NOT NULL DEFAULT 0,

            duplicate_count integer NOT NULL DEFAULT 0,
            failed_count integer NOT NULL DEFAULT 0,
            unsupported_count integer NOT NULL DEFAULT 0,
            semantic_pending_count integer NOT NULL DEFAULT 0,

            created_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz,

            CONSTRAINT ck_sib_status
                CHECK (status IN ('discovering', 'importing', 'completed', 'partial', 'failed')),
            CONSTRAINT ck_sib_counts_non_negative CHECK (
                discovered_files >= 0 AND discovered_archives >= 0 AND discovered_conversations >= 0
                AND discovered_messages >= 0 AND discovered_attachments >= 0 AND discovered_bytes >= 0
                AND stored_originals_done >= 0 AND stored_originals_total >= 0
                AND parsed_done >= 0 AND parsed_total >= 0
                AND duplicate_count >= 0 AND failed_count >= 0
                AND unsupported_count >= 0 AND semantic_pending_count >= 0
            ),
            -- The bootstrap doc's §P completeness proof, enforced structurally rather than
            -- left as an application-level convention: a batch cannot be marked completed
            -- unless every discovered file is accounted for as either stored or failed, and
            -- every stored original is accounted for as parsed, unsupported, or pending.
            CONSTRAINT ck_sib_completed_reconciles CHECK (
                status <> 'completed'
                OR (
                    discovered_files = stored_originals_done + failed_count
                    AND parsed_done + unsupported_count + semantic_pending_count = stored_originals_done
                )
            )
        );
        CREATE INDEX ix_sib_owner ON source_import_batches (owner_id);
    """)

    op.execute("""
        CREATE TABLE source_import_batch_failures (
            id uuid NOT NULL PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id),
            batch_id uuid NOT NULL REFERENCES source_import_batches(id),
            source_ref text NOT NULL,
            reason text NOT NULL,
            retryable boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_sibf_batch ON source_import_batch_failures (batch_id);
    """)

    op.execute("""
        ALTER TABLE source_import_batches ENABLE ROW LEVEL SECURITY;
        ALTER TABLE source_import_batches FORCE ROW LEVEL SECURITY;
        CREATE POLICY source_import_batches_isolation ON source_import_batches
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        ALTER TABLE source_import_batch_failures ENABLE ROW LEVEL SECURITY;
        ALTER TABLE source_import_batch_failures FORCE ROW LEVEL SECURITY;
        CREATE POLICY source_import_batch_failures_isolation ON source_import_batch_failures
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    # --- documents.source_import_batch_id -------------------------------------------------
    op.execute("""
        ALTER TABLE documents
            ADD COLUMN source_import_batch_id uuid REFERENCES source_import_batches(id);
        CREATE INDEX ix_documents_source_import_batch ON documents (source_import_batch_id);
    """)

    # --- S1C: message_source_units ---------------------------------------------------------
    # Extend the parent CHECK additively -- a constraint replace, not a rewrite. Every existing
    # row's source_kind is one of the three original values and remains valid.
    op.execute("""
        ALTER TABLE memory_source_units DROP CONSTRAINT ck_msu_source_kind;
        ALTER TABLE memory_source_units ADD CONSTRAINT ck_msu_source_kind
            CHECK (source_kind IN ('document_chunk', 'document_version', 'document_record', 'message'));
    """)

    # migration 0019's exclusive-arc guarantee (`trg_msu_check_subtype_exists`) was written
    # when `document_source_units` was the ONLY subtype table that could ever exist, and
    # hardcoded that single table name -- a real gap this pass found empirically (a genuine
    # message_source_units INSERT was rejected at commit with "no matching document_source_
    # units row", not a hypothetical). `CREATE OR REPLACE` here is safe: the function's
    # signature/behavior contract (raise unless a matching subtype row exists) is unchanged,
    # only WHICH subtype table it checks now depends on source_kind, exactly the same
    # discriminator document_source_units' own three partial-unique-index arms already use.
    op.execute("""
        CREATE OR REPLACE FUNCTION trg_msu_check_subtype_exists() RETURNS TRIGGER
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF NEW.source_kind IN ('document_chunk', 'document_version', 'document_record') THEN
                IF NOT EXISTS (SELECT 1 FROM public.document_source_units WHERE memory_source_id = NEW.id) THEN
                    RAISE EXCEPTION 'memory_source_units %: no matching document_source_units row', NEW.id;
                END IF;
            ELSIF NEW.source_kind = 'message' THEN
                IF NOT EXISTS (SELECT 1 FROM public.message_source_units WHERE memory_source_id = NEW.id) THEN
                    RAISE EXCEPTION 'memory_source_units %: no matching message_source_units row', NEW.id;
                END IF;
            ELSE
                RAISE EXCEPTION 'memory_source_units %: unrecognized source_kind %', NEW.id, NEW.source_kind;
            END IF;
            RETURN NEW;
        END;
        $$;
    """)

    op.execute("""
        CREATE TABLE message_source_units (
            memory_source_id uuid NOT NULL PRIMARY KEY REFERENCES memory_source_units(id),
            owner_id uuid NOT NULL,
            source_kind varchar(32) NOT NULL,

            message_id uuid NOT NULL REFERENCES messages(id),
            conversation_id uuid NOT NULL REFERENCES conversations(id),

            FOREIGN KEY (memory_source_id, owner_id, source_kind)
                REFERENCES memory_source_units (id, owner_id, source_kind)
        );

        -- Unlike document_source_units' three type-scoped partial indexes, message_id is
        -- always required (messages are never "purged to a degraded locator" the way a
        -- DocumentChunk can be) -- a single plain unique constraint is the correct, simpler
        -- equivalent, not a gap.
        CREATE UNIQUE INDEX uq_msgsu_message ON message_source_units (owner_id, message_id);
        CREATE INDEX ix_msgsu_conversation ON message_source_units (conversation_id);
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION trg_msgsu_validate_fields() RETURNS TRIGGER
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        DECLARE
            v_snap          varchar(16);
            v_identity      text;
            v_role          varchar(16);
            v_content       text;
            v_content_hash  varchar(64);
            v_hash_version  varchar(32);
            v_msg_role      varchar(16);
            v_msg_content   text;
            v_msg_conv      uuid;
            v_conv_owner    uuid;
            v_expected_role varchar(16);
        BEGIN
            SELECT snapshot_status, source_identity_key, source_role, content_text,
                   content_hash, content_hash_version
            INTO v_snap, v_identity, v_role, v_content, v_content_hash, v_hash_version
            FROM public.memory_source_units WHERE id = NEW.memory_source_id;

            IF NEW.source_kind <> 'message' THEN
                RAISE EXCEPTION 'unexpected source_kind % for message_source_units (id=%)', NEW.source_kind, NEW.memory_source_id;
            END IF;

            SELECT role, content, conversation_id INTO v_msg_role, v_msg_content, v_msg_conv
            FROM public.messages WHERE id = NEW.message_id;

            IF v_msg_conv IS NULL THEN
                RAISE EXCEPTION 'message_id % does not exist', NEW.message_id;
            END IF;
            IF v_msg_conv <> NEW.conversation_id THEN
                RAISE EXCEPTION 'conversation_id % does not match message %''s real conversation %', NEW.conversation_id, NEW.message_id, v_msg_conv;
            END IF;

            SELECT user_id INTO v_conv_owner FROM public.conversations WHERE id = NEW.conversation_id;
            IF v_conv_owner IS DISTINCT FROM NEW.owner_id THEN
                RAISE EXCEPTION 'conversation_id % does not belong to owner_id %', NEW.conversation_id, NEW.owner_id;
            END IF;

            -- The one real difference from document_source_units: a message's role is KNOWN,
            -- real authorship recorded at write time (app/routers/chat.py), not an inferred
            -- guess -- so source_role is verified against it exactly, never left 'unknown'.
            v_expected_role := CASE v_msg_role
                WHEN 'user' THEN 'founder'
                WHEN 'assistant' THEN 'assistant'
                WHEN 'system' THEN 'system'
                ELSE NULL
            END;
            IF v_expected_role IS NULL THEN
                RAISE EXCEPTION 'message % has unrecognized role % -- cannot assign source_role', NEW.message_id, v_msg_role;
            END IF;
            IF v_role <> v_expected_role THEN
                RAISE EXCEPTION 'message_source_units: source_role must be % for message % (role=%), got %', v_expected_role, NEW.message_id, v_msg_role, v_role;
            END IF;

            IF TG_OP = 'INSERT' AND v_identity IS DISTINCT FROM ('message:' || NEW.message_id::text) THEN
                RAISE EXCEPTION 'source_identity_key does not match message_id (id=%)', NEW.memory_source_id;
            END IF;

            -- Exact-snapshot verification mirrors document_chunk's own check against
            -- document_chunks.text (migration 0019) -- an 'exact' snapshot must be bound to
            -- the REAL message content, not merely to whatever text a caller submitted.
            IF v_snap = 'exact' THEN
                IF v_msg_content IS DISTINCT FROM v_content THEN
                    RAISE EXCEPTION 'message exact snapshot content_text does not match messages.content (id=%)', NEW.memory_source_id;
                END IF;
                IF v_content_hash IS DISTINCT FROM encode(sha256(convert_to(v_msg_content, 'UTF8')), 'hex') THEN
                    RAISE EXCEPTION 'message exact snapshot content_hash does not match sha256(messages.content) (id=%)', NEW.memory_source_id;
                END IF;
                IF v_hash_version IS DISTINCT FROM 'sha256-utf8-v1' THEN
                    RAISE EXCEPTION 'message exact snapshot content_hash_version must be sha256-utf8-v1 (id=%)', NEW.memory_source_id;
                END IF;
            END IF;

            RETURN NEW;
        END;
        $$;

        CREATE CONSTRAINT TRIGGER trg_msgsu_validate
            AFTER INSERT OR UPDATE ON message_source_units
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION trg_msgsu_validate_fields();

        CREATE OR REPLACE FUNCTION trg_msgsu_guard_update() RETURNS TRIGGER
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF NEW.memory_source_id  IS DISTINCT FROM OLD.memory_source_id
            OR NEW.owner_id          IS DISTINCT FROM OLD.owner_id
            OR NEW.source_kind       IS DISTINCT FROM OLD.source_kind
            OR NEW.message_id        IS DISTINCT FROM OLD.message_id
            OR NEW.conversation_id   IS DISTINCT FROM OLD.conversation_id
            THEN
                RAISE EXCEPTION 'message_source_units: all fields are immutable (id=%)', OLD.memory_source_id;
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_msgsu_update_guard
            BEFORE UPDATE ON message_source_units
            FOR EACH ROW EXECUTE FUNCTION trg_msgsu_guard_update();

        CREATE OR REPLACE FUNCTION trg_msgsu_forbid_delete() RETURNS TRIGGER
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF current_setting('memory.erasure_in_progress', true) IS DISTINCT FROM 'on' THEN
                RAISE EXCEPTION 'message_source_units: row deletion is not permitted outside account erasure (id=%)', OLD.memory_source_id;
            END IF;
            RETURN OLD;
        END;
        $$;

        CREATE TRIGGER trg_msgsu_no_delete
            BEFORE DELETE ON message_source_units
            FOR EACH ROW EXECUTE FUNCTION trg_msgsu_forbid_delete();
    """)

    op.execute("""
        ALTER TABLE message_source_units ENABLE ROW LEVEL SECURITY;
        ALTER TABLE message_source_units FORCE ROW LEVEL SECURITY;
        CREATE POLICY message_source_units_isolation ON message_source_units
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    # This migration does NOT contain literal REVOKE/GRANT naming mainai_app -- same reasoning
    # as migration 0019's own module docstring (a fresh `alembic upgrade head` on a database
    # where mainai_app does not exist yet would fail outright). Runtime privilege narrowing for
    # source_import_batches/source_import_batch_failures/message_source_units, and for
    # documents.storage_key/file_path (immutability, docs/LIFE_SOURCE_FOUNDATION_BOOTSTRAP.md
    # §D), is applied and verified at every boot by
    # backend/scripts/security/s1a_privilege_policy.py via ensure_app_role.py/
    # apply_runtime_privileges.py, exactly like every other S1A-pattern table.


def downgrade() -> None:
    op.execute("""
        DROP TRIGGER IF EXISTS trg_msgsu_no_delete ON message_source_units;
        DROP FUNCTION IF EXISTS trg_msgsu_forbid_delete();
        DROP TRIGGER IF EXISTS trg_msgsu_update_guard ON message_source_units;
        DROP FUNCTION IF EXISTS trg_msgsu_guard_update();
        DROP TRIGGER IF EXISTS trg_msgsu_validate ON message_source_units;
        DROP FUNCTION IF EXISTS trg_msgsu_validate_fields();
        DROP TABLE IF EXISTS message_source_units;

        -- Restore migration 0019's original single-subtype exclusive-arc check exactly, since
        -- message_source_units (the second arm this upgrade() added above) no longer exists
        -- after the DROP TABLE above.
        CREATE OR REPLACE FUNCTION trg_msu_check_subtype_exists() RETURNS TRIGGER
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM public.document_source_units WHERE memory_source_id = NEW.id) THEN
                RAISE EXCEPTION 'memory_source_units %: no matching document_source_units row', NEW.id;
            END IF;
            RETURN NEW;
        END;
        $$;

        ALTER TABLE memory_source_units DROP CONSTRAINT IF EXISTS ck_msu_source_kind;
        ALTER TABLE memory_source_units ADD CONSTRAINT ck_msu_source_kind
            CHECK (source_kind IN ('document_chunk', 'document_version', 'document_record'));

        DROP INDEX IF EXISTS ix_documents_source_import_batch;
        ALTER TABLE documents DROP COLUMN IF EXISTS source_import_batch_id;

        DROP TABLE IF EXISTS source_import_batch_failures;
        DROP TABLE IF EXISTS source_import_batches;
    """)
