"""Deterministic concept text normalization for Stage B reconciliation."""

from __future__ import annotations

import re
import unicodedata


_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize_concept_text(value: str) -> str:
    """NFKC → casefold → strip punctuation → collapse whitespace.

    Used as the fingerprint for SAME collapse. Deliberately deterministic and provider-free.
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    text = _PUNCT.sub(" ", text)
    text = _WS.sub(" ", text).strip()
    return text


def token_set(value: str) -> set[str]:
    norm = normalize_concept_text(value)
    if not norm:
        return set()
    return set(norm.split(" "))


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0
