def chunk_text(text: str, chunk_size: int = 800, overlap: int = 150) -> list[str]:
    """Simple word-based sliding-window chunker.

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
            chunks.append(chunk)
        if end >= len(words):
            break
        start = end - overlap
    return chunks
