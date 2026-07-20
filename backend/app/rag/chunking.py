# Hard character-length ceiling per chunk, independent of the word-based chunk_size below.
# Found as a real bug (not hypothetical): chunk_size/overlap operate on WORD count via
# text.split(), so a document with no whitespace at all (one pathologically long "word" —
# e.g. a minified file, a base64 blob, or just malformed/adversarial input) produces exactly
# one chunk containing the ENTIRE document, unbounded — verified locally with a 2MB
# whitespace-free string yielding a single 2,000,000-character chunk. That chunk then goes
# to the embedding provider whole and, later, straight into a chat/workbench prompt
# (app/routers/chat.py's context_block) — an unbounded per-request cost/prompt-size
# vulnerability, not just an embedding-quality issue. MAX_CHUNK_CHARS is a defensive fallback
# for exactly that pathological case; ordinary prose chunked by chunk_size/overlap never gets
# close to it.
MAX_CHUNK_CHARS = 6000


def _hard_split(chunk: str) -> list[str]:
    if len(chunk) <= MAX_CHUNK_CHARS:
        return [chunk]
    return [chunk[i : i + MAX_CHUNK_CHARS] for i in range(0, len(chunk), MAX_CHUNK_CHARS)]


def chunk_text(text: str, chunk_size: int = 800, overlap: int = 150) -> list[str]:
    """Simple word-based sliding-window chunker, with a hard character-length fallback split
    (see MAX_CHUNK_CHARS above) for the pathological case where a "word" (as text.split()
    defines one) is itself huge.

    Good enough for an MVP knowledge base — swap for a token-aware splitter
    (tiktoken-based) once real usage patterns are known.
    """
    words = text.split()
    if not words:
        return []

    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = start + chunk_size
        chunk = " ".join(words[start:end])
        if chunk.strip():
            chunks.extend(_hard_split(chunk))
        if end >= len(words):
            break
        start = end - overlap
    return chunks
