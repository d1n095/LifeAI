"""pgvector migration: replaces the Qdrant-backed knowledge index with a Postgres-native
document_chunks table (app/models/document_chunk.py, app/rag/vector_store.py). Chunks are
strictly per-owner and RLS-protected (app/rls.py's document_chunks_isolation) — a deliberate
narrowing from Document's own shared-company-knowledge model, not a preexisting property of
the old Qdrant search. See docs/RENDER_DEPLOY.md and app/models/document_chunk.py's
docstring for why.

embedding's vector(1536) dimension is fixed at migration time (pgvector requires a declared
size) — it must match app/config.py's embedding_dim. Switching embedding models to a
different dimension needs a new migration, not just a config change.

RLS enable/FORCE/policy statements mirror app/rls.py's apply_rls() exactly — that function
is also idempotent and re-applies these on every startup as a safety net (see app/main.py),
but the migration is what actually owns the schema, consistent with every other table.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-18
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")

    op.execute("""
        CREATE TABLE document_chunks (
            id uuid NOT NULL PRIMARY KEY,
            document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            chunk_index integer NOT NULL,
            text text NOT NULL,
            embedding vector(1536) NOT NULL,
            created_at timestamp without time zone NOT NULL
        );
        CREATE INDEX ix_document_chunks_document_id ON document_chunks USING btree (document_id);
        CREATE INDEX ix_document_chunks_owner_id ON document_chunks USING btree (owner_id);
    """)

    # HNSW over ivfflat: no training/list-count tuning needed, better recall at MVP scale,
    # and available since pgvector 0.5.0 — already required by the vector(1536) column type
    # above. vector_cosine_ops must match the operator app/rag/vector_store.py's search()
    # actually queries with (.cosine_distance(), i.e. the <=> operator) — a mismatched
    # opclass wouldn't error, it would just silently stop being used by the query planner.
    op.execute("""
        CREATE INDEX document_chunks_embedding_hnsw_idx ON document_chunks
        USING hnsw (embedding vector_cosine_ops);
    """)

    op.execute("""
        ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY;
        ALTER TABLE document_chunks FORCE ROW LEVEL SECURITY;

        CREATE POLICY document_chunks_isolation ON document_chunks
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    # No explicit GRANT here, matching every other migration in this file: mainai_app's
    # access comes from the ALTER DEFAULT PRIVILEGES set up once by
    # backend/db-init/01-app-role.sh (local) / backend/scripts/ensure_app_role.py (Render),
    # which already covers any new table the admin role creates — this one included.


def downgrade() -> None:
    op.execute("""
        DROP TABLE document_chunks;
    """)
    # Deliberately not DROP EXTENSION vector — other databases/schemas on the same Postgres
    # instance (or a future table) may depend on it; dropping an extension is a much bigger,
    # cluster-wide action than this migration's own table, and not this migration's to undo.
