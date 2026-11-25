"""Understand before acting — one composed investigation over existing primitives.

This is not a second planner. It answers, in order:

1. What is the underlying objective? (interpreted, not founder-confirmed)
2. What already exists? (explicit evidence states)
3. What did we already decide or learn? (problem/decision/lesson records)
4. What is actually missing?
5. Which dependencies are affected?
6. Which realistic alternatives exist?
7. What is the evidence-supported next step?

Every step is read-only. The result is a recommendation, never execution authorization, and
it never dispatches an agent or calls a provider.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.capability_reality.service import list_capability_records
from app.mainai_execution.lessons import lookup_lessons
from app.mainai_executive.engineering_decision import CRITERIA, OPTION_KINDS, default_option_set
from app.mainai_executive.missing_piece import detect_missing_pieces
from app.problem_learning.service import (
    active_decision,
    approaches_for_problem,
    open_problems,
    useful_components_from_failed_approaches,
)

_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "is", "are",
        "be", "this", "that", "it", "we", "i", "you", "please", "should", "would", "could",
        "can", "make", "add", "improve", "fix", "build", "new", "existing", "system",
        "lifeai", "mainai", "app", "backend",
    }
)


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", text.lower())
        if token not in _STOPWORDS
    }


def _overlaps(text: str, tokens: set[str], *, minimum: int = 2) -> bool:
    return len(_tokens(text) & tokens) >= minimum


def _objective(request: str) -> dict[str, Any]:
    first_sentence = re.split(r"(?<=[.!?])\s+", request.strip(), maxsplit=1)[0]
    return {
        "interpreted_objective": first_sentence[:300] or request[:300],
        "basis": "request_text",
        "founder_confirmed": False,
        "note": "an interpreted objective is a hypothesis to confirm, not authority",
    }


def _prior_problems(db: Session, *, owner_id: uuid.UUID, tokens: set[str]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for problem in open_problems(db, owner_id=owner_id):
        if not _overlaps(f"{problem.title} {problem.description}", tokens):
            continue
        decision = active_decision(db, owner_id=owner_id, problem_id=problem.id)
        approaches = approaches_for_problem(db, owner_id=owner_id, problem_id=problem.id)
        useful = useful_components_from_failed_approaches(
            db, owner_id=owner_id, problem_id=problem.id
        )
        matches.append(
            {
                "problem_id": str(problem.id),
                "title": problem.title,
                "status": problem.status,
                "recurrence": (problem.provenance or {}).get("recurrence"),
                "active_decision": (
                    {
                        "decision_id": str(decision.id),
                        "decision": decision.decision,
                        "authority": decision.authority,
                        "basis": decision.basis,
                        "status": decision.status,
                    }
                    if decision is not None
                    else None
                ),
                "approaches": [
                    {"approach_id": str(row.id), "status": row.status, "description": row.description[:200]}
                    for row in approaches
                ],
                "useful_components_from_failed_approaches": [
                    {"component_id": str(row.id), "kind": row.component_kind, "description": row.description[:200]}
                    for row in useful
                ],
            }
        )
    return matches[:10]


def _capability_records(
    db: Session, *, owner_id: uuid.UUID, tokens: set[str], capability_keys: tuple[str, ...]
) -> list[dict[str, Any]]:
    rows = list_capability_records(db, owner_id=owner_id)
    wanted = {key.lower() for key in capability_keys}
    matches: list[dict[str, Any]] = []
    for row in rows:
        if wanted and row.capability_key.lower() not in wanted:
            continue
        if not wanted and not _overlaps(f"{row.capability_key} {row.domain}", tokens, minimum=1):
            continue
        matches.append(
            {
                "capability_key": row.capability_key,
                "domain": row.domain,
                "status": row.status,
                "status_reason": row.status_reason,
                "last_verified_at": row.last_verified_at.isoformat() if row.last_verified_at else None,
                "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
                "last_failure_at": row.last_failure_at.isoformat() if row.last_failure_at else None,
            }
        )
    return matches[:20]


def _lessons(db: Session, *, tokens: set[str]) -> list[dict[str, Any]]:
    tags = sorted(tokens)[:8] + ["executive_scan"]
    try:
        rows = lookup_lessons(db, applies_to_any=tags)
    except Exception:  # noqa: BLE001 - investigation must not fail on a lesson lookup
        return []
    return [
        {
            "lesson_id": str(row.id),
            "summary": getattr(row, "summary", None) or getattr(row, "title", None) or str(row.id),
            "applies_to": list(getattr(row, "applies_to", []) or []),
        }
        for row in rows[:10]
    ]


def _option_payload(options) -> list[dict[str, Any]]:
    return [
        {
            "kind": option.kind,
            "description": option.description,
            "criterion_scores": dict(option.criterion_scores),
            "evidence_refs": list(option.evidence_refs),
            "risks": list(option.risks),
            "dependencies": list(option.dependencies),
        }
        for option in options
    ]


def _recommendation(
    *,
    matched: list[str],
    strongest: str,
    weakest: str,
    propose_new: bool,
) -> dict[str, Any]:
    if not matched:
        action = "investigate_gap"
        reason = "no capability keyword matched; absence is not established"
    elif propose_new:
        action = "investigate_gap"
        reason = "matched capability area has no located module in this checkout"
    elif strongest == "runtime_verified":
        action = "reuse_existing"
        reason = "operational evidence exists; verify weaker siblings before changing anything"
    elif strongest in ("integration_verified", "tests_observed", "implementation_inspected"):
        action = "extend_or_repair_existing"
        reason = "implementation evidence exists; a parallel subsystem is not justified"
    else:
        action = "inspect_before_deciding"
        reason = "modules are located but not inspected"
    return {
        "action": action,
        "reason": reason,
        "weakest_state": weakest,
        "requires_human_decision": True,
        "recommendation_is_not_authorization": True,
        "execution_authorized": False,
    }


def investigate_engineering_request(
    db: Session,
    *,
    owner_id: uuid.UUID,
    request: str,
    repo_root: str | None = None,
    capability_keys: tuple[str, ...] = (),
    paths: tuple[str, ...] = (),
    runtime: Any | None = None,
    untrusted_text: str | None = None,
    untrusted_source_ref: str = "retrieved_source",
) -> dict[str, Any]:
    """Run the read-only investigation path and return an inspectable report.

    ``runtime`` is an optional ``app.context_atlas.runtime.SystemIntelligence`` instance. When
    supplied, path overlap, module dependencies and untrusted-source classification reuse that
    service; when absent, those steps are explicitly reported as not performed rather than
    guessed.
    """
    tokens = _tokens(request)
    steps: list[str] = ["objective_interpreted"]
    unverified: list[str] = []

    missing = detect_missing_pieces(founder_request=request, repo_root=repo_root)
    steps.append("existing_implementations_scanned")

    problems = _prior_problems(db, owner_id=owner_id, tokens=tokens)
    steps.append("prior_problems_and_decisions_checked")

    lessons = _lessons(db, tokens=tokens)
    steps.append("lessons_checked")

    records = _capability_records(
        db, owner_id=owner_id, tokens=tokens, capability_keys=capability_keys
    )
    steps.append("capability_records_checked")

    overlap: dict[str, Any] | None = None
    if runtime is not None and paths:
        try:
            overlap = runtime.overlap(db, owner_id=owner_id, paths=list(paths)).to_dict()
            steps.append("work_overlap_checked")
        except Exception as exc:  # noqa: BLE001
            unverified.append(f"overlap_check_failed:{type(exc).__name__}")

    dependency: dict[str, Any] | None = None
    if runtime is not None and missing["existing_modules"]:
        try:
            dependency = runtime.dependencies(missing["existing_modules"][0]).to_dict()
            steps.append("dependencies_checked")
        except Exception as exc:  # noqa: BLE001
            unverified.append(f"dependency_check_failed:{type(exc).__name__}")

    untrusted: dict[str, Any] | None = None
    if runtime is not None and untrusted_text is not None:
        untrusted = runtime.inspect_untrusted_source(untrusted_source_ref, untrusted_text).to_dict()
        steps.append("untrusted_source_classified")

    if missing["strongest_state"] != "runtime_verified":
        unverified.append("no_runtime_verified_evidence_for_this_capability")
    if overlap is None:
        unverified.append("work_overlap_not_checked")
    if dependency is None:
        unverified.append("module_dependencies_not_checked")
    if untrusted is None:
        unverified.append("no_untrusted_source_supplied")

    alternatives = default_option_set(reuse_evidence=tuple(missing["existing_modules"]))
    recommendation = _recommendation(
        matched=missing["matched_capabilities"],
        strongest=missing["strongest_state"],
        weakest=missing["weakest_state"],
        propose_new=missing["propose_new_subsystem"],
    )

    return {
        "request": request[:500],
        "objective": _objective(request),
        "investigation_steps": steps,
        "existing_implementations": missing["existing_modules"],
        "evidence_states": missing["evidence_states"],
        "evidence_detail": missing["evidence_detail"],
        "strongest_state": missing["strongest_state"],
        "weakest_state": missing["weakest_state"],
        "missing_pieces": missing["missing_pieces"],
        "prior_problems": problems,
        "lessons": lessons,
        "capability_records": records,
        "work_overlap": overlap,
        "dependency_evidence": dependency,
        "untrusted_source": untrusted,
        "alternatives": _option_payload(alternatives),
        "comparison_criteria": list(CRITERIA),
        "option_kinds": list(OPTION_KINDS),
        "recommendation": recommendation,
        "unverified_areas": unverified,
        "read_only": True,
        "provider_invoked": False,
        "execution_authorized": False,
        "note": (
            "reuse of existing components is preferred; a new component is not approved merely "
            "because this scan did not recognize an existing one"
        ),
    }
