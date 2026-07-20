"""STEG 10 (claim-level trust): adds knowledge_claims (a testable factual claim extracted
from a source, bound to the exact source/version/chunk it came from) and claim_relationships
(supports/contradicts/supersedes/duplicates edges between claims — the claim-level analogue
of migration 0006's source_relationships). See docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md's DEL 8
section and app/rag/claims.py.

RLS enable/FORCE/policy statements follow the exact same pattern as migration 0006's new
tables — app/rls.py's apply_rls() is extended to match and re-applies these idempotently on
every startup as a safety net, but this migration is what actually owns the schema.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-20
"""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE knowledge_claims (
            id uuid NOT NULL PRIMARY KEY,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            version_id uuid REFERENCES knowledge_versions(id) ON DELETE SET NULL,
            chunk_id uuid REFERENCES document_chunks(id) ON DELETE SET NULL,
            project_id uuid REFERENCES projects(id),
            claim_text text NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'proposed',
            confidence varchar(16) NOT NULL DEFAULT 'uncertain',
            grounding_score double precision NOT NULL DEFAULT 0,
            valid_from timestamp without time zone,
            valid_until timestamp without time zone,
            extraction_version varchar(32) NOT NULL,
            created_at timestamp without time zone NOT NULL
        );
        CREATE INDEX ix_knowledge_claims_owner_id ON knowledge_claims USING btree (owner_id);
        CREATE INDEX ix_knowledge_claims_source_id ON knowledge_claims USING btree (source_id);
        CREATE INDEX ix_knowledge_claims_chunk_id ON knowledge_claims USING btree (chunk_id);
        CREATE INDEX ix_knowledge_claims_project_id ON knowledge_claims USING btree (project_id);

        ALTER TABLE knowledge_claims ENABLE ROW LEVEL SECURITY;
        ALTER TABLE knowledge_claims FORCE ROW LEVEL SECURITY;
        CREATE POLICY knowledge_claims_isolation ON knowledge_claims
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    op.execute("""
        CREATE TABLE claim_relationships (
            id uuid NOT NULL PRIMARY KEY,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            from_claim_id uuid NOT NULL REFERENCES knowledge_claims(id) ON DELETE CASCADE,
            to_claim_id uuid NOT NULL REFERENCES knowledge_claims(id) ON DELETE CASCADE,
            relationship_type varchar(32) NOT NULL,
            note text,
            created_at timestamp without time zone NOT NULL
        );
        CREATE INDEX ix_claim_relationships_owner_id ON claim_relationships USING btree (owner_id);
        CREATE INDEX ix_claim_relationships_from_claim_id ON claim_relationships USING btree (from_claim_id);
        CREATE INDEX ix_claim_relationships_to_claim_id ON claim_relationships USING btree (to_claim_id);

        ALTER TABLE claim_relationships ENABLE ROW LEVEL SECURITY;
        ALTER TABLE claim_relationships FORCE ROW LEVEL SECURITY;
        CREATE POLICY claim_relationships_isolation ON claim_relationships
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE claim_relationships;")
    op.execute("DROP TABLE knowledge_claims;")
