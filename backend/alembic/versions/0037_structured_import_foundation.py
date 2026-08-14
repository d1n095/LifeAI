"""ChatGPT Import Foundation — format-agnostic bootstrap.

Adds durable, owner-scoped execution state for generic structured-export adapters. The
canonical archive remains an existing ``documents``/LocalFilesystemStorage object; these
tables contain only checkpoints, per-item outcomes, and exact provenance pointers.

No ChatGPT schema is represented here. The real structural adapter remains blocked on a
real export sample.

Revision ID: 0037
Revises: 0036
Create Date: 2026-08-14
"""

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE documents
            ADD CONSTRAINT uq_documents_id_uploaded_by UNIQUE (id, uploaded_by);

        CREATE TABLE structured_import_runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            job_id uuid NOT NULL UNIQUE,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_document_id uuid NOT NULL,
            adapter_key varchar(128) NOT NULL,
            adapter_version varchar(64) NOT NULL,
            source_checksum varchar(64) NOT NULL,
            checkpoint jsonb NOT NULL DEFAULT '{}'::jsonb,
            discovered_total integer,
            status varchar(16) NOT NULL DEFAULT 'running',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_structured_import_runs_total CHECK (
                discovered_total IS NULL OR discovered_total >= 0
            ),
            CONSTRAINT ck_structured_import_runs_checksum CHECK (
                source_checksum ~ '^[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_structured_import_runs_status CHECK (
                status IN ('running', 'completed', 'cancelled', 'failed')
            ),
            CONSTRAINT fk_structured_import_runs_job_owner
                FOREIGN KEY (job_id, owner_id) REFERENCES mainai_jobs(id, owner_id)
                ON DELETE CASCADE,
            CONSTRAINT fk_structured_import_runs_document_owner
                FOREIGN KEY (source_document_id, owner_id) REFERENCES documents(id, uploaded_by)
                ON DELETE CASCADE,
            CONSTRAINT uq_structured_import_runs_id_owner UNIQUE (id, owner_id)
        );
        CREATE INDEX ix_structured_import_runs_owner ON structured_import_runs(owner_id);

        CREATE TABLE structured_import_items (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id uuid NOT NULL,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_identity text NOT NULL,
            state varchar(16) NOT NULL,
            provenance jsonb NOT NULL,
            checkpoint_after jsonb NOT NULL,
            content_sha256 varchar(64),
            size_bytes bigint,
            failure_code varchar(64),
            retryable boolean NOT NULL DEFAULT false,
            attempt_count integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_structured_import_items_run_owner
                FOREIGN KEY (run_id, owner_id) REFERENCES structured_import_runs(id, owner_id)
                ON DELETE CASCADE,
            CONSTRAINT uq_structured_import_items_identity UNIQUE (run_id, source_identity),
            CONSTRAINT ck_structured_import_items_state CHECK (
                state IN ('discovered', 'stored', 'duplicate', 'parsed', 'unsupported', 'failed', 'deferred')
            ),
            CONSTRAINT ck_structured_import_items_size CHECK (size_bytes IS NULL OR size_bytes >= 0),
            CONSTRAINT ck_structured_import_items_attempts CHECK (attempt_count > 0),
            CONSTRAINT ck_structured_import_items_checksum CHECK (
                content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_structured_import_items_failure CHECK (
                (state = 'failed' AND failure_code IS NOT NULL)
                OR (state <> 'failed' AND failure_code IS NULL)
            )
        );
        CREATE INDEX ix_structured_import_items_owner ON structured_import_items(owner_id);
        CREATE INDEX ix_structured_import_items_run_state ON structured_import_items(run_id, state);

        ALTER TABLE structured_import_runs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE structured_import_runs FORCE ROW LEVEL SECURITY;
        CREATE POLICY structured_import_runs_isolation ON structured_import_runs
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        ALTER TABLE structured_import_items ENABLE ROW LEVEL SECURITY;
        ALTER TABLE structured_import_items FORCE ROW LEVEL SECURITY;
        CREATE POLICY structured_import_items_isolation ON structured_import_items
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE structured_import_items;
        DROP TABLE structured_import_runs;
        ALTER TABLE documents DROP CONSTRAINT uq_documents_id_uploaded_by;
    """)
