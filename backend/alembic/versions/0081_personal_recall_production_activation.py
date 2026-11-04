"""Personal Recall production activation foundation.

Encrypted founder-only source ingestion, bounded grants, and owner-scoped production recall
state. These rows are data/retrieval evidence only; file content and retrieved memory never
become MainAI authority.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0081_recall_prod_activation"
down_revision = "0080_verification_registry"
branch_labels = None
depends_on = None

_OWNER = "owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid"


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY {table}_isolation ON {table} USING ({_OWNER}) WITH CHECK ({_OWNER})")
    op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO mainai_app")


def upgrade() -> None:
    op.create_table(
        "personal_recall_owner_keys",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("wrap_algorithm", sa.String(48), nullable=False),
        sa.Column("system_kek_version", sa.String(64), nullable=False),
        sa.Column("wrap_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_owner_key", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('active','rotated','revoked')", name="ck_recall_owner_key_status"),
        sa.CheckConstraint("octet_length(wrapped_owner_key) > 0", name="ck_recall_owner_key_wrapped_nonempty"),
        sa.CheckConstraint("octet_length(wrap_nonce) = 12", name="ck_recall_owner_key_nonce"),
        sa.UniqueConstraint("owner_id", "key_version", name="uq_recall_owner_key_version"),
    )
    op.create_index("ix_recall_owner_keys_owner", "personal_recall_owner_keys", ["owner_id"])

    op.create_table(
        "personal_recall_sources",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_identity", sa.String(256), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("logical_path", sa.String(1024), nullable=True),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("media_type", sa.String(96), nullable=False, server_default="text/plain"),
        sa.Column("parser_version", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="active"),
        sa.Column("disclosure_class", sa.String(16), nullable=False, server_default="normal"),
        sa.Column("encrypted_payload", sa.LargeBinary(), nullable=False),
        sa.Column("payload_algorithm", sa.String(48), nullable=False),
        sa.Column("payload_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("owner_key_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("metadata_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("supersedes_source_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("source_hash ~ '^[0-9a-f]{64}$'", name="ck_recall_source_hash"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_recall_content_hash"),
        sa.CheckConstraint("state IN ('active','superseded','revoked','deleted')", name="ck_recall_source_state"),
        sa.CheckConstraint("disclosure_class IN ('normal','private','sensitive','restricted')", name="ck_recall_source_disclosure"),
        sa.CheckConstraint("state = 'deleted' OR octet_length(encrypted_payload) > 0", name="ck_recall_source_ciphertext_nonempty"),
        sa.CheckConstraint("state = 'deleted' OR octet_length(payload_nonce) = 12", name="ck_recall_source_nonce"),
        sa.UniqueConstraint("owner_id", "source_hash", name="uq_recall_source_exact_duplicate"),
    )
    op.create_index("ix_recall_sources_owner", "personal_recall_sources", ["owner_id"])
    op.create_index("ix_recall_sources_hash", "personal_recall_sources", ["source_hash"])
    op.create_index("ix_recall_sources_content_hash", "personal_recall_sources", ["content_hash"])
    op.create_index("ix_recall_sources_state", "personal_recall_sources", ["state"])

    op.create_table(
        "personal_recall_chunks",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", sa.UUID(), sa.ForeignKey("personal_recall_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunk_identity", sa.String(160), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("encrypted_text", sa.LargeBinary(), nullable=False),
        sa.Column("payload_algorithm", sa.String(48), nullable=False),
        sa.Column("payload_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("owner_key_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("classification", sa.String(32), nullable=False, server_default="UNKNOWN"),
        sa.Column("state", sa.String(16), nullable=False, server_default="active"),
        sa.Column("metadata_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_recall_chunk_hash"),
        sa.CheckConstraint("state IN ('active','superseded','revoked','deleted')", name="ck_recall_chunk_state"),
        sa.CheckConstraint("state = 'deleted' OR octet_length(encrypted_text) > 0", name="ck_recall_chunk_ciphertext_nonempty"),
        sa.CheckConstraint("state = 'deleted' OR octet_length(payload_nonce) = 12", name="ck_recall_chunk_nonce"),
        sa.UniqueConstraint("owner_id", "source_id", "chunk_index", name="uq_recall_chunk_order"),
    )
    op.create_index("ix_recall_chunks_owner", "personal_recall_chunks", ["owner_id"])
    op.create_index("ix_recall_chunks_source", "personal_recall_chunks", ["source_id"])
    op.create_index("ix_recall_chunks_identity", "personal_recall_chunks", ["chunk_identity"])
    op.create_index("ix_recall_chunks_hash", "personal_recall_chunks", ["content_hash"])
    op.create_index("ix_recall_chunks_state", "personal_recall_chunks", ["state"])

    op.create_table(
        "personal_recall_grants",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.String(160), nullable=False),
        sa.Column("boot_id", sa.String(160), nullable=True),
        sa.Column("purpose", sa.String(128), nullable=False),
        sa.Column("scope", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("resource_classes", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("disclosure_level", sa.String(16), nullable=False, server_default="metadata"),
        sa.Column("can_retrieve", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("can_disclose", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.String(64), nullable=False, server_default="founder"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("jsonb_typeof(scope) = 'object'", name="ck_recall_grant_scope_object"),
        sa.CheckConstraint("jsonb_typeof(resource_classes) = 'array'", name="ck_recall_grant_classes_array"),
        sa.CheckConstraint("disclosure_level IN ('none','metadata','snippet')", name="ck_recall_grant_disclosure"),
        sa.CheckConstraint("created_by IN ('founder','admin_bootstrap')", name="ck_recall_grant_created_by"),
    )
    op.create_index("ix_recall_grants_owner", "personal_recall_grants", ["owner_id"])
    op.create_index("ix_recall_grants_session", "personal_recall_grants", ["session_id"])

    op.create_table(
        "personal_recall_extractions",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", sa.UUID(), sa.ForeignKey("personal_recall_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_id", sa.UUID(), sa.ForeignKey("personal_recall_chunks.id", ondelete="CASCADE"), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False, server_default="UNKNOWN"),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("state", sa.String(24), nullable=False, server_default="proposed"),
        sa.Column("provenance", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("text_hash ~ '^[0-9a-f]{64}$'", name="ck_recall_extraction_hash"),
        sa.CheckConstraint("state IN ('proposed','rejected','superseded','accepted_after_review')", name="ck_recall_extraction_state"),
    )
    op.create_index("ix_recall_extractions_owner", "personal_recall_extractions", ["owner_id"])
    op.create_index("ix_recall_extractions_source", "personal_recall_extractions", ["source_id"])

    for table in ("personal_recall_owner_keys", "personal_recall_sources", "personal_recall_chunks", "personal_recall_grants", "personal_recall_extractions"):
        _rls(table)

    op.execute("""
    CREATE OR REPLACE FUNCTION personal_recall_key_guard()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'personal recall owner keys are not deletable'; END IF;
      IF TG_OP = 'INSERT' AND current_setting('app.personal_recall_key_authority', true) <> 'founder_authorized' THEN
        RAISE EXCEPTION 'personal recall key creation requires governed key authority';
      END IF;
      IF TG_OP = 'UPDATE' AND current_setting('app.personal_recall_key_authority', true) <> 'founder_authorized' THEN
        RAISE EXCEPTION 'personal recall key updates require governed key authority';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER trg_personal_recall_key_guard BEFORE INSERT OR UPDATE OR DELETE ON personal_recall_owner_keys
      FOR EACH ROW EXECUTE FUNCTION personal_recall_key_guard();
    CREATE OR REPLACE FUNCTION personal_recall_grant_guard()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'personal recall grants are revoked, not deleted'; END IF;
      IF TG_OP = 'INSERT' AND current_setting('app.personal_recall_grant_authority', true) <> 'founder_authorized' THEN
        RAISE EXCEPTION 'personal recall grant creation requires founder-authorized path';
      END IF;
      IF TG_OP = 'UPDATE' AND OLD.owner_id <> NEW.owner_id THEN RAISE EXCEPTION 'grant owner is immutable'; END IF;
      IF TG_OP = 'UPDATE' AND OLD.session_id <> NEW.session_id THEN RAISE EXCEPTION 'grant session is immutable'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER trg_personal_recall_grant_guard BEFORE INSERT OR UPDATE OR DELETE ON personal_recall_grants
      FOR EACH ROW EXECUTE FUNCTION personal_recall_grant_guard();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_personal_recall_grant_guard ON personal_recall_grants")
    op.execute("DROP FUNCTION IF EXISTS personal_recall_grant_guard()")
    op.execute("DROP TRIGGER IF EXISTS trg_personal_recall_key_guard ON personal_recall_owner_keys")
    op.execute("DROP FUNCTION IF EXISTS personal_recall_key_guard()")
    for table in ("personal_recall_extractions", "personal_recall_grants", "personal_recall_chunks", "personal_recall_sources", "personal_recall_owner_keys"):
        op.drop_table(table)
