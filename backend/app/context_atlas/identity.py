"""Source identity and duplicate intelligence (Context Atlas Workstream B).

Read-only analysis over bytes that are ALREADY stored elsewhere: app/storage/ keeps the
immutable original and Document.checksum/KnowledgeVersion.checksum already carry sha256. This
module stores, indexes, moves and deletes nothing. The caller keeps both originals.

Why it exists: exact byte identity is already covered by Document.checksum and the
content-addressed storage key. What did not exist was one bounded way to distinguish
"identical bytes" from "identical after whitespace/Unicode normalization" from "one character
changed" from "similar but not identical" — without letting a similarity score masquerade as
proof of identity. Only `exact_byte_identical` is authoritative; every other verdict is a
candidate for review, never a dedup decision.

app/rag/trust.py remains the only place that interprets RECORDED `duplicates`/`contradicts`
edges for retrieval. This module does not write those edges.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

# Bounded work: text metrics are skipped above this size, but sha256 is still computed so
# exact identity always works. 8 MiB matches the largest supported single library upload
# class in practice while staying cheap on the 2 GB VPS.
MAX_COMPARE_BYTES = 8 * 1024 * 1024
# Edit-distance check is O(n) but allocates two decoded strings; cap it rather than run it on
# a multi-megabyte document. Above this, token similarity still answers "same or not".
MAX_EDIT_CHARS = 200_000
MAX_TOKENS = 20_000
SIMILARITY_THRESHOLD = 0.4  # same threshold app/project_memory.py already uses for Jaccard
NEAR_DUPLICATE_THRESHOLD = 0.9

_WS_RE = re.compile(r"\s+", re.UNICODE)
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_BOM = b"\xef\xbb\xbf"


class Verdict(str, Enum):
    exact_byte_identical = "exact_byte_identical"
    normalized_identical = "normalized_identical"
    one_character_edit = "one_character_edit"
    near_duplicate = "near_duplicate"
    similar = "similar"
    distinct = "distinct"
    not_comparable = "not_comparable"


class MediaKind(str, Enum):
    text = "text"
    binary = "binary"
    undecodable = "undecodable"
    oversized = "oversized"


@dataclass(frozen=True)
class SourceFingerprint:
    """Deterministic, cheap evidence about one source. Never a storage handle."""

    sha256: str
    size_bytes: int
    media_kind: MediaKind
    # Hashes of progressively normalized text. None for non-text/oversized input. They are
    # identity *hints* for review, never authoritative identity.
    nfc_sha256: str | None = None
    whitespace_sha256: str | None = None
    nfkc_whitespace_sha256: str | None = None
    punctuation_sha256: str | None = None
    token_count: int = 0


@dataclass(frozen=True)
class IdentityAssessment:
    verdict: Verdict
    authoritative: bool
    reason: str
    left: SourceFingerprint
    right: SourceFingerprint
    similarity: float | None
    edit_distance: int | None
    differences: tuple[str, ...]
    proposed_relationship: str | None
    requires_review: bool
    # Never false: a normalized or similar document is still a distinct original until a
    # human (or an explicit, recorded founder decision) says otherwise.
    preserve_originals: bool = True


def _sha256(data: bytes) -> str:
    hasher = hashlib.sha256()
    view = memoryview(data)
    for start in range(0, len(view), 1 << 20):
        hasher.update(view[start : start + (1 << 20)])
    return hasher.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _decode_text(data: bytes) -> tuple[str | None, MediaKind]:
    if b"\x00" in data[:8192]:
        return None, MediaKind.binary
    body = data[len(_BOM) :] if data.startswith(_BOM) else data
    try:
        return body.decode("utf-8"), MediaKind.text
    except UnicodeDecodeError:
        return None, MediaKind.undecodable


def _collapse_whitespace(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _strip_punctuation(text: str) -> str:
    return "".join(ch for ch in text if not unicodedata.category(ch).startswith("P"))


def _tokens(text: str) -> set[str]:
    folded = unicodedata.normalize("NFKC", text).casefold()
    tokens = _TOKEN_RE.findall(folded)
    return set(tokens[:MAX_TOKENS])


def fingerprint(data: bytes, *, max_bytes: int = MAX_COMPARE_BYTES) -> SourceFingerprint:
    """Hashes and normalizes one payload. Raises TypeError for non-bytes input."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("fingerprint() requires bytes-like input")
    raw = bytes(data)
    sha = _sha256(raw)
    size = len(raw)
    if size > max_bytes:
        return SourceFingerprint(
            sha256=sha, size_bytes=size, media_kind=MediaKind.oversized
        )

    text, media_kind = _decode_text(raw)
    if text is None:
        return SourceFingerprint(
            sha256=sha, size_bytes=size, media_kind=media_kind
        )

    nfc = unicodedata.normalize("NFC", text)
    nfkc = unicodedata.normalize("NFKC", text)
    nfkc_ws = _collapse_whitespace(nfkc)
    return SourceFingerprint(
        sha256=sha,
        size_bytes=size,
        media_kind=MediaKind.text,
        nfc_sha256=_sha256_text(nfc),
        whitespace_sha256=_sha256_text(_collapse_whitespace(text)),
        nfkc_whitespace_sha256=_sha256_text(nfkc_ws),
        punctuation_sha256=_sha256_text(_strip_punctuation(nfkc_ws)),
        token_count=len(_tokens(text)),
    )


