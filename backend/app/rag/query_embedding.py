"""Query-side embedding input for instruction-aware models.

Some embedding models are trained ASYMMETRICALLY: the retrieval QUERY is embedded with an
instruction prefix ("Instruct: …\\nQuery: …") while DOCUMENTS are embedded bare. Using such a
model without the prefix does not fail — it silently degrades ranking, which is the worst
failure mode for a local-first knowledge base because nothing looks broken.

The prefix is configuration (`EMBEDDING_QUERY_PREFIX`), never a provider hardcode: it is a
property of the model in use, several established local models (nomic-embed-text, all-minilm,
text-embedding-3-*) are symmetric and must be embedded unmodified, and the default is empty so
this can never change an existing embedding space by surprise.

Only the SEMANTIC query channel is affected. The independent text-match channel in
app/rag/vector_store.py:hybrid_search() keeps the raw query, and stored document text/vectors
are untouched — so this is not a second retrieval system, just the correct input for the
existing one.
"""
from app.config import get_settings


def prepare_embedding_query(query: str) -> str:
    """Return the text to embed for a retrieval query, with the configured prefix applied.

    Empty prefix (the default) is a byte-identical pass-through, so symmetric models and every
    already-indexed embedding space behave exactly as before."""
    prefix = get_settings().embedding_query_prefix
    if not prefix:
        return query
    return f"{prefix}{query}"
