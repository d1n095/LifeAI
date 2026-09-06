"""Atomic, owner-scoped change feed for Personal Recall.

The outbox is routing metadata only. Canonical rows remain the authority and are read again
by consumers. Triggers make the event and the canonical mutation one transaction.
"""
from alembic import op
import sqlalchemy as sa

revision = "0070_personal_recall_outbox"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("recall_generation", sa.BigInteger(), nullable=False, server_default="1"))
    op.add_column("documents", sa.Column("recall_generation", sa.BigInteger(), nullable=False, server_default="1"))
    op.add_column("document_chunks", sa.Column("recall_generation", sa.BigInteger(), nullable=False, server_default="1"))
    op.add_column("knowledge_versions", sa.Column("recall_generation", sa.BigInteger(), nullable=False, server_default="1"))
    op.add_column("memory_source_units", sa.Column("recall_generation", sa.BigInteger(), nullable=False, server_default="1"))
    op.add_column("source_relationships", sa.Column("recall_generation", sa.BigInteger(), nullable=False, server_default="1"))
    op.create_check_constraint("ck_messages_recall_generation_positive", "messages", "recall_generation > 0")
    op.create_check_constraint("ck_documents_recall_generation_positive", "documents", "recall_generation > 0")
    op.create_check_constraint("ck_document_chunks_recall_generation_positive", "document_chunks", "recall_generation > 0")
    op.create_check_constraint("ck_knowledge_versions_recall_generation_positive", "knowledge_versions", "recall_generation > 0")
    op.create_check_constraint("ck_memory_source_units_recall_generation_positive", "memory_source_units", "recall_generation > 0")
    op.create_check_constraint("ck_source_relationships_recall_generation_positive", "source_relationships", "recall_generation > 0")
    op.create_table(
        "personal_recall_outbox",
        sa.Column("event_id", sa.UUID(), primary_key=True),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_type", sa.String(40), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("canonical_version", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("transaction_id", sa.BigInteger(), nullable=False),
        sa.Column("routing_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("canonical_version > 0", name="ck_recall_outbox_version_positive"),
        sa.CheckConstraint("event_type IN ('created','updated','deleted','revoked','purged','superseded','restored')", name="ck_recall_outbox_event_type"),
    )
    op.create_index("ix_recall_outbox_pending", "personal_recall_outbox", ["owner_id", "occurred_at", "event_id"])
    op.create_index("ix_recall_outbox_source_version", "personal_recall_outbox", ["owner_id", "source_type", "source_id", "canonical_version"])
    op.create_table(
        "personal_recall_outbox_delivery",
        sa.Column("event_id", sa.UUID(), sa.ForeignKey("personal_recall_outbox.event_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    op.create_index("ix_recall_outbox_delivery_owner", "personal_recall_outbox_delivery", ["owner_id", "delivered_at"])
    op.execute("""
    ALTER TABLE personal_recall_outbox ENABLE ROW LEVEL SECURITY;
    ALTER TABLE personal_recall_outbox FORCE ROW LEVEL SECURITY;
    CREATE POLICY personal_recall_outbox_owner ON personal_recall_outbox
      USING (owner_id = current_setting('app.current_user_id', true)::uuid)
      WITH CHECK (owner_id = current_setting('app.current_user_id', true)::uuid);
    REVOKE ALL ON personal_recall_outbox FROM PUBLIC;
    GRANT SELECT ON personal_recall_outbox TO mainai_app;
    ALTER TABLE personal_recall_outbox_delivery ENABLE ROW LEVEL SECURITY;
    ALTER TABLE personal_recall_outbox_delivery FORCE ROW LEVEL SECURITY;
    CREATE POLICY personal_recall_outbox_delivery_owner ON personal_recall_outbox_delivery
      USING (owner_id = current_setting('app.current_user_id', true)::uuid)
      WITH CHECK (owner_id = current_setting('app.current_user_id', true)::uuid);
    REVOKE ALL ON personal_recall_outbox_delivery FROM PUBLIC;
    GRANT SELECT, INSERT, UPDATE ON personal_recall_outbox_delivery TO mainai_app;
    """)
    op.execute("""
    CREATE OR REPLACE FUNCTION personal_recall_emit_outbox()
    RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
      v_owner uuid;
      v_source uuid;
      v_type text;
      v_version bigint;
      v_event text;
      v_meta jsonb := '{}'::jsonb;
    BEGIN
      IF TG_TABLE_NAME = 'messages' THEN
        v_owner := (SELECT c.user_id FROM conversations c WHERE c.id = COALESCE(NEW.conversation_id, OLD.conversation_id));
        v_source := COALESCE(NEW.id, OLD.id); v_type := 'conversation_message';
        v_version := COALESCE(NEW.recall_generation, OLD.recall_generation);
      ELSIF TG_TABLE_NAME = 'documents' THEN
        v_owner := COALESCE(NEW.uploaded_by, OLD.uploaded_by); v_source := COALESCE(NEW.id, OLD.id); v_type := 'file';
        v_version := COALESCE(NEW.recall_generation, OLD.recall_generation);
      ELSIF TG_TABLE_NAME = 'document_chunks' THEN
        v_owner := COALESCE(NEW.owner_id, OLD.owner_id); v_source := COALESCE(NEW.document_id, OLD.document_id); v_type := 'file';
        v_version := COALESCE(NEW.recall_generation, OLD.recall_generation);
        v_meta := jsonb_build_object('chunk_id', COALESCE(NEW.id, OLD.id)::text);
      ELSIF TG_TABLE_NAME = 'knowledge_versions' THEN
        v_owner := COALESCE(NEW.owner_id, OLD.owner_id); v_source := COALESCE(NEW.source_id, OLD.source_id); v_type := 'file';
        v_version := COALESCE(NEW.recall_generation, OLD.recall_generation);
        v_meta := jsonb_build_object('version_number', COALESCE(NEW.version_number, OLD.version_number));
      ELSE
        v_owner := COALESCE(NEW.owner_id, OLD.owner_id); v_source := COALESCE(NEW.id, OLD.id); v_type := 'durable_memory';
        v_version := COALESCE(NEW.recall_generation, OLD.recall_generation);
      END IF;
      IF TG_OP = 'INSERT' THEN
        v_event := 'created';
      ELSIF TG_OP = 'DELETE' THEN
        v_event := 'deleted';
      ELSIF TG_TABLE_NAME = 'memory_source_units' THEN
        IF NEW.lifecycle_status = 'purged' THEN v_event := 'purged';
        ELSIF NEW.lifecycle_status = 'revoked' THEN v_event := 'revoked';
        ELSIF OLD.lifecycle_status <> 'active' AND NEW.lifecycle_status = 'active' THEN v_event := 'restored';
        ELSE v_event := 'updated'; END IF;
      ELSIF TG_TABLE_NAME = 'documents' THEN
        IF OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS NULL THEN v_event := 'restored';
        ELSE v_event := 'updated'; END IF;
      ELSE
        v_event := 'updated';
      END IF;
      IF v_owner IS NOT NULL THEN
        INSERT INTO public.personal_recall_outbox(event_id, owner_id, source_type, source_id, canonical_version, event_type, transaction_id, routing_metadata)
        VALUES (md5(txid_current()::text || clock_timestamp()::text || random()::text)::uuid, v_owner, v_type, v_source, GREATEST(v_version, 1), v_event, txid_current(), v_meta);
      END IF;
      IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END $$;
    """)
    op.execute("""
    CREATE OR REPLACE FUNCTION personal_recall_emit_relationship_outbox()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      INSERT INTO public.personal_recall_outbox(event_id, owner_id, source_type, source_id, canonical_version, event_type, transaction_id, routing_metadata)
      VALUES (md5(txid_current()::text || clock_timestamp()::text || random()::text)::uuid, COALESCE(NEW.owner_id, OLD.owner_id), 'file', COALESCE(NEW.from_source_id, OLD.from_source_id),
              GREATEST(COALESCE(NEW.recall_generation, OLD.recall_generation), 1),
              CASE WHEN TG_OP = 'DELETE' THEN 'deleted' ELSE 'superseded' END, txid_current(),
              jsonb_build_object('relationship_id', COALESCE(NEW.id, OLD.id)::text));
      IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END $$;
    CREATE OR REPLACE FUNCTION personal_recall_bump_source_relationships() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN NEW.recall_generation := OLD.recall_generation + 1; RETURN NEW; END $$;
    CREATE TRIGGER personal_recall_source_relationships_generation BEFORE UPDATE ON source_relationships
      FOR EACH ROW EXECUTE FUNCTION personal_recall_bump_source_relationships();
    CREATE TRIGGER personal_recall_source_relationships_outbox AFTER INSERT OR UPDATE OR DELETE ON source_relationships
      FOR EACH ROW EXECUTE FUNCTION personal_recall_emit_relationship_outbox();
    """)
    for table in ("messages", "documents", "document_chunks", "knowledge_versions", "memory_source_units"):
        op.execute(f"""
        CREATE OR REPLACE FUNCTION personal_recall_bump_{table}() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'UPDATE' THEN NEW.recall_generation := OLD.recall_generation + 1; END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER personal_recall_{table}_generation BEFORE UPDATE ON {table}
          FOR EACH ROW EXECUTE FUNCTION personal_recall_bump_{table}();
        CREATE TRIGGER personal_recall_{table}_outbox AFTER INSERT OR UPDATE OR DELETE ON {table}
          FOR EACH ROW EXECUTE FUNCTION personal_recall_emit_outbox();
        """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS personal_recall_source_relationships_outbox ON source_relationships")
    op.execute("DROP TRIGGER IF EXISTS personal_recall_source_relationships_generation ON source_relationships")
    op.execute("DROP FUNCTION IF EXISTS personal_recall_emit_relationship_outbox()")
    op.execute("DROP FUNCTION IF EXISTS personal_recall_bump_source_relationships()")
    for table in ("source_relationships", "memory_source_units", "knowledge_versions", "document_chunks", "documents", "messages"):
        op.execute(f"DROP TRIGGER IF EXISTS personal_recall_{table}_outbox ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS personal_recall_{table}_generation ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS personal_recall_bump_{table}()")
    op.execute("DROP FUNCTION IF EXISTS personal_recall_emit_outbox()")
    op.drop_index("ix_recall_outbox_source_version", table_name="personal_recall_outbox")
    op.drop_index("ix_recall_outbox_pending", table_name="personal_recall_outbox")
    op.drop_index("ix_recall_outbox_delivery_owner", table_name="personal_recall_outbox_delivery")
    op.drop_table("personal_recall_outbox_delivery")
    op.drop_table("personal_recall_outbox")
    for table in ("source_relationships", "memory_source_units", "knowledge_versions", "document_chunks", "documents", "messages"):
        op.drop_constraint(f"ck_{table}_recall_generation_positive", table, type_="check")
        op.drop_column(table, "recall_generation")
