"""Deterministic (no LLM), tag/title-overlap check against recently rejected ideas and work
candidates for the same owner -- REJECTED IDEA NON-RESURFACING enforcement (reconciliation
doc §5, second half / real gap #8: "disposition_reason/dismissed_reason fields exist; nothing
checks a NEW idea against recently-rejected ones before allowing it through").

`check_recently_rejected()` NEVER blocks by itself -- it returns a signal dict for a caller
(Part 2's judgment.py) to weigh alongside everything else it already knows. This module writes
nothing to the database."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.intelligence_governance import IntelligenceIdea
from app.models.work_candidate import WorkCandidate

DEFAULT_LOOKBACK_DAYS = 90
_TITLE_OVERLAP_THRESHOLD = 0.5
_WORD_RE = re.compile(r"[a-zA-Z0-9åäöÅÄÖ]+")

# WorkCandidate has no literal "rejected" status -- app.work_candidates.service's real
# vocabulary is unreviewed/authorized/dismissed/superseded (migration 0055 + 0057's own
# module docstrings). "dismissed" is the real equivalent of a founder-level rejection
# (dismiss_work_candidate()'s own docstring: "an explicit 'this candidate is not worth
# pursuing' outcome"); "superseded" is included too since it also means the row is no longer
# a live candidate the founder would want re-surfaced unprompted.
WORK_CANDIDATE_REJECTED_STATUSES = frozenset({"dismissed", "superseded"})


def _tokens(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "") if len(w) > 2}


def _overlap_ratio(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def check_recently_rejected(
    db: Session,
    *,
    owner_id: uuid.UUID,
    candidate_title: str,
    candidate_tags: list[str] | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, Any]:
    """Deterministic tag/title-overlap check (Jaccard token overlap, no LLM) against
    `IntelligenceIdea` rows with `disposition == 'rejected'` and `WorkCandidate` rows with a
    dismissed/superseded status, for the same owner, within `lookback_days`. Returns
    {"is_likely_duplicate_of_rejected": bool, "matches": [...], "reason": str, "blocks":
    False} -- `blocks` is always False; this function never itself prevents anything."""
    if lookback_days < 1:
        raise ValueError("lookback_days must be positive")
    since = datetime.utcnow() - timedelta(days=lookback_days)
    title_tokens = _tokens(candidate_title)
    tag_set = {str(t).lower() for t in (candidate_tags or [])}

    matches: list[dict[str, Any]] = []

    rejected_ideas = db.execute(
        select(IntelligenceIdea).where(
            IntelligenceIdea.owner_id == owner_id,
            IntelligenceIdea.disposition == "rejected",
            IntelligenceIdea.created_at >= since,
        )
    ).scalars().all()
    for idea in rejected_ideas:
        ratio = _overlap_ratio(title_tokens, _tokens(idea.content))
        if ratio >= _TITLE_OVERLAP_THRESHOLD:
            matches.append({
                "kind": "intelligence_idea",
                "id": str(idea.id),
                "overlap_ratio": round(ratio, 4),
                "disposition_reason": idea.disposition_reason,
                "rejected_at": idea.created_at.isoformat(),
            })

    rejected_candidates = db.execute(
        select(WorkCandidate).where(
            WorkCandidate.owner_id == owner_id,
            WorkCandidate.status.in_(WORK_CANDIDATE_REJECTED_STATUSES),
            WorkCandidate.updated_at >= since,
        )
    ).scalars().all()
    for cand in rejected_candidates:
        title_ratio = _overlap_ratio(title_tokens, _tokens(cand.title))
        tag_ratio = 0.0
        if tag_set and isinstance(cand.provenance, dict):
            existing_tags = {str(t).lower() for t in (cand.provenance.get("tags") or [])}
            tag_ratio = _overlap_ratio(tag_set, existing_tags)
        ratio = max(title_ratio, tag_ratio)
        if ratio >= _TITLE_OVERLAP_THRESHOLD:
            matches.append({
                "kind": "work_candidate",
                "id": str(cand.id),
                "overlap_ratio": round(ratio, 4),
                "status": cand.status,
                "dismissed_reason": cand.dismissed_reason,
                "rejected_at": cand.updated_at.isoformat(),
            })

    matches.sort(key=lambda m: m["overlap_ratio"], reverse=True)
    return {
        "is_likely_duplicate_of_rejected": bool(matches),
        "matches": matches,
        "reason": (
            f"{len(matches)} recently-rejected row(s) share >= {_TITLE_OVERLAP_THRESHOLD:.0%} token overlap"
            if matches
            else "no recently-rejected idea or work candidate overlaps this title/tags"
        ),
        "lookback_days": lookback_days,
        "blocks": False,
    }
