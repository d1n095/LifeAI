"""Persistent retry leases and dead-letter state for Recall delivery."""
from alembic import op
import sqlalchemy as sa

revision = "0071_personal_recall_delivery_ops"
down_revision = "0070_personal_recall_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("personal_recall_outbox_delivery", "delivered_at", nullable=True)
    op.add_column("personal_recall_outbox_delivery", sa.Column("state", sa.String(16), nullable=False, server_default="pending"))
    op.add_column("personal_recall_outbox_delivery", sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("personal_recall_outbox_delivery", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("personal_recall_outbox_delivery", sa.Column("error_class", sa.String(64), nullable=True))
    op.add_column("personal_recall_outbox_delivery", sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("personal_recall_outbox_delivery", sa.Column("blocked_reason", sa.String(256), nullable=True))
    op.add_column("personal_recall_outbox_delivery", sa.Column("lease_owner", sa.String(128), nullable=True))
    op.add_column("personal_recall_outbox_delivery", sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE personal_recall_outbox_delivery SET state='delivered', retryable=true WHERE delivered_at IS NOT NULL")
    op.create_check_constraint("ck_recall_delivery_state", "personal_recall_outbox_delivery", "state IN ('pending','retrying','delivered','dead_letter')")


def downgrade() -> None:
    op.drop_constraint("ck_recall_delivery_state", "personal_recall_outbox_delivery", type_="check")
    for name in ("lease_until", "lease_owner", "blocked_reason", "retryable", "error_class", "next_attempt_at", "last_attempt_at", "state"):
        op.drop_column("personal_recall_outbox_delivery", name)
    op.alter_column("personal_recall_outbox_delivery", "delivered_at", nullable=False)
