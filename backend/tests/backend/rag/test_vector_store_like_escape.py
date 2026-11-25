"""The text channel of hybrid_search must treat the founder's query as literal text.

Without escaping, ``%`` and ``_`` are SQL LIKE wildcards. A query containing either could
match every chunk in the owner's library and inject irrelevant documents into the prompt.
These tests pin the escaping helper; the DB-backed search tests exercise the resulting SQL.
"""

from app.rag.vector_store import _like_pattern


def test_percent_is_escaped():
    assert _like_pattern("100%") == "%100\\%%"


def test_underscore_is_escaped():
    assert _like_pattern("a_b") == "%a\\_b%"


def test_backslash_is_escaped_before_wildcards():
    assert _like_pattern("C:\\temp%") == "%C:\\\\temp\\%%"


def test_ordinary_text_is_unchanged_apart_from_the_wrapping_percent():
    assert _like_pattern("pgvector") == "%pgvector%"
