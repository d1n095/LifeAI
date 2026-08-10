"""app/rag/chunking.py had no dedicated test file at all before this — a real gap found
while reviewing STEG 7 ("obegränsad promptstorlek") of the Founder Knowledge Studio work
order. See MAX_CHUNK_CHARS's docstring in chunking.py for the real bug this file's first
test guards against: a whitespace-free document produced one unbounded chunk."""

from app.rag.chunking import MAX_CHUNK_CHARS, chunk_text


def test_empty_text_yields_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   \n\t  ") == []


def test_short_text_yields_a_single_chunk():
    chunks = chunk_text("Detta är en kort testmening.")
    assert chunks == ["Detta är en kort testmening."]


def test_long_text_is_split_with_overlap():
    words = [f"ord{i}" for i in range(2000)]
    text = " ".join(words)
    chunks = chunk_text(text, chunk_size=800, overlap=150)
    assert len(chunks) > 1
    # Consecutive chunks genuinely overlap — the last words of one chunk reappear as the
    # first words of the next, which is the whole point of a sliding window (context isn't
    # lost at a chunk boundary).
    first_chunk_words = chunks[0].split()
    second_chunk_words = chunks[1].split()
    assert first_chunk_words[-1] == second_chunk_words[149]


def test_whitespace_free_document_is_still_hard_capped_in_character_length():
    """Regression test for the real bug: text.split() treats a document with no whitespace
    at all as ONE giant "word", so the word-based chunk_size/overlap logic alone never
    triggers a split — verified locally (before the fix) that a 2MB whitespace-free string
    produced a single 2,000,000-character chunk. MAX_CHUNK_CHARS must bound every chunk
    regardless of whether the input has any word boundaries to split on."""
    pathological = "a" * (MAX_CHUNK_CHARS * 5 + 123)
    chunks = chunk_text(pathological)
    assert len(chunks) > 1
    assert all(len(c) <= MAX_CHUNK_CHARS for c in chunks)
    # No data silently dropped — every character of the original text is still accounted for.
    assert sum(len(c) for c in chunks) == len(pathological)


def test_normal_prose_never_approaches_the_hard_cap():
    """The hard cap is a defensive fallback for pathological input, not something ordinary
    text should ever hit — confirms the fix doesn't fragment normal chunking behavior."""
    words = [f"ord{i}" for i in range(800)]
    text = " ".join(words)
    chunks = chunk_text(text, chunk_size=800, overlap=150)
    assert len(chunks) == 1
    assert len(chunks[0]) < MAX_CHUNK_CHARS
