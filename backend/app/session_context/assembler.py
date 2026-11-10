"""Session-start context assembler — a compact, deterministic, source-attributed project context
for the start or resume of a Life session. Implements
docs/LIFE_SESSION_CONTEXT_ASSEMBLY_DESIGN_2026-11-09.md.

REUSE, NO NEW STORE. It assembles EXISTING canonical sources and writes nothing — no table, no
migration, no second Founder Vault / memory / task-ledger:
  * mainai_startup_readiness.evaluate_startup_readiness  (may-I-act level)
  * mainai_executive.continuity                          (resume the last checkpoint)
  * active_context.current_members                       (what already matters)
  * founder_memory.list_current_founder_memory           (active-only founder truth)
  * inspectable_memory.list_truth_claim_violations       (disputed/contradicted memory)
  * capability_reality.list_capability_gaps              (what is NOT verified)
  * agent_coordination AgentWorkAssignment               (active assignments + blockers)
  * a canonical project-status reference (LIFEAI_STATUS.md)

Doctrine:
  - Deterministic, fixed section order (reproducible session start).
  - Source-attributed: every item names its source module + a row id/ref for bounded follow-up.
  - Current-truth-aware: founder memory is active-only; an open truth-claim violation downgrades
    the whole bundle to BLOCKED (never silently acted on).
  - FAIL CLOSED: if a CRITICAL source read raises, the bundle is BLOCKED and names the source —
    never a partial bundle presented as complete.
  - Token/size budget: over budget, NON-safety sections are trimmed (readiness + truth-claim
    violations + blocked_reasons are never dropped); `truncated` is set.
  - Owner-scoped throughout (startup readiness is system config, not another owner's data).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

READINESS_BLOCKED = "BLOCKED"
_DEFAULT_TOKEN_BUDGET = 4000
_DEFAULT_MAX_ITEMS = 20
_MAX_TEXT = 300

# Sections that must NEVER be dropped by a token-budget trim — they carry the safety signal.
_SAFETY_SECTIONS = frozenset({"startup_readiness", "truth_claim_violations"})


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _trim(value) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1] + "…"


@dataclass(frozen=True)
class SessionContextSection:
    name: str
    source_module: str
    reference: str  # how to fetch MORE of this source (bounded follow-up retrieval)
    items: tuple[dict, ...] = ()
    error: str | None = None  # set iff this source read failed

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "source_module": self.source_module,
            "reference": self.reference,
            "items": list(self.items),
            "error": self.error,
        }


@dataclass(frozen=True)
class SessionContextBundle:
    owner_id: str
    session_id: str
    generated_at: str
    readiness: str
    blocked: bool
    blocked_reasons: tuple[str, ...]
    sections: tuple[SessionContextSection, ...]
    estimated_tokens: int
    truncated: bool

    def to_dict(self) -> dict:
        return {
            "owner_id": self.owner_id,
            "session_id": self.session_id,
            "generated_at": self.generated_at,
            "readiness": self.readiness,
            "blocked": self.blocked,
            "blocked_reasons": list(self.blocked_reasons),
            "sections": [s.to_dict() for s in self.sections],
            "estimated_tokens": self.estimated_tokens,
            "truncated": self.truncated,
        }

    def section(self, name: str) -> SessionContextSection | None:
        return next((s for s in self.sections if s.name == name), None)


def _safe_items(
    fn: Callable[[], list[dict]],
) -> tuple[tuple[dict, ...], str | None]:
    """Run a source read, returning (items, error). Never raises — the caller decides whether a
    given source is critical (its error blocks) or best-effort (its error is recorded only)."""
    try:
        return tuple(fn()), None
    except Exception as exc:  # noqa: BLE001 - the whole point is to fail closed, not crash
        return (), f"{type(exc).__name__}: {exc}"


def assemble_session_context(
    db: Session,
    *,
    owner_id: uuid.UUID,
    session_id: str,
    token_budget: int = _DEFAULT_TOKEN_BUDGET,
    max_items_per_section: int = _DEFAULT_MAX_ITEMS,
    claude_reviews_satisfied: bool | None = None,
) -> SessionContextBundle:
    blocked = False
    blocked_reasons: list[str] = []
    sections: list[SessionContextSection] = []

    # --- 1. Startup readiness (CRITICAL, system-level) ---------------------------------------
    readiness_level = READINESS_BLOCKED

    def _readiness_items() -> list[dict]:
        nonlocal readiness_level
        from app.mainai_startup_readiness import evaluate_startup_readiness

        report = evaluate_startup_readiness(claude_reviews_satisfied=claude_reviews_satisfied)
        readiness_level = getattr(report.level, "value", str(report.level))
        return [
            {
                "source_module": "mainai_startup_readiness",
                "source_ref": "evaluate_startup_readiness",
                "level": readiness_level,
                "blocking": list(report.blocking),
            }
        ]

    items, err = _safe_items(_readiness_items)
    if err is not None:
        blocked = True
        blocked_reasons.append("source_unavailable:startup_readiness")
        readiness_level = READINESS_BLOCKED
    sections.append(SessionContextSection("startup_readiness", "mainai_startup_readiness",
                                          "evaluate_startup_readiness()", items, err))

    # --- 2. Continuity checkpoint (best-effort: a new session may have none) ------------------
    def _continuity_items() -> list[dict]:
        from app.mainai_executive.continuity import load_continuity_checkpoint, resume_summary

        cp = load_continuity_checkpoint(db, owner_id=owner_id, session_id=session_id)
        if cp is None:
            return []
        return [{"source_module": "mainai_executive.continuity", "source_ref": session_id,
                 "resume": resume_summary(cp)}]

    items, err = _safe_items(_continuity_items)
    sections.append(SessionContextSection("continuity", "mainai_executive.continuity",
                                          f"load_continuity_checkpoint(owner, '{session_id}')", items[:max_items_per_section], err))

    # --- 3. Active context members (best-effort) ---------------------------------------------
    def _active_context_items() -> list[dict]:
        from app.active_context.service import current_members
        from app.models.active_context import ActiveContextSet

        cs = db.execute(
            select(ActiveContextSet)
            .where(ActiveContextSet.owner_id == owner_id)
            .order_by(ActiveContextSet.created_at.desc())
        ).scalars().first()
        if cs is None:
            return []
        out: list[dict] = []
        for m in current_members(db, owner_id=owner_id, context_set_id=cs.id):
            out.append({
                "source_module": "active_context",
                "source_ref": str(getattr(m, "id", "")),
                "object_type": getattr(m, "object_type", None),
                "object_ref": str(getattr(m, "object_ref", getattr(m, "member_ref_id", ""))),
            })
        return out

    items, err = _safe_items(_active_context_items)
    sections.append(SessionContextSection("active_context", "active_context",
                                          "active_context.current_members(latest set)", items[:max_items_per_section], err))

    # --- 4. Current founder memory (CRITICAL, active-only) ------------------------------------
    def _founder_memory_items() -> list[dict]:
        from app.founder_memory.service import list_current_founder_memory

        out: list[dict] = []
        for n in list_current_founder_memory(db, owner_id=owner_id):
            out.append({
                "source_module": "founder_memory",
                "source_ref": str(n.id),
                "note_type": getattr(getattr(n, "note_type", None), "value", str(getattr(n, "note_type", ""))),
                "authority": getattr(getattr(n, "authority", None), "value", str(getattr(n, "authority", ""))),
                "content": _trim(getattr(n, "content", None)),
            })
        return out

    items, err = _safe_items(_founder_memory_items)
    if err is not None:
        blocked = True
        blocked_reasons.append("source_unavailable:founder_memory")
    sections.append(SessionContextSection("founder_memory", "founder_memory",
                                          "founder_memory.list_current_founder_memory(owner)", items[:max_items_per_section], err))

    # --- 5. Truth-claim violations (CRITICAL; presence BLOCKS) --------------------------------
    def _violation_items() -> list[dict]:
        from app.inspectable_memory.service import list_truth_claim_violations

        out: list[dict] = []
        for v in list_truth_claim_violations(db, owner_id=owner_id):
            out.append({
                "source_module": "inspectable_memory",
                "source_ref": str(getattr(v, "id", "")),
                "target_kind": getattr(v, "target_kind", None),
                "target_id": str(getattr(v, "target_id", "")),
                "claimed_state": getattr(getattr(v, "claimed_state", None), "value", str(getattr(v, "claimed_state", ""))),
            })
        return out

    items, err = _safe_items(_violation_items)
    if err is not None:
        blocked = True
        blocked_reasons.append("source_unavailable:truth_claim_violations")
    elif items:
        blocked = True
        blocked_reasons.append(f"unresolved_truth_claim_violations:{len(items)}")
    sections.append(SessionContextSection("truth_claim_violations", "inspectable_memory",
                                          "inspectable_memory.list_truth_claim_violations(owner)", items, err))

    # --- 6. Capability gaps (CRITICAL) -------------------------------------------------------
    def _gap_items() -> list[dict]:
        from app.capability_reality.service import list_capability_gaps

        out: list[dict] = []
        for g in list_capability_gaps(db, owner_id=owner_id):
            out.append({
                "source_module": "capability_reality",
                "source_ref": str(getattr(g, "id", getattr(g, "capability_key", ""))),
                "capability_key": getattr(g, "capability_key", None),
                "status": getattr(getattr(g, "status", None), "value", str(getattr(g, "status", ""))),
            })
        return out

    items, err = _safe_items(_gap_items)
    if err is not None:
        blocked = True
        blocked_reasons.append("source_unavailable:capability_reality")
    sections.append(SessionContextSection("capability_gaps", "capability_reality",
                                          "capability_reality.list_capability_gaps(owner)", items[:max_items_per_section], err))

    # --- 7. Active assignments + blockers (best-effort; canonical coordination source) --------
    def _assignment_items() -> list[dict]:
        from app.models.agent_coordination import AgentWorkAssignment, WorkAssignmentStatus

        terminal = {
            WorkAssignmentStatus.completed,
            WorkAssignmentStatus.failed,
            WorkAssignmentStatus.cancelled,
            WorkAssignmentStatus.superseded,
        }
        rows = db.execute(
            select(AgentWorkAssignment).where(
                AgentWorkAssignment.owner_id == owner_id,
                AgentWorkAssignment.status.notin_(terminal),
            )
        ).scalars().all()
        return [
            {
                "source_module": "agent_coordination",
                "source_ref": str(a.id),
                "status": getattr(getattr(a, "status", None), "value", str(getattr(a, "status", ""))),
                "branch": getattr(a, "branch", None),
                "base_sha": getattr(a, "base_sha", None),
                "approval_required": getattr(a, "approval_required", None),
            }
            for a in rows
        ]

    items, err = _safe_items(_assignment_items)
    sections.append(SessionContextSection("active_assignments", "agent_coordination",
                                          "agent_coordination active AgentWorkAssignment(owner)", items[:max_items_per_section], err))

    # --- 8. Canonical project-status reference (pointer, not a copy) --------------------------
    sections.append(SessionContextSection(
        "project_status", "docs",
        "LIFEAI_STATUS.md",
        ({"source_module": "docs", "source_ref": "LIFEAI_STATUS.md",
          "note": "canonical living project status (see also LIFE_* readiness docs)"},),
        None,
    ))

    readiness = READINESS_BLOCKED if blocked else readiness_level

    # --- Token/size budget: trim non-safety sections until within budget (safety never dropped).
    def _tokens(secs: list[SessionContextSection]) -> int:
        return _estimate_tokens(json.dumps([s.to_dict() for s in secs], default=str))

    truncated = False
    budgeted = list(sections)
    while _tokens(budgeted) > token_budget:
        # Find the largest trimmable (non-safety) section with items and drop one item.
        candidates = [
            (i, s) for i, s in enumerate(budgeted)
            if s.name not in _SAFETY_SECTIONS and s.items
        ]
        if not candidates:
            break
        i, s = max(candidates, key=lambda pair: len(pair[1].items))
        budgeted[i] = SessionContextSection(s.name, s.source_module, s.reference, s.items[:-1], s.error)
        truncated = True

    return SessionContextBundle(
        owner_id=str(owner_id),
        session_id=session_id,
        generated_at=_utcnow_iso(),
        readiness=readiness,
        blocked=blocked,
        blocked_reasons=tuple(blocked_reasons),
        sections=tuple(budgeted),
        estimated_tokens=_tokens(budgeted),
        truncated=truncated,
    )
