"""Blueprint drafts — migration 0070_blueprint_drafts.

Founder Alpha item 8/9. Purely ADDITIVE and NON-DESTRUCTIVE: creates one new owner-scoped table
`blueprint_drafts` holding versioned, source-traceable, DRAFT-pending-approval Blueprint drafts
(AI interpretations only — never original bytes). Touches no existing table; fully reversible
(downgrade drops the table, which cascades its RLS policy). Owner-scoped RLS is enabled +
FORCEd here, in the migration (the repo convention — every owner-scoped table enables RLS in its
own migration; app/rls.py re-asserts the same policy at runtime), so a migration upgrade→
downgrade→upgrade round-trip restores the table AND its isolation policy identically.

The composed lineage is serial: historical provider-locality revision 0069, then
0069_embedding_space_identity, then this revision. Keeping the historical revision id in the
graph is required for data-safe upgrades from databases that already recorded it.
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
    # Owner-scoped RLS, enabled + FORCEd in the migration (same policy app/rls.py re-asserts at
    # runtime). FORCE so even the table owner is subject to it. Expression matches
    # POLICY_DEFINITIONS["blueprint_drafts_isolation"] in app/rls.py exactly.
    op.execute("ALTER TABLE blueprint_drafts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE blueprint_drafts FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY blueprint_drafts_isolation ON blueprint_drafts "
        "USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid) "
        "WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)"
    )


def downgrade() -> None:
    op.drop_index("ix_blueprint_drafts_owner_title_version", table_name="blueprint_drafts")
    op.drop_table("blueprint_drafts")
