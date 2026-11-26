"""Read-only reuse check before MainAI proposes new code.

The founder can ask for something LifeAI already has. The investigation engine
(`app.mainai_executive.investigation.investigate_engineering_request`) already knows how to
report explicit evidence states, prior problems/decisions and the atlas recommendation, but
nothing in the live chat path called it. This module is the narrow adapter that:

- runs only for requests that actually ask to build/create something (a plain question must
  not pay for a repository scan);
- delegates to that investigation instead of re-implementing capability matching;
- adds the founder's recorded project decisions from `project_memory`;
- returns a bounded, JSON-safe report whose `execution_authorized` is always false.

ANALYSIS IS NOT AUTHORITY. A `reuse` recommendation is evidence for a conversation, never
permission to skip review, dispatch an agent or merge anything.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_BUILD_INTENT_PATTERNS = (
    re.compile(
        r"\b(bygg|bygga|bygger|implementera|implementerar|skapa|skapar|utveckla|utvecklar|"
        r"lägg till|lagg till)\b"
    ),
    re.compile(r"\b(build|implement|create|develop|write|add)\b"),
)

_STOPWORDS = frozenset(
    {
        "att", "och", "det", "den", "som", "för", "med", "kan", "jag", "vill", "ska",
        "the", "and", "for", "with", "can", "you", "please", "into", "from", "that",
        "this", "new", "existing", "lifeai", "mainai",
    }
)


def looks_like_build_request(text: str) -> bool:
    """True only for explicit build/create wording, in Swedish or English."""
    lowered = (text or "").casefold()
    return any(pattern.search(lowered) for pattern in _BUILD_INTENT_PATTERNS)


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"\w{4,}", (text or "").casefold())
        if token not in _STOPWORDS
    }


def _prior_project_decisions(
    db: Session, *, request_text: str, limit: int = 3
) -> list[dict[str, Any]]:
    """The founder's own recorded decisions that share wording with this request.

    Project notes are founder-wide project state (see `app/models/project_memory.py`), and
    this function only reads them; it never promotes a note to truth or to authorization.
    """
    try:
        from app.models.project_memory import NoteKind
        from app.project_memory import list_notes

        notes = list_notes(db, kind=NoteKind.decision)
    except Exception:  # noqa: BLE001 - the reuse check must not fail on a memory read
        logger.warning("reuse check: project decisions could not be read", exc_info=True)
        return []

    tokens = _tokens(request_text)

    def _overlap(content: str) -> int:
        note_tokens = _tokens(content)
        return sum(
            1
            for token in tokens
            if any(
                token == other
                or (len(token) >= 5 and (other.startswith(token) or token.startswith(other)))
                for other in note_tokens
            )
        )

    scored = [(_overlap(note.content), note) for note in notes]
    scored = [pair for pair in scored if pair[0] > 0]
    scored.sort(key=lambda pair: (-pair[0], pair[1].created_at))
    return [
        {
            "content": note.content[:300],
            "source_type": note.source_type,
            "source_ref": note.source_ref,
            "created_at": note.created_at.isoformat() if note.created_at else None,
        }
        for _overlap, note in scored[:limit]
    ]


def check_reuse_before_build(
    db: Session,
    *,
    owner_id: uuid.UUID,
    request_text: str,
    project_id: uuid.UUID | None = None,
    runtime: Any | None = None,
    max_components: int = 8,
) -> dict[str, Any] | None:
    """Assess a build request against the real checkout, or return None.

    None means "this was not a build request"; it is not a verdict that nothing exists.
    A failed scan also returns None: the chat must degrade to an ordinary answer rather
    than inventing reuse evidence.
    """
    if not looks_like_build_request(request_text):
        return None

    repo_root: str | None = None
    try:
        from app.config import get_settings
        from app.context_atlas.runtime import SystemIntelligence, default_repo_root

        repo_root = get_settings().project_root or str(default_repo_root())
        if runtime is None:
            runtime = SystemIntelligence(repo_root)
    except Exception:  # noqa: BLE001 - no checkout means no reuse evidence, not a crash
        logger.warning("reuse check: system intelligence unavailable", exc_info=True)
        runtime = None

    try:
        from app.mainai_executive.investigation import investigate_engineering_request

        report = investigate_engineering_request(
            db,
            owner_id=owner_id,
            request=request_text,
            repo_root=repo_root,
            runtime=runtime,
            project_ref=str(project_id) if project_id else "unknown",
        )
    except Exception:  # noqa: BLE001 - a broken investigation must not break chat
        logger.warning("reuse check: investigation failed", exc_info=True)
        return None

    decision = report.get("decision_support") or {}
    recommendation = decision.get("recommendation") or {}
    fallback = report.get("recommendation") or {}
    existing = set(report.get("existing_implementations") or [])
    components = [
        detail
        for detail in (report.get("evidence_detail") or [])
        if detail.get("module") in existing
    ][:max_components]

    return {
        "status": "assessed",
        "request": report.get("request"),
        "outcome": recommendation.get("outcome"),
        "current_state": recommendation.get("current_state"),
        "next_action": recommendation.get("next_action") or fallback.get("action"),
        "existing_implementations": sorted(existing),
        "components": components,
        "missing_pieces": list(report.get("missing_pieces") or []),
        "prior_problems": list(report.get("prior_problems") or [])[:3],
        "prior_project_decisions": _prior_project_decisions(db, request_text=request_text),
        "work_overlap": report.get("work_overlap"),
        "unverified_areas": list(report.get("unverified_areas") or []),
        "read_only": True,
        "execution_authorized": False,
    }


def format_reuse_instructions(report: dict[str, Any], *, max_components: int = 8) -> str:
    """Render the bounded prompt block. Empty string when there is nothing to say."""
    if not report:
        return ""
    lines = ["ÅTERANVÄNDNINGSKONTROLL (läs-only; ger ingen exekveringsbehörighet):"]
    outcome = report.get("outcome") or "okänt"
    lines.append(f"- Utfall: {outcome}.")
    if report.get("current_state"):
        lines.append(f"- Nuvarande läge: {report['current_state']}")

    components = report.get("components") or []
    if components:
        lines.append("- Befintlig implementation (modul: evidensnivå; spårbara referenser):")
        for component in components[:max_components]:
            refs = [component.get("source_path"), *(component.get("test_references") or [])]
            refs = [ref for ref in refs if ref][:3]
            suffix = f" — {', '.join(refs)}" if refs else ""
            lines.append(
                f"  - {component.get('module')}: {component.get('state')}{suffix}"
            )

    for decision in report.get("prior_project_decisions") or []:
        lines.append(
            f"- Tidigare grundarbeslut: {decision.get('content')} "
            f"({decision.get('source_type')} {decision.get('source_ref')})"
        )
    for problem in report.get("prior_problems") or []:
        latest = problem.get("latest_decision") or {}
        lines.append(
            f"- Tidigare problem: {problem.get('title')} "
            f"(senaste beslut: {latest.get('status') or 'inget'})"
        )

    overlap = report.get("work_overlap") or {}
    ownership = overlap.get("ownership") or {}
    if ownership.get("status"):
        lines.append(f"- Pågående ägarskap: {ownership.get('status')}.")

    if report.get("next_action"):
        lines.append(f"- Föreslaget nästa steg: {report['next_action']}")
    unverified = report.get("unverified_areas") or []
    if unverified:
        lines.append("- Inte verifierat: " + ", ".join(str(item) for item in unverified[:5]))
    lines.append(
        "- Presentera återanvändning eller förbättring med referenserna ovan. Analysen är "
        "inte ett beslut och inte exekveringsbehörighet."
    )
    return "\n".join(lines)
