"""Embedding-space identity on document_chunks — migration 0069_embedding_space_identity.

Adds two nullable columns that record which embedding model produced each chunk vector and
its NATIVE dimension before zero-padding to the fixed storage-column width:

    embedding_model       TEXT     NULL
    embedding_native_dim  INTEGER  NULL

Purely ADDITIVE and NON-DESTRUCTIVE: the `embedding vector(1536)` column, its HNSW index, and
every existing row are untouched. Existing rows get NULL identity (legacy space) and remain
exactly as searchable as before via NULL-identity queries; retrieval scoped to a NEW active
space simply won't return them until they are re-embedded (that is the point — no silent
mixing). Fully REVERSIBLE: downgrade drops the columns and the helper index and restores the
prior schema bit-for-bit.

This does NOT change the vector column dimension. Fitting a local model whose native
dimension is <= the storage dimension is handled at the application layer by zero-padding
(app/rag/embedding_space.py), which needs no schema change. The only scenario that would
require an actual, destructive column-dimension change (a model wider than the storage dim)
remains a separate, explicitly Founder-gated migration — intentionally not performed here.

NOTE (merge ordering): this branches from 0068, as does PR #326's own 0069
("0069_provider_disclosure_local_decision"). They have distinct revision ids and touch
disjoint tables; whichever merges second must set its down_revision to the other (standard
per the repo's merge-rule), or a trivial merge migration can join the two heads. No data or
logic conflict exists between them.
"""

import sqlalchemy as sa
from alembic import op

revision = "0069_embedding_space_identity"
down_revision = "0068"
branch_labels = None
depends_on = None


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


def downgrade() -> None:
    op.drop_index("ix_document_chunks_embedding_space", table_name="document_chunks")
    op.drop_column("document_chunks", "embedding_native_dim")
    op.drop_column("document_chunks", "embedding_model")
