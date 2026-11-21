"""Deterministic text-sensitive embedding for the test-only E2E provider.

Production code never imports this module. It exists so the E2E harness can exercise the
real pgvector/RLS/citation path without a model, while a query sharing a unique marker still
ranks that marker's own source above unrelated test documents.
"""

from __future__ import annotations

import hashlib
import math
import re


def text_embedding(text: str, dim: int) -> list[float]:
    if dim <= 0:
        raise ValueError("embedding dimension must be positive")
    vector = [0.0] * dim
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        digest = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        vector[index] += 1.0 if digest[4] & 1 else -1.0
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        vector[0] = 1.0
        return vector
    return [value / norm for value in vector]
