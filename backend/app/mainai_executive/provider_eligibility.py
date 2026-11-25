"""Deterministic provider eligibility — before any scoring, and never as authorization.

Reuses the shared catalog (``app.providers.catalog``), the shared provider registry,
the Redis circuit breaker and the provider-spend authorization. Adds no registry, no task
queue and no provider adapter.

A model is ``eligible`` only when it passes every deterministic filter. It is ``callable``
only when a live spend authorization for this goal also allows it. ``eligible`` is not
``callable``; neither is execution authorization.

Chat-model integration and coding-agent integration are separate facts: an OpenAI chat
adapter does not grant Codex CLI access, and a chat provider does not automatically support
the Z.ai development agent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.providers import circuit_breaker
from app.providers.catalog import (
    ModelSelectionPolicy,
    ModelSpec,
    evaluate_model,
    model_specs,
)
from app.providers.registry import get_provider, model_is_freshly_verified
from app.provider_spend.service import (
    get_current_provider_spend_authorization,
    provider_spend_is_live,
)

INTEGRATION_KINDS = ("chat_model", "coding_agent")


@dataclass(frozen=True)
class ModelEligibility:
    provider: str
    model: str
    locality: str
    integration_kind: str = "chat_model"
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "locality": self.locality,
            "integration_kind": self.integration_kind,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class RejectedModel:
    provider: str
    model: str
    reason: str

    def to_dict(self) -> dict:
        return {"provider": self.provider, "model": self.model, "reason": self.reason}


@dataclass(frozen=True)
class EligibilityReport:
    integration_kind: str
    policy_eligible: tuple[ModelEligibility, ...]
    callable_models: tuple[ModelEligibility, ...]
    rejected: tuple[RejectedModel, ...]
    spend_authorization_checked: bool
    authorization_required: bool
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "integration_kind": self.integration_kind,
            "policy_eligible": [item.to_dict() for item in self.policy_eligible],
            "callable_models": [item.to_dict() for item in self.callable_models],
            "rejected": [item.to_dict() for item in self.rejected],
            "spend_authorization_checked": self.spend_authorization_checked,
            "authorization_required": self.authorization_required,
            "selection_authority": "caller",
            "execution_authorized": False,
            "notes": list(self.notes),
        }


def _spend_allows(db: Session, *, owner_id: uuid.UUID, goal_id: uuid.UUID, provider: str, model: str) -> tuple[bool, str]:
    row = get_current_provider_spend_authorization(db, owner_id=owner_id, goal_id=goal_id)
    if row is None:
        return False, "no_live_spend_authorization"
    if not provider_spend_is_live(db, owner_id=owner_id, goal_id=goal_id):
        return False, "spend_authorization_exhausted_or_revoked"
    allowed_providers = list(row.allowed_providers or [])
    allowed_models = list(row.allowed_models or [])
    if allowed_providers and provider not in allowed_providers:
        return False, "provider_not_in_spend_authorization"
    if allowed_models and model not in allowed_models:
        return False, "model_not_in_spend_authorization"
    return True, "spend_authorization_allows_call"


def evaluate_chat_eligibility(
    db: Session,
    *,
    owner_id: uuid.UUID | None = None,
    goal_id: uuid.UUID | None = None,
    policy: ModelSelectionPolicy | None = None,
    specs: tuple[ModelSpec, ...] | None = None,
    check_circuit: bool = True,
) -> EligibilityReport:
    """Filter every catalog model in a fixed order and report why each was rejected.

    Order: catalog policy → provider configured → exact-model verification (when the policy
    requires it) → circuit breaker → spend authorization. The policy is caller-owned; a
    model can never relax it.
    """
    policy = policy or ModelSelectionPolicy(role="chat")
    specs = specs if specs is not None else model_specs()

    eligible: list[ModelEligibility] = []
    callable_models: list[ModelEligibility] = []
    rejected: list[RejectedModel] = []
    spend_checked = bool(owner_id is not None and goal_id is not None)

    for spec in specs:
        allowed, reason = evaluate_model(spec, policy)
        if not allowed:
            rejected.append(RejectedModel(spec.provider, spec.model_id, reason))
            continue
        provider = get_provider(spec.provider)
        if not provider.is_configured():
            rejected.append(RejectedModel(spec.provider, spec.model_id, "provider_not_configured"))
            continue
        requires_verification = policy.require_verified and (
            spec.external or policy.require_verified_local
        )
        if requires_verification and not model_is_freshly_verified(db, spec, policy.role):
            rejected.append(
                RejectedModel(spec.provider, spec.model_id, "no_fresh_exact_model_verification")
            )
            continue
        if check_circuit and circuit_breaker.is_open(spec.provider, spec.model_id):
            rejected.append(RejectedModel(spec.provider, spec.model_id, "circuit_open"))
            continue

        reasons = ["catalog_policy_passed", "provider_configured"]
        if requires_verification:
            reasons.append("fresh_exact_model_verification")
        eligible.append(
            ModelEligibility(
                provider=spec.provider,
                model=spec.model_id,
                locality=spec.locality.value,
                reasons=tuple(reasons),
            )
        )
        if spend_checked:
            allowed_spend, spend_reason = _spend_allows(
                db,
                owner_id=owner_id,  # type: ignore[arg-type]
                goal_id=goal_id,  # type: ignore[arg-type]
                provider=spec.provider,
                model=spec.model_id,
            )
            if allowed_spend:
                callable_models.append(
                    ModelEligibility(
                        provider=spec.provider,
                        model=spec.model_id,
                        locality=spec.locality.value,
                        reasons=tuple(reasons) + (spend_reason,),
                    )
                )

    notes = [
        "eligibility is a filter, not a selection or an execution authorization",
        "a model score cannot make an ineligible provider eligible",
        "chat-model eligibility says nothing about coding-agent availability",
    ]
    if not spend_checked:
        notes.append("no goal_id supplied; spend authorization was not checked")
    return EligibilityReport(
        integration_kind="chat_model",
        policy_eligible=tuple(eligible),
        callable_models=tuple(callable_models),
        rejected=tuple(rejected),
        spend_authorization_checked=spend_checked,
        authorization_required=not bool(callable_models),
        notes=tuple(notes),
    )


def fallback_plan(
    report: EligibilityReport,
    *,
    prefer_local: bool = True,
) -> dict:
    """Order the already-eligible models local-first, then cheapest, then catalog order.

    Returns a plan, not a dispatch: the caller still chooses and still passes the existing
    egress, spend and agent-authorization boundaries.
    """
    if report.integration_kind != "chat_model":
        return {
            "ordered": [],
            "reason": "fallback ordering applies to chat models only",
            "selection_authority": "caller",
            "execution_authorized": False,
        }
    # Preserve catalog order as the final tiebreaker without re-importing prices here.
    order = {f"{item.provider}/{item.model}": index for index, item in enumerate(report.policy_eligible)}
    ranked = sorted(
        report.policy_eligible,
        key=lambda item: (
            0 if (prefer_local and item.locality == "local") else 1,
            order[f"{item.provider}/{item.model}"],
        ),
    )
    return {
        "ordered": [item.to_dict() for item in ranked],
        "callable_models": [item.to_dict() for item in report.callable_models],
        "selection_authority": "caller",
        "execution_authorized": False,
        "note": "ordered eligibility only; spend, egress and agent authorization still apply",
    }


def coding_agent_eligibility(*, chat_provider: str | None = None) -> dict:
    """Describe coding-agent integration WITHOUT borrowing chat eligibility.

    The authoritative facts live in ``app.agent_coordination.adapter_config``. This function
    deliberately does not import an adapter or probe a binary; it states the separation and
    points the caller at the adapter registry.
    """
    return {
        "integration_kind": "coding_agent",
        "chat_provider": chat_provider,
        "chat_provider_grants_coding_agent": False,
        "openai_chat_adapter_grants_codex_cli": False,
        "chat_provider_grants_zai_development_agent": False,
        "authoritative_source": "app.agent_coordination.adapter_config.adapter_availability",
        "execution_authorized": False,
        "note": (
            "coding-agent eligibility must come from the adapter registry's own availability "
            "facts; a configured chat provider is not evidence of a usable coding agent"
        ),
    }
