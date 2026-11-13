"""Blueprint draft generation idempotency + crash-safe lease (migration 0073).

Mission 3 of the Life MainAI Intelligence/Blueprint program: a client retry after a perceived
timeout (the model synthesis call can legitimately take up to 240s — see
routers/blueprint.py's _SYNTH_TIMEOUT_SECONDS) previously caused a second real provider
dispatch and a second Blueprint version from one logical request, with no way to tell a
genuinely new request from a resubmission of an in-flight or already-completed one.

Purely ADDITIVE and NON-DESTRUCTIVE: two new nullable columns on the existing
`blueprint_drafts` table, no change to any existing row, column, or constraint.

  * `idempotency_key` (text, nullable) — caller-supplied; a request without one behaves
    exactly as before (no idempotency applied, unchanged legacy behavior).
  * `lease_expires_at` (timestamp, nullable) — only meaningful while `status = 'generating'`;
    lets a crashed/restarted request's claim be safely reclaimed by a later retry with the
    same key, the same "lease" vocabulary this codebase already uses for its job queues
    (see app/models/mainai_job.py), reused here rather than inventing new terminology.

Partial unique index `(owner_id, idempotency_key) WHERE idempotency_key IS NOT NULL` is the
actual concurrency primitive: the service layer attempts an INSERT inside a SAVEPOINT and
catches the exact unique violation on THIS index (never a generic "any IntegrityError", same
discipline PR #340's own P2 fix already established for the version-allocation retry) to
fall back to reading the existing claim atomically rather than racing a separate SELECT.

Reversible: downgrade drops the index and both columns; no data loss for rows that never used
this feature (both columns are NULL for every row created before this migration).
"""

import sqlalchemy as sa
from alembic import op

revision = "0073_blueprint_draft_idempotency"
down_revision = "0072_blueprint_approve_identity"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_blueprint_drafts_owner_idempotency_key"


def upgrade() -> None:
    op.add_column("blueprint_drafts", sa.Column("idempotency_key", sa.Text(), nullable=True))
    op.add_column("blueprint_drafts", sa.Column("lease_expires_at", sa.DateTime(), nullable=True))
    op.create_index(
        _INDEX_NAME,
        "blueprint_drafts",
        ["owner_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="blueprint_drafts")
    op.drop_column("blueprint_drafts", "lease_expires_at")
    op.drop_column("blueprint_drafts", "idempotency_key")