def _within_one_edit(left: str, right: str) -> bool:
    """True only for a single insertion, deletion or substitution."""
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(1 for a, b in zip(left, right) if a != b) <= 1
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    i = j = 0
    skipped = False
    while i < len(shorter) and j < len(longer):
        if shorter[i] == longer[j]:
            i += 1
            j += 1
            continue
        if skipped:
            return False
        skipped = True
        j += 1
    return True


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def compare_sources(
    left: bytes,
    right: bytes,
    *,
    similarity_threshold: float = SIMILARITY_THRESHOLD,
    near_duplicate_threshold: float = NEAR_DUPLICATE_THRESHOLD,
    max_bytes: int = MAX_COMPARE_BYTES,
) -> IdentityAssessment:
    """Classifies the relationship between two source payloads.

    Ordered, least-surprising checks first: byte identity, then normalization identity, then
    a single-character edit, then bounded token similarity. Similarity never upgrades to
    identity, and only exact byte identity is authoritative or proposes a recordable
    `duplicates` edge.
    """
    left_fp = fingerprint(left, max_bytes=max_bytes)
    right_fp = fingerprint(right, max_bytes=max_bytes)

    if left_fp.sha256 == right_fp.sha256:
        return IdentityAssessment(
            verdict=Verdict.exact_byte_identical,
            authoritative=True,
            reason="identical sha256 over the original bytes",
            left=left_fp,
            right=right_fp,
            similarity=1.0,
            edit_distance=0,
            differences=(),
            proposed_relationship="duplicates",
            requires_review=False,
        )

    differences = ["sha256_differs"]
    if left_fp.size_bytes != right_fp.size_bytes:
        differences.append(
            f"size_differs:{left_fp.size_bytes}:{right_fp.size_bytes}"
        )

    if left_fp.media_kind is not MediaKind.text or right_fp.media_kind is not MediaKind.text:
        return IdentityAssessment(
            verdict=Verdict.not_comparable,
            authoritative=False,
            reason=(
                "not byte-identical, and at least one payload is not comparable text "
                f"({left_fp.media_kind.value}/{right_fp.media_kind.value})"
            ),
            left=left_fp,
            right=right_fp,
            similarity=None,
            edit_distance=None,
            differences=tuple(differences),
            proposed_relationship=None,
            requires_review=True,
        )

    if left_fp.whitespace_sha256 == right_fp.whitespace_sha256:
        return _normalized(left_fp, right_fp, differences, "whitespace_only")
    if left_fp.nfc_sha256 == right_fp.nfc_sha256:
        return _normalized(left_fp, right_fp, differences, "unicode_equivalence_only")
    if left_fp.nfkc_whitespace_sha256 == right_fp.nfkc_whitespace_sha256:
        return _normalized(
            left_fp, right_fp, differences, "unicode_and_whitespace_only"
        )
    if left_fp.punctuation_sha256 == right_fp.punctuation_sha256:
        return _normalized(left_fp, right_fp, differences, "punctuation_only")

    left_text = _decode_text(left)[0] or ""
    right_text = _decode_text(right)[0] or ""
    if len(left_text) <= MAX_EDIT_CHARS and len(right_text) <= MAX_EDIT_CHARS:
        if _within_one_edit(left_text, right_text):
            return IdentityAssessment(
                verdict=Verdict.one_character_edit,
                authoritative=False,
                reason="exactly one inserted, deleted or substituted character",
                left=left_fp,
                right=right_fp,
                similarity=_jaccard(_tokens(left_text), _tokens(right_text)),
                edit_distance=1,
                differences=tuple(differences + ["one_character_edit"]),
                proposed_relationship=None,
                requires_review=True,
            )

    similarity = _jaccard(_tokens(left_text), _tokens(right_text))
    if similarity >= near_duplicate_threshold:
        verdict = Verdict.near_duplicate
        reason = f"token Jaccard {similarity:.3f} >= {near_duplicate_threshold} (near-duplicate)"
    elif similarity >= similarity_threshold:
        verdict = Verdict.similar
        reason = f"token Jaccard {similarity:.3f} >= {similarity_threshold} (similar, not identical)"
    else:
        verdict = Verdict.distinct
        reason = f"token Jaccard {similarity:.3f} < {similarity_threshold} (distinct)"

    return IdentityAssessment(
        verdict=verdict,
        authoritative=False,
        reason=reason,
        left=left_fp,
        right=right_fp,
        similarity=similarity,
        edit_distance=None,
        differences=tuple(differences + [verdict.value]),
        proposed_relationship=None,
        requires_review=True,
    )


def _normalized(
    left: SourceFingerprint,
    right: SourceFingerprint,
    differences: list[str],
    detail: str,
) -> IdentityAssessment:
    return IdentityAssessment(
        verdict=Verdict.normalized_identical,
        authoritative=False,
        reason=(
            f"equal only after normalization ({detail}); original bytes differ, so this is "
            "not proof of identity"
        ),
        left=left,
        right=right,
        similarity=1.0,
        edit_distance=None,
        differences=tuple(differences + [detail]),
        proposed_relationship=None,
        requires_review=True,
    )
