"""STEG 13 (multimedia in UI): stores the raw uploaded bytes for an audio/video source so a
real <audio>/<video> player can actually play it back — Founder Knowledge Studio never
stored raw file bytes anywhere before this (text/document imports only ever kept the
extracted text and embeddings, see app/rag/library_import.py), which was fine when nothing
in the UI needed to play the original file back.

Deliberately a bytea column on `documents` itself, not a new blob-storage service or bucket
— this app has no S3/object storage today and STEG 12/13's work order explicitly forbids
activating a new paid service. Only ever set for audio/video documents
(app/rag/library_import.py's media_kind branch); NULL for every text/document import,
keeping those rows exactly as small as before. Capped by the existing upload size ceiling
(app/routers/library.py's MAX_UPLOAD_BYTES, 60 MB) — the same bound that already applies to
everything going through this endpoint, not a new risk.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-20
"""

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE documents ADD COLUMN media_blob bytea;")


def downgrade() -> None:
    op.execute("ALTER TABLE documents DROP COLUMN media_blob;")
