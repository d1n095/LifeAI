"""Life Library upload consolidation package: adds granular IndexStatus values so a
document's pipeline progress (original file stored -> extracting -> extracted -> embedding
-> indexed, or failed at any step) can be modeled and surfaced without losing the received
material when a later step fails. Purely additive to the existing `indexstatus` enum type —
`pending`/`indexing` are left in place (no rows are migrated to the new values; only newly
created/updated rows use them) so nothing already relying on those values breaks. See
app/models/document.py's IndexStatus docstring and docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-22
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Each ADD VALUE is its own statement (see migration 0006's docstring for why: a new
    # enum value can't be used in the same transaction it was added in, so no other DDL/DML
    # in this migration touches these values) but all four are safe to add within one
    # transaction together, since none of them is referenced until a later, separate
    # transaction (the app's own runtime commits).
    op.execute("ALTER TYPE indexstatus ADD VALUE IF NOT EXISTS 'original_stored';")
    op.execute("ALTER TYPE indexstatus ADD VALUE IF NOT EXISTS 'extracting';")
    op.execute("ALTER TYPE indexstatus ADD VALUE IF NOT EXISTS 'extracted';")
    op.execute("ALTER TYPE indexstatus ADD VALUE IF NOT EXISTS 'awaiting_classification';")
    op.execute("ALTER TYPE indexstatus ADD VALUE IF NOT EXISTS 'embedding';")


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE — same recreate-the-type workaround as
    # migration 0005's downgrade. Any row already sitting in one of the four new granular
    # states is remapped to its closest legacy predecessor first, so the USING cast below
    # completes instead of aborting on rows this migration itself produced.
    op.execute("""
        UPDATE documents SET status = 'pending' WHERE status = 'original_stored';
        UPDATE documents SET status = 'indexing'
            WHERE status IN ('extracting', 'extracted', 'awaiting_classification', 'embedding');
    """)
    op.execute("""
        ALTER TYPE indexstatus RENAME TO indexstatus_old;
        CREATE TYPE indexstatus AS ENUM ('pending', 'indexing', 'indexed', 'failed');
        ALTER TABLE documents ALTER COLUMN status TYPE indexstatus USING status::text::indexstatus;
        DROP TYPE indexstatus_old;
    """)
