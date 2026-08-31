"""Deep memory retrieval quality harness — FAILURE TO FIND IS INFORMATION."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.founder_memory import list_founder_memory, record_founder_memory
from app.models.founder_memory import FounderMemoryNote


@dataclass
class RetrievalCase:
    name: str
    seed_content: str
    note_type: str
    query: str
    expect_find: bool
    superseded: bool = False


@dataclass
class RetrievalQualityReport:
    cases: int
    found: int
    missed_expected: int
    false_positives: int
    failure_to_find_rate: float
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cases": self.cases,
            "found": self.found,
            "missed_expected": self.missed_expected,
            "false_positives": self.false_positives,
            "failure_to_find_rate": self.failure_to_find_rate,
            "failure_to_find_is_information": True,
            "hallucinated_match": False,
            "notes": list(self.notes),
        }


def _simple_retrieve(db: Session, *, owner_id: uuid.UUID, query: str) -> list[FounderMemoryNote]:
    """Deterministic substring retrieve over founder memory — no LLM match invention."""
    q = query.lower().strip()
    if not q:
        return []
    rows = list_founder_memory(db, owner_id=owner_id, status="active")
    hits = []
    for row in rows:
        text = (row.content or "").lower()
        # Require substantial overlap — avoid hallucinated weak matches
        tokens = [t for t in q.replace(",", " ").split() if len(t) > 2]
        if not tokens:
            continue
        overlap = sum(1 for t in tokens if t in text)
        if overlap >= max(1, len(tokens) // 2):
            hits.append(row)
    return hits


def run_retrieval_quality_suite(
    db: Session,
    *,
    owner_id: uuid.UUID,
    cases: list[RetrievalCase] | None = None,
) -> RetrievalQualityReport:
    cases = cases or _default_cases()
    found = 0
    missed = 0
    false_pos = 0
    notes: list[str] = []

    for case in cases:
        note = record_founder_memory(
            db,
            owner_id=owner_id,
            note_type=case.note_type,
            content=case.seed_content,
            idempotency_key=f"retq:{uuid.uuid4()}",
            authority="founder",
            basis="manual",
            provenance={"retrieval_case": case.name},
        )
        if case.superseded:
            record_founder_memory(
                db,
                owner_id=owner_id,
                note_type=case.note_type,
                content=f"SUPERSEDED: {case.seed_content}",
                idempotency_key=f"retq-sup:{uuid.uuid4()}",
                authority="founder",
                basis="manual",
                supersedes_note_id=note.id,
                provenance={"retrieval_case": case.name, "supersedes": True},
            )
        hits = _simple_retrieve(db, owner_id=owner_id, query=case.query)
        # Prefer active non-superseded
        active_hits = [h for h in hits if h.status == "active"]
        if case.expect_find:
            if active_hits:
                found += 1
            else:
                missed += 1
                notes.append(f"FAILURE_TO_FIND:{case.name}")
        else:
            if active_hits and any(case.seed_content[:40].lower() in (h.content or "").lower() for h in active_hits):
                # Found the seed when we expected not to (e.g. only superseded should match weakly)
                false_pos += 1
                notes.append(f"FALSE_POSITIVE:{case.name}")
            else:
                notes.append(f"CORRECT_MISS:{case.name}")

    total = len(cases)
    return RetrievalQualityReport(
        cases=total,
        found=found,
        missed_expected=missed,
        false_positives=false_pos,
        failure_to_find_rate=(missed / total) if total else 0.0,
        notes=notes,
    )


def _default_cases() -> list[RetrievalCase]:
    return [
        RetrievalCase("old_correction", "Never use MongoDB for sessions — use Postgres", "correction", "sessions Postgres", True),
        RetrievalCase("superseded_decision", "Use Redis for all queues", "decision", "Redis queues", True, superseded=True),
        RetrievalCase("unfinished_idea", "Dormant idea: hot-warm-cold memory tiers", "observation", "hot warm cold memory", True),
        RetrievalCase("failed_approach", "Tried SAME-collapse without locks — race failed", "observation", "SAME collapse race", True),
        RetrievalCase("different_wording", "Founder prefers short answers", "preference", "korta svar", False),  # Swedish query may miss English store — failure is info
        RetrievalCase("security_bug", "RLS leak fixed on work_candidates owner check", "correction", "RLS work_candidates", True),
        RetrievalCase("disproven_claim", "External claim: provider invoke is safe — DISPROVEN", "observation", "provider invoke safe", True),
        RetrievalCase("no_match", "Unrelated gardening tip about tomatoes", "observation", "workforce kill switch", False),
    ]
