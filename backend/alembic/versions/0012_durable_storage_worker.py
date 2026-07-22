"""Life Library durable-worker package: persistent original-file storage + restart-safe
worker support.

documents gains storage_key/size_bytes/stored_at (the content-addressed blob this
document's original bytes live at in app/storage/, see app/models/document.py's
IndexStatus/DeletionStatus docstrings) and deletion_status (a new native enum — tracks
physical blob purge separately from the row's existing soft-delete via deleted_at).

knowledge_import_jobs gains the same source_storage_key/source_size_bytes/source_media_type
trio for the RAW uploaded package, plus locked_by/lease_expires_at/last_heartbeat_at — the
worker's claim/lease bookkeeping (app/worker.py's claim_next_job, Postgres FOR UPDATE SKIP
LOCKED). Its `status` column is a plain varchar(16) (see migration 0006), not a native enum,
so ImportJobStatus.cancelled needs no ALTER TYPE at all — just an application-level addition.

indexstatus (native enum, documents.status) gains 'received', 'original_storing',
'classifying', 'cancelled' — additive ADD VALUE statements, same pattern as migration 0011.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-22
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE indexstatus ADD VALUE IF NOT EXISTS 'received';")
    op.execute("ALTER TYPE indexstatus ADD VALUE IF NOT EXISTS 'original_storing';")
    op.execute("ALTER TYPE indexstatus ADD VALUE IF NOT EXISTS 'classifying';")
    op.execute("ALTER TYPE indexstatus ADD VALUE IF NOT EXISTS 'cancelled';")

    op.execute("""
        CREATE TYPE deletionstatus AS ENUM ('none', 'pending', 'purged', 'failed');

        ALTER TABLE documents
            ADD COLUMN storage_key varchar(140),
            ADD COLUMN size_bytes integer,
            ADD COLUMN stored_at timestamp without time zone,
            ADD COLUMN deletion_status deletionstatus NOT NULL DEFAULT 'none';

        CREATE INDEX ix_documents_storage_key ON documents USING btree (storage_key);
    """)

    op.execute("""
        ALTER TABLE knowledge_import_jobs
            ADD COLUMN source_storage_key varchar(140),
            ADD COLUMN source_size_bytes integer,
            ADD COLUMN source_media_type varchar(96),
            ADD COLUMN locked_by varchar(128),
            ADD COLUMN lease_expires_at timestamp without time zone,
            ADD COLUMN last_heartbeat_at timestamp without time zone;

        CREATE INDEX ix_knowledge_import_jobs_lease_expires_at ON knowledge_import_jobs USING btree (lease_expires_at);
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE knowledge_import_jobs
            DROP COLUMN source_storage_key,
            DROP COLUMN source_size_bytes,
            DROP COLUMN source_media_type,
            DROP COLUMN locked_by,
            DROP COLUMN lease_expires_at,
            DROP COLUMN last_heartbeat_at;
    """)

    op.execute("""
        ALTER TABLE documents
            DROP COLUMN storage_key,
            DROP COLUMN size_bytes,
            DROP COLUMN stored_at,
            DROP COLUMN deletion_status;

        DROP TYPE deletionstatus;
    """)

    # Postgres has no ALTER TYPE ... DROP VALUE — same recreate-the-type workaround as
    # migrations 0005/0011's downgrades. Any row already sitting in one of the four new
    # states is remapped to its closest legacy predecessor first, so the USING cast below
    # completes instead of aborting on rows this migration itself produced.
    op.execute("""
        UPDATE documents SET status = 'original_stored' WHERE status IN ('received', 'original_storing');
        UPDATE documents SET status = 'awaiting_classification' WHERE status = 'classifying';
        UPDATE documents SET status = 'failed' WHERE status = 'cancelled';
    """)
    op.execute("""
        ALTER TYPE indexstatus RENAME TO indexstatus_old;
        CREATE TYPE indexstatus AS ENUM (
            'pending', 'original_stored', 'extracting', 'extracted',
            'awaiting_classification', 'embedding', 'indexing', 'indexed', 'failed'
        );
        ALTER TABLE documents ALTER COLUMN status TYPE indexstatus USING status::text::indexstatus;
        DROP TYPE indexstatus_old;
    """)
