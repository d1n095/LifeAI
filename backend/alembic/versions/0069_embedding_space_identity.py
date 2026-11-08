"""Embedding-space identity on document_chunks — migration 0069_embedding_space_identity.

Adds two nullable columns that record which embedding model produced each chunk vector and
its NATIVE dimension before zero-padding to the fixed storage-column width:

    embedding_model       TEXT     NULL
    embedding_native_dim  INTEGER  NULL

Purely ADDITIVE and NON-DESTRUCTIVE: the `embedding vector(1536)` column, its HNSW index, and
every existing row are untouched. Existing rows get NULL identity (legacy space) and are not
eligible for semantic comparison until explicitly re-embedded. Downgrade preserves exact
non-NULL provenance in a locked migration-owned backup table; re-upgrade restores it rather
than inventing or silently discarding embedding identity.

This does NOT change the vector column dimension. Fitting a local model whose native
dimension is <= the storage dimension is handled at the application layer by zero-padding
(app/rag/embedding_space.py), which needs no schema change. The only scenario that would
require an actual, destructive column-dimension change (a model wider than the storage dim)
remains a separate, explicitly Founder-gated migration — intentionally not performed here.

The historical #326 revision `0069` precedes this revision. Keeping its exact identifier
prevents Alembic from interpreting the older revision as a prefix for this one and silently
skipping the embedding columns.
"""

import sqlalchemy as sa
from alembic import op

revision = "0069_embedding_space_identity"
down_revision = "0069"
branch_labels = None
depends_on = None

_PROVENANCE_BACKUP = "_alembic_0069_embedding_space_identity_backup"


def upgrade() -> None:
    op.add_column("document_chunks", sa.Column("embedding_model", sa.Text(), nullable=True))
    op.add_column("document_chunks", sa.Column("embedding_native_dim", sa.Integer(), nullable=True))
    # Lets the active-space filter in app/rag/vector_store.py prune by identity cheaply before
    # the cosine ordering runs; partial on NOT NULL so legacy NULL-identity rows cost nothing.
    op.create_index(
        "ix_document_chunks_embedding_space",
        "document_chunks",
        ["embedding_model", "embedding_native_dim"],
        postgresql_where=sa.text("embedding_model IS NOT NULL"),
    )
    bind = op.get_bind()
    if sa.inspect(bind).has_table(_PROVENANCE_BACKUP):
        # This table exists only after an intentional downgrade. Restore exactly what was
        # recorded; rows created while running 0068 remain NULL because their provenance is
        # genuinely unknown and must never be fabricated.
        op.execute(
            sa.text(
                f"""
                UPDATE document_chunks AS chunks
                   SET embedding_model = backup.embedding_model,
                       embedding_native_dim = backup.embedding_native_dim
                  FROM {_PROVENANCE_BACKUP} AS backup
                 WHERE chunks.id = backup.document_chunk_id
                """
            )
        )
        op.drop_table(_PROVENANCE_BACKUP)


def downgrade() -> None:
    # Dropping the identity columns without retaining their values makes a subsequent upgrade
    # silently relabel known vectors as unknown. Keep the exact values in a migration-owned
    # table that the runtime role cannot read or mutate. The table deliberately has no foreign
    # key: the repository's supported full downgrade removes document_chunks at migration 0004.
    # Re-upgrade restores with an inner join, so provenance for a legitimately deleted chunk is
    # ignored while the remaining rows are preserved exactly.
    op.create_table(
        _PROVENANCE_BACKUP,
        sa.Column(
            "document_chunk_id",
            sa.Uuid(),
            primary_key=True,
        ),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.Column("embedding_native_dim", sa.Integer(), nullable=True),
    )
    op.execute(
        sa.text(
            f"""
            INSERT INTO {_PROVENANCE_BACKUP}
                (document_chunk_id, embedding_model, embedding_native_dim)
            SELECT id, embedding_model, embedding_native_dim
              FROM document_chunks
             WHERE embedding_model IS NOT NULL
                OR embedding_native_dim IS NOT NULL
            """
        )
    )
    op.execute(sa.text(f"REVOKE ALL ON TABLE {_PROVENANCE_BACKUP} FROM PUBLIC"))
    op.execute(
        sa.text(
            f"""
            DO $guard$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mainai_app') THEN
                    EXECUTE 'REVOKE ALL ON TABLE {_PROVENANCE_BACKUP} FROM mainai_app';
                END IF;
            END
            $guard$;
            """
        )
    )
    op.drop_index("ix_document_chunks_embedding_space", table_name="document_chunks")
    op.drop_column("document_chunks", "embedding_native_dim")
    op.drop_column("document_chunks", "embedding_model")
