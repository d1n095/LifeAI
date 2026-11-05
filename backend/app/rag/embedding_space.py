"""Embedding-space identity and storage fitting.

`document_chunks.embedding` is a FIXED pgvector column of width `settings.embedding_dim`
(the "storage dimension", 1536 historically), with an HNSW `vector_cosine_ops` index
(migration 0004). pgvector requires a declared, fixed dimension for an indexed column, so
historically "switching to an embedding model with a different vector size needs a new
migration" (see app/models/document_chunk.py / app/config.py).

This module removes that as a hard blocker for any local model whose NATIVE dimension is at
most the storage dimension, WITHOUT a destructive column-dimension migration or re-embedding:
such a model's vectors are zero-padded up to the storage width on write and on query.
Zero-padding preserves cosine similarity EXACTLY — for two vectors a, b of equal native
length, cos(pad(a), pad(b)) == cos(a, b), because the appended zeros add nothing to the dot
product or to either norm — so the HNSW cosine index returns rankings identical to a native
`vector(native_dim)` column, at the cost of a few stored zeros per row.

That correctness depends ENTIRELY on vectors from different models (or different native
dims) never being compared against one another: a zero-padded 768-d vector and a genuine
1536-d vector occupy the same column but are semantically incomparable. So every chunk
records the `(embedding_model, embedding_native_dim)` that produced it, and retrieval filters
strictly to the ACTIVE embedding space (see app/rag/vector_store.py). That is the
no-silent-mixing guarantee required by the Life embedding design.

A model whose native dimension EXCEEDS the storage dimension cannot be fitted by padding
(truncation would discard signal and break cosine preservation). That is the one case that
genuinely requires a Founder-gated column-dimension migration; `pad_to_storage` fails closed
with a clear error there rather than silently corrupting the space.
"""
from collections.abc import Sequence

from app.config import get_settings


class EmbeddingSpaceError(ValueError):
    """Raised when a batch cannot be fitted into the active storage space (inconsistent
    native dimensions, or a native dimension larger than the storage column). Fail closed —
    never store or compare vectors that would silently corrupt the space."""


def storage_dim() -> int:
    """The physical pgvector column width (document_chunks.embedding)."""
    return get_settings().embedding_dim


def native_dim_of(vectors: Sequence[Sequence[float]]) -> int:
    """The native dimension of a batch (length of its first vector), 0 for an empty batch."""
    return len(vectors[0]) if vectors else 0


def pad_to_storage(vector: Sequence[float], storage: int | None = None) -> list[float]:
    """Zero-pad a single native vector up to the storage dimension. Pass-through when it is
    already the storage width. Fails closed if it is wider than storage (padding cannot
    shrink a vector without discarding signal)."""
    storage = storage_dim() if storage is None else storage
    n = len(vector)
    if n == storage:
        return list(vector)
    if n < storage:
        return list(vector) + [0.0] * (storage - n)
    raise EmbeddingSpaceError(
        f"Embedding native dimension {n} exceeds the storage dimension {storage}; a "
        "Founder-gated column-dimension migration is required — padding cannot shrink a vector."
    )


def fit_vectors(
    vectors: Sequence[Sequence[float]], storage: int | None = None
) -> tuple[list[list[float]], int]:
    """Fit a whole batch for storage. Returns (padded_vectors, native_dim). All vectors in the
    batch must share one native dimension, or it fails closed."""
    storage = storage_dim() if storage is None else storage
    native = native_dim_of(vectors)
    for v in vectors:
        if len(v) != native:
            raise EmbeddingSpaceError(
                f"Inconsistent embedding dimensions in one batch ({len(v)} vs {native}) — "
                "refusing to store a mixed-dimension batch."
            )
    return [pad_to_storage(v, storage) for v in vectors], native
