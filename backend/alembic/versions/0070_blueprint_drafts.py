"""Blueprint drafts — migration 0070_blueprint_drafts.

Founder Alpha item 8/9. Purely ADDITIVE and NON-DESTRUCTIVE: creates one new owner-scoped table
`blueprint_drafts` holding versioned, source-traceable, DRAFT-pending-approval Blueprint drafts
(AI interpretations only — never original bytes). Touches no existing table; fully reversible
(downgrade drops the table). RLS is applied by app/rls.py (owner_id isolation), not here.

Merge ordering: branches from 0069_embedding_space_identity (this lineage's single head). PR
#326's own 0069 (provider_disclosure_local_decision) also branches from 0068; whichever of
#333/#326 merges first, this migration's down_revision must end up chained onto the resulting
single head at merge time (standard per the repo merge-rule). No data/logic conflict exists.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0070_blueprint_drafts"
down_revision = "0069_embedding_space_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "blueprint_drafts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("title", sa.String(length=300), nullable=False, index=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("generated_from_query", sa.Text(), nullable=True),
        sa.Column("source_document_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("source_refs", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("model_provider", sa.String(length=64), nullable=True),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_blueprint_drafts_owner_title_version",
        "blueprint_drafts",
        ["owner_id", "title", "version"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_blueprint_drafts_owner_title_version", table_name="blueprint_drafts")
    op.drop_table("blueprint_drafts")
