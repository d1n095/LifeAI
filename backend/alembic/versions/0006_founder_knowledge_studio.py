"""Founder Knowledge Studio v1: extends documents with checksum/media_type/classification/
active_truth_status/project_id/soft-delete, enables RLS on documents (previously
intentionally shared, see app/rls.py's old docstring — now owner-scoped, since Founder
Knowledge Studio's per-document access model is meant to survive into the future UserAI
phase where "shared" would be wrong by default), and adds three new tables: knowledge_versions,
knowledge_import_jobs, source_relationships. See docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md.

RLS enable/FORCE/policy statements mirror the document_chunks pattern from migration 0004 —
app/rls.py's apply_rls() is extended to match and re-applies these idempotently on every
startup as a safety net, but this migration is what actually owns the schema.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-20
"""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- documents: new Founder Knowledge Studio columns ---
    op.execute("""
        ALTER TABLE documents
            ADD COLUMN checksum varchar(64),
            ADD COLUMN media_type varchar(96),
            ADD COLUMN original_filename varchar(512),
            ADD COLUMN classification varchar(32) NOT NULL DEFAULT 'general',
            ADD COLUMN active_truth_status varchar(32) NOT NULL DEFAULT 'active',
            ADD COLUMN project_id uuid REFERENCES projects(id),
            ADD COLUMN version_number integer NOT NULL DEFAULT 1,
            ADD COLUMN imported_at timestamp without time zone,
            ADD COLUMN deleted_at timestamp without time zone;

        CREATE INDEX ix_documents_checksum ON documents USING btree (checksum);
    """)

    # Separate statement: ALTER TYPE ... ADD VALUE must not be combined in the same
    # transaction as anything that *uses* the new value (see migration 0005's docstring) —
    # kept as its own op.execute() so it's unambiguous nothing above or below touches
    # 'zip_import' in this same migration.
    op.execute("ALTER TYPE documentsource ADD VALUE IF NOT EXISTS 'zip_import';")

    # --- knowledge_import_jobs (created before documents.import_job_id FK can point to it) ---
    op.execute("""
        CREATE TABLE knowledge_import_jobs (
            id uuid NOT NULL PRIMARY KEY,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            project_id uuid REFERENCES projects(id),
            status varchar(16) NOT NULL DEFAULT 'pending',
            source_filename varchar(512),
            source_checksum varchar(64),
            progress_current integer NOT NULL DEFAULT 0,
            progress_total integer NOT NULL DEFAULT 0,
            succeeded_count integer NOT NULL DEFAULT 0,
            failed_count integer NOT NULL DEFAULT 0,
            skipped_count integer NOT NULL DEFAULT 0,
            failure_reason text,
            manifest jsonb,
            file_results jsonb,
            started_at timestamp without time zone,
            completed_at timestamp without time zone,
            created_at timestamp without time zone NOT NULL,
            updated_at timestamp without time zone NOT NULL
        );
        CREATE INDEX ix_knowledge_import_jobs_owner_id ON knowledge_import_jobs USING btree (owner_id);
        CREATE INDEX ix_knowledge_import_jobs_source_checksum ON knowledge_import_jobs USING btree (source_checksum);

        ALTER TABLE knowledge_import_jobs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE knowledge_import_jobs FORCE ROW LEVEL SECURITY;
        CREATE POLICY knowledge_import_jobs_isolation ON knowledge_import_jobs
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    op.execute("""
        ALTER TABLE documents ADD COLUMN import_job_id uuid REFERENCES knowledge_import_jobs(id);
    """)

    # --- documents: enable RLS now that every row has a real owner (uploaded_by) in
    # practice — see app/models/document.py's docstring update. NULL uploaded_by rows
    # (should not exist for real uploads, guarded in app/rag/ingest.py) simply become
    # invisible to everyone, which is the correct fail-closed behavior, not a regression.
    op.execute("""
        ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
        ALTER TABLE documents FORCE ROW LEVEL SECURITY;
        CREATE POLICY documents_isolation ON documents
        USING (uploaded_by = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (uploaded_by = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    # --- knowledge_versions ---
    op.execute("""
        CREATE TABLE knowledge_versions (
            id uuid NOT NULL PRIMARY KEY,
            source_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            version_number integer NOT NULL,
            checksum varchar(64) NOT NULL,
            extraction_version varchar(32) NOT NULL,
            raw_metadata jsonb,
            created_at timestamp without time zone NOT NULL
        );
        CREATE INDEX ix_knowledge_versions_source_id ON knowledge_versions USING btree (source_id);
        CREATE INDEX ix_knowledge_versions_owner_id ON knowledge_versions USING btree (owner_id);

        ALTER TABLE knowledge_versions ENABLE ROW LEVEL SECURITY;
        ALTER TABLE knowledge_versions FORCE ROW LEVEL SECURITY;
        CREATE POLICY knowledge_versions_isolation ON knowledge_versions
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    # --- source_relationships ---
    op.execute("""
        CREATE TABLE source_relationships (
            id uuid NOT NULL PRIMARY KEY,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            from_source_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            to_source_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            relationship_type varchar(32) NOT NULL,
            note text,
            created_at timestamp without time zone NOT NULL
        );
        CREATE INDEX ix_source_relationships_owner_id ON source_relationships USING btree (owner_id);
        CREATE INDEX ix_source_relationships_from_source_id ON source_relationships USING btree (from_source_id);
        CREATE INDEX ix_source_relationships_to_source_id ON source_relationships USING btree (to_source_id);

        ALTER TABLE source_relationships ENABLE ROW LEVEL SECURITY;
        ALTER TABLE source_relationships FORCE ROW LEVEL SECURITY;
        CREATE POLICY source_relationships_isolation ON source_relationships
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE source_relationships;")
    op.execute("DROP TABLE knowledge_versions;")
    op.execute("""
        ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_isolation;
        DROP POLICY IF EXISTS documents_isolation ON documents;
        ALTER TABLE documents DISABLE ROW LEVEL SECURITY;
    """)
    op.execute("ALTER TABLE documents DROP COLUMN import_job_id;")
    op.execute("DROP TABLE knowledge_import_jobs;")
    op.execute("""
        ALTER TABLE documents
            DROP COLUMN checksum,
            DROP COLUMN media_type,
            DROP COLUMN original_filename,
            DROP COLUMN classification,
            DROP COLUMN active_truth_status,
            DROP COLUMN project_id,
            DROP COLUMN version_number,
            DROP COLUMN imported_at,
            DROP COLUMN deleted_at;
    """)
    # Not removing 'zip_import' from the documentsource enum — Postgres has no ALTER TYPE
    # ... DROP VALUE (see migration 0005's downgrade for the full explanation of why, and the
    # recreate-the-type workaround). No documents.source value ever gets set to 'zip_import'
    # again after this downgrade runs (the app code that would set it is downgraded too), so
    # leaving the unused enum label behind is harmless — recreating the type here would need
    # the same "reassign any row using it first" dance 0005 does, which is not worth the risk
    # for a purely additive, never-again-written enum value.
