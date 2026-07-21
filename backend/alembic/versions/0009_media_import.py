"""STEG 12 (audio/video import v1): extends document_chunks with a timestamp range
(start_seconds/end_seconds, both nullable — NULL for ordinary text chunks, set for chunks
built from a timed transcript segment) so a citation can open the exact moment in an
audio/video source instead of just the source itself. Extends documents with
media_duration_seconds and transcript_provider (which TranscriptionProvider produced the
transcript — see app/providers/transcription.py — surfaced for UI/debug transparency, not a
functional requirement).

Also adds media_url_imports: a secure URL-import MODEL for future YouTube/web video import,
explicitly NOT wired to any fetcher. This migration only records intent (the URL, the
platform, an explicit consent/rights confirmation, a free-text rights note) with a status
that starts and stays 'pending_review' in v1 — nothing in this codebase ever downloads from
it. See app/models/media_url_import.py and docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md's STEG 12
section for the documented consent/rights/platform-restriction rationale.

RLS pattern matches migration 0007's knowledge_claims table.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-20
"""

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE document_chunks
            ADD COLUMN start_seconds double precision,
            ADD COLUMN end_seconds double precision;

        ALTER TABLE documents
            ADD COLUMN media_duration_seconds double precision,
            ADD COLUMN transcript_provider varchar(64);
    """)

    op.execute("""
        CREATE TABLE media_url_imports (
            id uuid NOT NULL PRIMARY KEY,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            project_id uuid REFERENCES projects(id),
            url text NOT NULL,
            platform varchar(64) NOT NULL,
            consent_confirmed boolean NOT NULL DEFAULT false,
            rights_note text,
            status varchar(32) NOT NULL DEFAULT 'pending_review',
            created_at timestamp without time zone NOT NULL
        );
        CREATE INDEX ix_media_url_imports_owner_id ON media_url_imports USING btree (owner_id);

        ALTER TABLE media_url_imports ENABLE ROW LEVEL SECURITY;
        ALTER TABLE media_url_imports FORCE ROW LEVEL SECURITY;
        CREATE POLICY media_url_imports_isolation ON media_url_imports
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE media_url_imports;")
    op.execute("""
        ALTER TABLE documents
            DROP COLUMN media_duration_seconds,
            DROP COLUMN transcript_provider;

        ALTER TABLE document_chunks
            DROP COLUMN start_seconds,
            DROP COLUMN end_seconds;
    """)
