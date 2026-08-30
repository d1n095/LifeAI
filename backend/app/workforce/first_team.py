"""First logical MainAI workforce team (Lane C).

Honest SAID vs IMPLEMENTED — do not fake capabilities.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.workforce.registry import list_workforce_agents, register_workforce_agent


@dataclass(frozen=True)
class DepartmentSpec:
    agent_key: str
    name: str
    role: str
    agent_type: str
    capability_tags: tuple[str, ...]
    trust_zone: str
    # Honesty fields:
    schema_exists: bool
    selection_logic: bool
    runtime: str  # none | in_process | provider_gated | real
    provider_model_available: str | None
    evidence_proves_capability: bool
    blocked_reason: str | None


# The first logical organization MainAI should eventually manage.
FIRST_TEAM_SPECS: tuple[DepartmentSpec, ...] = (
    DepartmentSpec(
        agent_key="dept-research",
        name="Research",
        role="lead_research",
        agent_type="RESEARCH",
        capability_tags=("web_research", "fact_gathering"),
        trust_zone="EXTERNAL_PROVIDER",
        schema_exists=True,
        selection_logic=True,
        runtime="none",
        provider_model_available=None,
        evidence_proves_capability=False,
        blocked_reason="No verified research adapter; provider activation gated",
    ),
    DepartmentSpec(
        agent_key="dept-planning",
        name="Planning",
        role="lead_planning",
        agent_type="PLANNER",
        capability_tags=("planning", "task_decomposition"),
        trust_zone="CONTROLLED_INTERNAL",
        schema_exists=True,
        selection_logic=True,
        runtime="provider_gated",
        provider_model_available="via provider_planning.RegistryPlanningAdapter (gated)",
        evidence_proves_capability=False,
        blocked_reason="Reuse provider_planning only after safety gates; not workforce-wired",
    ),
    DepartmentSpec(
        agent_key="dept-memory",
        name="Memory",
        role="lead_memory",
        agent_type="MEMORY",
        capability_tags=("memory_retrieval", "context_packaging"),
        trust_zone="LOCAL_INTERNAL",
        schema_exists=True,
        selection_logic=True,
        runtime="in_process",
        provider_model_available=None,
        evidence_proves_capability=False,
        blocked_reason="active_context/inspectable_memory exist; not yet a delegated worker role",
    ),
    DepartmentSpec(
        agent_key="dept-coding",
        name="Coding",
        role="lead_coding",
        agent_type="CODING",
        capability_tags=("repo_edit", "run_tests"),
        trust_zone="CONTROLLED_INTERNAL",
        schema_exists=True,
        selection_logic=True,
        runtime="provider_gated",
        provider_model_available="agent_coordination LocalCLIAdapter (claude-code/cursor/codex)",
        evidence_proves_capability=False,
        blocked_reason="CLI adapters exist in coordination layer; workforce broker not dispatching yet",
    ),
    DepartmentSpec(
        agent_key="dept-redteam",
        name="Testing/Red Team",
        role="lead_red_team",
        agent_type="RED_TEAM",
        capability_tags=("adversarial_test", "verification"),
        trust_zone="LOCAL_INTERNAL",
        schema_exists=True,
        selection_logic=True,
        runtime="in_process",
        provider_model_available=None,
        evidence_proves_capability=False,
        blocked_reason="Verifier role exists in broker tests; no autonomous red-team loop",
    ),
    DepartmentSpec(
        agent_key="dept-security",
        name="Security",
        role="lead_security",
        agent_type="SECURITY",
        capability_tags=("security_review", "injection_containment"),
        trust_zone="LOCAL_INTERNAL",
        schema_exists=True,
        selection_logic=True,
        runtime="in_process",
        provider_model_available=None,
        evidence_proves_capability=False,
        blocked_reason="Injection scrub + egress exist; department agent not auto-run",
    ),
    DepartmentSpec(
        agent_key="dept-finance",
        name="Cost/Finance",
        role="lead_finance",
        agent_type="FINANCE",
        capability_tags=("cost_governance", "spend_review"),
        trust_zone="LOCAL_INTERNAL",
        schema_exists=True,
        selection_logic=True,
        runtime="in_process",
        provider_model_available=None,
        evidence_proves_capability=False,
        blocked_reason="workforce.cost + provider_spend exist; no autonomous finance agent",
    ),
    DepartmentSpec(
        agent_key="dept-personal-intent",
        name="Personal Intent",
        role="lead_personal_intent",
        agent_type="DOMAIN_SPECIALIST",
        capability_tags=("personal_intent", "founder_language"),
        trust_zone="LOCAL_INTERNAL",
        schema_exists=True,
        selection_logic=True,
        runtime="none",
        provider_model_available=None,
        evidence_proves_capability=False,
        blocked_reason="Stacked personal-intent fix (#218) awaits independent Claude verification",
    ),
    DepartmentSpec(
        agent_key="dept-self-improve",
        name="Self Improvement",
        role="lead_self_improvement",
        agent_type="INTERNAL_SPECIALIST",
        capability_tags=("self_improvement", "roi_review"),
        trust_zone="LOCAL_INTERNAL",
        schema_exists=True,
        selection_logic=True,
        runtime="none",
        provider_model_available=None,
        evidence_proves_capability=False,
        blocked_reason="Acceptance docs exist; bounded self-improvement run not workforce-delegated",
    ),
)


def bootstrap_first_team(db: Session, *, owner_id: uuid.UUID) -> list[dict[str, Any]]:
    """Idempotently register first-team profiles at candidate/probation honesty levels.

    Departments with no proven capability stay `candidate` (lowest trust).
    Creating them grants ZERO authority / empty tool classes.
    """
    out: list[dict[str, Any]] = []
    for spec in FIRST_TEAM_SPECS:
        # Never mark active without evidence.
        status = "probation" if spec.runtime in ("in_process", "provider_gated") else "candidate"
        if not spec.evidence_proves_capability:
            # Even in_process departments start candidate until evidence recorded.
            status = "candidate"
        profile = register_workforce_agent(
            db,
            owner_id=owner_id,
            agent_key=spec.agent_key,
            name=spec.name,
            role=spec.role,
            agent_type=spec.agent_type,
            capability_tags=list(spec.capability_tags),
            trust_zone=spec.trust_zone,
            allowed_tool_classes=(),
            status=status,
            cost_class="unknown",
            provenance={
                "first_team": True,
                "schema_exists": spec.schema_exists,
                "selection_logic": spec.selection_logic,
                "runtime": spec.runtime,
                "provider_model_available": spec.provider_model_available,
                "evidence_proves_capability": spec.evidence_proves_capability,
                "blocked_reason": spec.blocked_reason,
                "SAID_NE_IMPLEMENTED": True,
            },
        )
        out.append(inspect_department(profile, spec))
    return out


def inspect_department(profile, spec: DepartmentSpec | None = None) -> dict[str, Any]:
    prov = dict(profile.provenance or {})
    return {
        "agent_key": profile.agent_key,
        "name": profile.name,
        "role": profile.role,
        "agent_type": profile.agent_type,
        "status": profile.status,
        "trust_zone": profile.trust_zone,
        "capabilities": list(profile.capability_tags or []),
        "allowed_tool_classes": list(profile.allowed_tool_classes or []),
        "what_actually_exists": {
            "schema": True,
            "selection_logic": prov.get("selection_logic", bool(spec and spec.selection_logic)),
            "runtime": prov.get("runtime", "none"),
            "provider_model_available": prov.get("provider_model_available"),
            "evidence_proves_capability": prov.get("evidence_proves_capability", False),
            "blocked_reason": prov.get("blocked_reason"),
        },
        "authority_on_create": False,
    }


def inspect_first_team(db: Session, *, owner_id: uuid.UUID) -> dict[str, Any]:
    agents = [
        a
        for a in list_workforce_agents(db, owner_id=owner_id, include_retired=True)
        if (a.provenance or {}).get("first_team")
    ]
    return {
        "executive": "MainAI",
        "departments": [inspect_department(a) for a in agents],
        "selection_rule": "Prefer agents with evidence_proves_capability=True; never fake runtime",
        "note": "SAID != IMPLEMENTED — candidate status until durable evidence exists",
    }
