"""Thin bridge: executive cycle ↔ Local Intelligence School.

LOCAL ATTEMPT FIRST. External APIs enhance; they do not define existence.
TEACHER != TRUTH. No provider invoke here.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.mainai_school.cycle import run_learning_cycle
from app.mainai_school.evidence import evidence_policy_dict
from app.mainai_school.learning_contract import build_learning_contract
from app.mainai_school.metrics import snapshot_domain
from app.mainai_school.routing import route_local_first, routing_as_dict
from app.mainai_school.types import LocalAttempt, RouteDecision


def derive_task_class(need_capability: str) -> tuple[str, str]:
    """Map workforce capability string → (domain, task_class)."""
    cap = (need_capability or "general").strip().lower()
    if "classif" in cap or "public_text" in cap:
        return "research", "low_risk_classification"
    if "code" in cap or "implement" in cap:
        return "coding", cap[:64]
    if "secur" in cap:
        return "security", cap[:64]
    return "general", cap[:64] or "unspecified"


def run_local_first_school_step(
    db: Session,
    *,
    owner_id: UUID,
    founder_request: str,
    need_capability: str,
    local_success: bool | None,
    local_confidence: float,
    workforce_dry_run: dict[str, Any] | None,
    new_or_hard_domain: bool = False,
) -> dict[str, Any]:
    """Check local capability, attempt locally (already done upstream), record learn path.

    Does NOT call external providers. Teacher critique only if caller later supplies one.
    """
    domain, task_class = derive_task_class(need_capability)
    advice = route_local_first(
        db,
        owner_id=owner_id,
        domain=domain,
        task_class=task_class,
        local_confidence=local_confidence,
        evidence_jobs=1 if workforce_dry_run else 0,
        recent_failures=0 if local_success else 1,
        new_or_hard_domain=new_or_hard_domain,
    )

    attempt = LocalAttempt(
        domain=domain,
        task_class=task_class,
        attempt_summary=(founder_request or "")[:120],
        success=local_success,
        confidence=local_confidence,
        evidence={
            "workforce_dry": bool(workforce_dry_run),
            "provider_invoked": bool((workforce_dry_run or {}).get("provider_invoked")),
        },
    )

    # Learning cycle without teacher by default — offline-capable.
    cycle = run_learning_cycle(
        db,
        owner_id=owner_id,
        local=attempt,
        teacher=None,
        run_exam=False,
        new_or_hard_domain=new_or_hard_domain,
    )

    # If routing says teacher guided but no teacher available: stay local + note gap.
    seek_teacher = advice.use_external_teacher and advice.decision in {
        RouteDecision.TEACHER_GUIDED,
        RouteDecision.LOCAL_THEN_TEACHER_REVIEW,
        RouteDecision.SUPERVISED_LEARNING,
    }

    contract = build_learning_contract(
        domain=domain,
        task_class=task_class,
        local_attempt=attempt.attempt_summary,
        teachers=[],
        evidence=[{"rank": "DIRECTLY_OBSERVED_OUTCOME", "summary": "workforce_dry_run"}],
        verified_outcome=(
            "local_right"
            if local_success
            else ("unresolved" if local_success is None else "neither")
        ),
        practice_set=list(cycle.practice),
        capability_change=None,
    )

    return {
        "wired": True,
        "local_attempt_first": True,
        "routing": routing_as_dict(advice),
        "seek_external_teacher": seek_teacher,
        "teacher_invoked": False,
        "provider_as_default_execution": False,
        "learning_cycle": {
            "teacher_used": cycle.teacher_used,
            "lesson_id": cycle.lesson_id,
            "practice_n": len(cycle.practice),
            "weight_training_ran": False,
            "authority_widened": False,
        },
        "learning_contract": contract.as_dict(),
        "independence": snapshot_domain(domain).__dict__,
        "evidence_policy": evidence_policy_dict(),
        "external_model_is_not_mainai": True,
        "teacher_is_not_truth": True,
    }
