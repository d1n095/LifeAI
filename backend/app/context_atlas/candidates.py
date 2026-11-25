"""Indexed, bounded candidate lookup over existing Document rows (Phase 1).

The identity primitives in `identity.py` compare two payloads. This module connects them to
the real document workflow without inventing a second index: exact identity is looked up
through `documents.checksum` (already indexed, sha256, written by
`app/rag/library_import.py`), and near-duplicate candidates are drawn from a bounded
size-window scan over the same owner-scoped table. Nothing is written, no relationship edge
is created, and every original is preserved.

Deliberately NOT a background deduplicator: `find_candidates()` is a read-only advisory a
caller (library upload, CLI, future review UI) can use before/after an import. Recording a
`duplicates` edge stays an explicit, reviewed action.

Isolation: every query filters `Document.uploaded_by == owner_id` and relies on the same RLS
policy as the rest of the app (documents is FORCE ROW LEVEL SECURITY — see app/rls.py). The
caller is responsible for setting `app.current_user_id` exactly like a request does; a
missing setting yields zero rows rather than a cross-tenant leak.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.context_atlas.identity import (
    MAX_COMPARE_BYTES,
    Verdict,
    compare_sources,
    fingerprint,
)
from app.models.document import Document
from app.storage import StorageError, get_storage

DEFAULT_LIMIT = 5
DEFAULT_MAX_SCAN = 25
# A candidate must be within this factor of the incoming size to be worth reading at all.
# Exact identity never depends on this window — it is answered by the checksum index.
SIZE_WINDOW_RATIO = 0.5
MIN_SIZE_WINDOW_BYTES = 256

_RELATED_VERDICTS = frozenset(
    {
        Verdict.normalized_identical,
        Verdict.one_character_edit,
        Verdict.near_duplicate,
        Verdict.similar,
    }
)


@dataclass(frozen=True)
class SourceCandidate:
    document_id: str
    title: str
    checksum: str
    size_bytes: int | None
    verdict: Verdict
    similarity: float | None
    evidence: str  # "checksum_index" | "size_window"
    project_id: str | None
    version_number: int
    active_truth_status: str
    requires_review: bool


@dataclass(frozen=True)
class CandidateReport:
    incoming_sha256: str
    incoming_size_bytes: int
    exact: tuple[SourceCandidate, ...]
    related: tuple[SourceCandidate, ...]
    scanned_documents: int
    screened_out: int
    truncated: bool
    unreadable_originals: tuple[str, ...]
    notes: tuple[str, ...]
    # Only exact byte identity is authoritative. Related candidates are review material.
    lookup_authoritative: bool
    originals_preserved: bool = True
    execution_authorized: bool = False

    def to_dict(self) -> dict:
        def _candidate(c: SourceCandidate) -> dict:
            return {
                "document_id": c.document_id,
                "title": c.title,
                "checksum": c.checksum,
                "size_bytes": c.size_bytes,
                "verdict": c.verdict.value,
                "similarity": c.similarity,
                "evidence": c.evidence,
                "project_id": c.project_id,
                "version_number": c.version_number,
                "active_truth_status": c.active_truth_status,
                "requires_review": c.requires_review,
            }

        return {
            "incoming_sha256": self.incoming_sha256,
            "incoming_size_bytes": self.incoming_size_bytes,
            "exact": [_candidate(c) for c in self.exact],
            "related": [_candidate(c) for c in self.related],
            "scanned_documents": self.scanned_documents,
            "screened_out": self.screened_out,
            "truncated": self.truncated,
            "unreadable_originals": list(self.unreadable_originals),
            "notes": list(self.notes),
            "lookup_authoritative": self.lookup_authoritative,
            "originals_preserved": self.originals_preserved,
            "execution_authorized": False,
        }


def _read_original(storage, storage_key: str | None) -> bytes | None:
    if not storage_key:
        return None
    try:
        with storage.open_read(storage_key) as handle:
            return handle.read()
    except (StorageError, OSError, ValueError):
        return None


def find_candidates(
    db: Session,
    *,
    owner_id: uuid.UUID,
    content: bytes,
    project_id: uuid.UUID | None = None,
    limit: int = DEFAULT_LIMIT,
    max_scan: int = DEFAULT_MAX_SCAN,
) -> CandidateReport:
    """Return exact matches plus bounded, review-only near-duplicate candidates.

    `limit` caps returned matches per bucket; `max_scan` caps how many candidate originals
    are read. Both are hard bounds, so cost stays proportional to `max_scan` — never to the
    size of the corpus.
    """
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise TypeError("find_candidates() requires bytes-like content")
    if limit < 1 or max_scan < 1:
        raise ValueError("limit and max_scan must be positive")

    payload = bytes(content)
    incoming = fingerprint(payload)
    size = incoming.size_bytes

    base = db.query(Document).filter(
        Document.uploaded_by == owner_id,
        Document.deleted_at.is_(None),
    )
    if project_id is not None:
        base = base.filter(Document.project_id == project_id)

    exact_rows = (
        base.filter(Document.checksum == incoming.sha256)
        .order_by(Document.created_at.asc())
        .limit(limit)
        .all()
    )

    exact = tuple(
        SourceCandidate(
            document_id=str(row.id),
            title=row.title,
            checksum=row.checksum or "",
            size_bytes=row.size_bytes,
            verdict=Verdict.exact_byte_identical,
            similarity=1.0,
            evidence="checksum_index",
            project_id=str(row.project_id) if row.project_id else None,
            version_number=row.version_number,
            active_truth_status=row.active_truth_status.value,
            requires_review=False,
        )
        for row in exact_rows
    )

    related: list[SourceCandidate] = []
    unreadable: list[str] = []
    screened_out = 0
    scanned = 0
    truncated = False

    if size > 0:
        window = max(int(size * SIZE_WINDOW_RATIO), MIN_SIZE_WINDOW_BYTES)
        low = max(0, size - window)
        high = size + window
        rows = (
            base.filter(
                Document.checksum != incoming.sha256,
                Document.size_bytes.isnot(None),
                Document.size_bytes >= low,
                Document.size_bytes <= high,
            )
            .order_by(Document.created_at.desc())
            .limit(max_scan + 1)
            .all()
        )
        truncated = len(rows) > max_scan
        rows = rows[:max_scan]
        scanned = len(rows)

        storage = get_storage()
        for row in rows:
            original = _read_original(storage, row.storage_key)
            if original is None:
                unreadable.append(str(row.id))
                continue
            assessment = compare_sources(payload, original)
            if assessment.verdict not in _RELATED_VERDICTS:
                screened_out += 1
                continue
            related.append(
                SourceCandidate(
                    document_id=str(row.id),
                    title=row.title,
                    checksum=row.checksum or "",
                    size_bytes=row.size_bytes,
                    verdict=assessment.verdict,
                    similarity=assessment.similarity,
                    evidence="size_window",
                    project_id=str(row.project_id) if row.project_id else None,
                    version_number=row.version_number,
                    active_truth_status=row.active_truth_status.value,
                    requires_review=True,
                )
            )

    related.sort(
        key=lambda c: (
            0 if c.verdict is Verdict.normalized_identical else 1,
            0 if c.verdict is Verdict.one_character_edit else 1,
            -(c.similarity or 0.0),
            c.document_id,
        )
    )

    notes = [
        "exact matches come from the indexed documents.checksum column (sha256)",
        "related candidates come from a bounded size-window scan; size_bytes is not indexed",
        "no source_relationship row was written; similarity is never recorded as identity",
        f"comparison is capped at {MAX_COMPARE_BYTES} bytes for text metrics",
    ]
    if project_id is not None:
        notes.append("lookup was scoped to one project")

    return CandidateReport(
        incoming_sha256=incoming.sha256,
        incoming_size_bytes=size,
        exact=exact,
        related=tuple(related),
        scanned_documents=scanned,
        screened_out=screened_out,
        truncated=truncated,
        unreadable_originals=tuple(unreadable),
        notes=tuple(notes),
        lookup_authoritative=bool(exact),
    )
