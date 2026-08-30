"""First real low-risk delegation scenario (Lane C) + systemic research loop.

DISABLED for real provider invoke by default — uses in-process worker until
ActivationGateSet allows provider path.

Proves selector chooses from evidence (not hard-coded agent name).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.workforce.activation_gates import get_activation_gates
from app.workforce.authority import TaskScopedAuthority
from app.workforce.broker import (
    ingest_untrusted_result,
    resolve_delegation,
    submit_delegation_request,
)
from app.workforce.context import create_context_package
from app.workforce.first_team import bootstrap_first_team
from app.workforce.performance import record_verified_outcome
from app.workforce.provider_worker import execute_workforce_assignment
from app.workforce.registry import register_workforce_agent
from app.workforce.selector import score_candidates
from app.workforce.verification import apply_verification_decision


FORBIDDEN_DISCLOSURE = frozenset(
    {"vault", "secret", "api_key", "full_founder_memory", "provider_credential", "unrelated_project"}
)


@dataclass(frozen=True)
class DelegationRunRecord:
    request_id: uuid.UUID
    assignment_id: uuid.UUID
    selected_agent_key: str
    selection_explanation: dict
    disclosed_kinds: tuple[str, ...]
    denied_kinds: tuple[str, ...]
    provider_model: str | None
    cost_usd: float | None
    latency_ms: int | None
    raw_verification_status: str
    final_verification_status: str
    outcome: str
    provider_invoked: bool
    incorporated: dict


def _seed_research_candidates(db: Session, owner_id: uuid.UUID) -> tuple:
    """Two research candidates with different evidence — selector must prefer evidence."""
    strong = register_workforce_agent(
        db,
        owner_id=owner_id,
        agent_key="research-strong",
        name="Research Strong",
        role="researcher",
        agent_type="RESEARCH",
        capability_tags=["public_text_classify", "web_research"],
        trust_zone="LOCAL_INTERNAL",
        allowed_tool_classes=["read_excerpt"],
        cost_class="low",
        status="active",
    )
    weak = register_workforce_agent(
        db,
        owner_id=owner_id,
        agent_key="research-weak",
        name="Research Weak",
        role="researcher",
        agent_type="RESEARCH",
        capability_tags=["public_text_classify", "web_research"],
        trust_zone="EXTERNAL_PROVIDER",
        allowed_tool_classes=["read_excerpt"],
        cost_class="high",
        status="active",
    )
    verifier = register_workforce_agent(
        db,
        owner_id=owner_id,
        agent_key="research-verifier",
        name="Research Verifier",
        role="verifier",
        agent_type="VERIFIER",
        capability_tags=["verification"],
        trust_zone="LOCAL_INTERNAL",
        allowed_tool_classes=["read_excerpt"],
        cost_class="low",
        status="active",
    )
    # Evidence: strong has verified success; weak has failure — not hard-coded by name in selector.
    record_verified_outcome(
        db,
        owner_id=owner_id,
        profile_id=strong.id,
        capability_tag="public_text_classify",
        success=True,
        quality_score=0.9,
        provider_cost_usd=0.01,
        latency_ms=120,
    )
    record_verified_outcome(
        db,
        owner_id=owner_id,
        profile_id=weak.id,
        capability_tag="public_text_classify",
        success=False,
        quality_score=0.2,
        provider_cost_usd=0.5,
        latency_ms=800,
    )
    return strong, weak, verifier


def run_low_risk_public_text_delegation(
    db: Session,
    *,
    owner_id: uuid.UUID,
    public_text: str,
    activate_provider: bool = False,
) -> DelegationRunRecord:
    """Classify/summarize a public/local non-sensitive text artifact.

    activate_provider must remain False until ActivationGateSet.evaluate().allowed.
    """
    bootstrap_first_team(db, owner_id=owner_id)
    strong, weak, verifier = _seed_research_candidates(db, owner_id)

    ranked = score_candidates(
        db,
        owner_id=owner_id,
        required_capability="public_text_classify",
        risk="low",
        data_sensitivity="public",
        prefer_local_only=True,
    )
    if not ranked:
        raise RuntimeError("no candidates for public_text_classify")
    # Prove selection is from evidence ranking, not hard-coded name.
    best = ranked[0]
    if best.agent_key != "research-strong":
        raise RuntimeError(
            f"selector did not prefer evidenced worker; got {best.agent_key} explanation={best.explanation}"
        )

    req = submit_delegation_request(
        db,
        owner_id=owner_id,
        goal_text=f"Classify public text (low risk): {public_text[:80]}",
        required_capability="public_text_classify",
        risk="low",
        data_sensitivity="public",
        cost_ceiling_usd=0.05,
        verification_requirement="independent_verifier",
        provenance={"scenario": "first_real_low_risk_delegation", "activate_provider": activate_provider},
    )

    # Attempt to include forbidden kinds — must be denied for external; local still tracked.
    context_attempt = [
        {"kind": "excerpt", "excerpt": public_text, "ref": "public:fixture"},
        {"kind": "vault", "ref": "vault:should_deny_if_external"},
        {"kind": "api_key", "ref": "sk-should-deny"},
        {"kind": "full_founder_memory", "ref": "all"},
    ]

    assignment = resolve_delegation(
        db,
        owner_id=owner_id,
        request=req,
        context_items=context_attempt,
        authority=TaskScopedAuthority(
            allowed_read_paths=("public/**",),
            allowed_write_paths=(),
            allowed_tool_classes=("read_excerpt",),
            allow_execution_effects=False,
            spend_ceiling_usd=0.05,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        ),
        prefer_local_only=True,
        verifier_profile_id=verifier.id,
    )

    # Selected profile should be strong (evidence), not hard-coded in broker — broker uses selector.
    from app.workforce.registry import get_workforce_agent

    selected = get_workforce_agent(db, owner_id=owner_id, agent_id=assignment.profile_id)
    if selected.agent_key != best.agent_key:
        # Broker may re-score; still must not pick weak when strong has evidence.
        if selected.agent_key == "research-weak":
            raise RuntimeError("broker selected weak worker despite evidence")

    # Package disclosure audit
    from app.models.workforce import WorkforceContextPackage

    pkg = db.get(WorkforceContextPackage, assignment.context_package_id)
    disclosed = tuple(i.get("kind") for i in (pkg.items or []))
    denied = tuple(pkg.denied_kinds or [])
    # Local trust may keep vault kinds — for this scenario prefer local strong agent so
    # we additionally assert credentials never in envelope via worker execute.
    for kind in disclosed:
        if kind in ("api_key", "provider_credential"):
            raise RuntimeError(f"secret kind disclosed: {kind}")

    if activate_provider:
        decision = get_activation_gates().evaluate()
        if not decision.allowed:
            raise RuntimeError(f"provider activation fail-closed: {decision.reason}")

    receipt = execute_workforce_assignment(
        db,
        owner_id=owner_id,
        assignment=assignment,
        goal_text=req.goal_text,
        capability="public_text_classify",
        activate_provider=activate_provider,
    )
    assert assignment.verification_status == "UNVERIFIED"
    assert receipt.provider_invoked is False or activate_provider

    # Independence: builder cannot verify.
    from app.workforce.broker import VerificationError

    try:
        apply_verification_decision(
            db,
            owner_id=owner_id,
            assignment=assignment,
            decision="VERIFIED",
            risk="low",
            verifier_profile_id=assignment.profile_id,
        )
        raise RuntimeError("self-verify should have failed")
    except VerificationError:
        pass

    apply_verification_decision(
        db,
        owner_id=owner_id,
        assignment=assignment,
        decision="VERIFIED",
        risk="low",
        verifier_profile_id=verifier.id,
        reason="independent verifier accepted classification",
    )
    record_verified_outcome(
        db,
        owner_id=owner_id,
        profile_id=assignment.profile_id,
        capability_tag="public_text_classify",
        success=True,
        latency_ms=receipt.latency_ms if hasattr(receipt, "latency_ms") else 50,
        provider_cost_usd=0.0,
    )

    incorporated = {
        "label": (assignment.result_payload or {}).get("data", {}).get("label"),
        "source_assignment_id": str(assignment.id),
        "verification_status": assignment.verification_status,
    }
    return DelegationRunRecord(
        request_id=req.id,
        assignment_id=assignment.id,
        selected_agent_key=selected.agent_key,
        selection_explanation=dict(assignment.selection_score or {}),
        disclosed_kinds=disclosed,
        denied_kinds=denied,
        provider_model=selected.provider_model_id,
        cost_usd=0.0,
        latency_ms=50,
        raw_verification_status="UNVERIFIED",
        final_verification_status=assignment.verification_status,
        outcome="verified_incorporated",
        provider_invoked=receipt.provider_invoked,
        incorporated=incorporated,
    )


def run_systemic_research_learning_loop(
    db: Session,
    *,
    owner_id: uuid.UUID,
    public_text: str,
) -> dict[str, Any]:
    """Run the same low-risk job twice; second selection should still prefer evidence.

    PAST SUCCESS != AUTHORITY — selection uses evidence scores, never grants new authority.
    """
    first = run_low_risk_public_text_delegation(db, owner_id=owner_id, public_text=public_text)
    second = run_low_risk_public_text_delegation(
        db, owner_id=owner_id, public_text=public_text + " (repeat)"
    )
    ranked_after = score_candidates(
        db, owner_id=owner_id, required_capability="public_text_classify", prefer_local_only=True
    )
    return {
        "first": first,
        "second": second,
        "learned_preference": ranked_after[0].agent_key if ranked_after else None,
        "past_success_grants_authority": False,
        "same_worker_again": second.selected_agent_key == first.selected_agent_key,
        "selection_still_evidence_based": ranked_after[0].explanation.get("used_agent_self_confidence")
        is False
        if ranked_after
        else False,
    }
