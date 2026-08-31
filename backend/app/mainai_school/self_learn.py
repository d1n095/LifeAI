"""Self-teaching without external APIs — validators, history, peer debate, simulators.

External teacher is optional. AGENT OUTPUT ≠ AUTOMATIC TRUTH.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SelfTeachPlan:
    domain: str
    method: str
    steps: list[str]
    requires_external_api: bool = False
    evidence_required: bool = True
    notes: dict[str, Any] = field(default_factory=dict)


def plan_self_teaching(
    *,
    domain: str,
    weakness: str,
    has_deterministic_validator: bool = False,
    has_historical_outcomes: bool = False,
    has_documents: bool = False,
    has_peer_agents: bool = False,
    has_simulator: bool = False,
) -> SelfTeachPlan:
    steps: list[str] = [f"define practice cases for: {weakness[:80]}"]
    method = "practice_only"
    if has_deterministic_validator:
        method = "validator_driven"
        steps.append("score attempts with deterministic validator")
    if has_historical_outcomes:
        steps.append("compare against historical verified outcomes")
        method = "history_plus_" + method if method != "practice_only" else "history_driven"
    if has_documents:
        steps.append("extract principles from local documents (not memorize prose)")
    if has_simulator:
        steps.append("run adversarial simulator variants")
    if has_peer_agents:
        steps.append("local peer critique — peer output remains untrusted evidence")
    steps.extend(
        [
            "generate variation practice (not exact memorization)",
            "independent exam without teacher context",
            "promote competence only with multi-pass evidence",
        ]
    )
    return SelfTeachPlan(
        domain=domain,
        method=method,
        steps=steps,
        requires_external_api=False,
        evidence_required=True,
        notes={
            "external_teacher_optional": True,
            "agent_output_is_not_automatic_truth": True,
        },
    )


def classify_failure_layer(failure_kind: str) -> str:
    """Failure-driven school: fix the correct layer — don't train a model for a broken query."""
    mapping = {
        "knowledge": "level1_memory",
        "retrieval": "level3_retrieval",
        "reasoning": "level2_playbook",
        "tool": "level4_tools",
        "planning": "level2_playbook",
        "verification": "level2_verification",
        "language": "level1_founder_language",
        "authority": "do_not_widen_authority",
        "stale": "level1_refresh",
        "bad_teacher": "teacher_evaluation",
        "bad_curriculum": "curriculum_engine",
        "broken_query": "fix_code_not_train_model",
    }
    return mapping.get(failure_kind, "unknown_inspect_before_training")
