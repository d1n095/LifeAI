"""Chat message persistence/failure-boundary fix — the audit-confirmed bug where a founder's
own chat message was only ever committed in the same transaction as a successful AI reply, so
a provider failure meant real, silent data loss (see docs/BRANCH_REGISTRY.md's LLM Coupling
audit). Additive only:

  - messages.status: "succeeded" (default — true for every existing row, and for every user
    message from now on) or "failed" (an assistant reply attempt that didn't produce content).
  - messages.in_reply_to_id: set only on assistant rows, pointing at the user message they
    answer. A partial unique index enforces at most one assistant reply per user message —
    retrying an existing (failed) attempt updates that same row rather than creating a
    second, duplicate assistant message.
  - messages.error_category: set only when status='failed' — one of
    app/providers/verification.py's VerificationResult values ("unreachable", "rate_limited",
    "invalid_key", "unsupported"), never a raw exception string.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-26
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE messages
            ADD COLUMN status varchar(16) NOT NULL DEFAULT 'succeeded',
            ADD COLUMN in_reply_to_id uuid REFERENCES messages(id),
            ADD COLUMN error_category varchar(32);

        CREATE UNIQUE INDEX ix_messages_in_reply_to_id_unique
            ON messages (in_reply_to_id)
            WHERE in_reply_to_id IS NOT NULL;
    """)


def downgrade() -> None:
    op.execute("""
        DROP INDEX IF EXISTS ix_messages_in_reply_to_id_unique;
        ALTER TABLE messages
            DROP COLUMN IF EXISTS error_category,
            DROP COLUMN IF EXISTS in_reply_to_id,
            DROP COLUMN IF EXISTS status;
    """)
