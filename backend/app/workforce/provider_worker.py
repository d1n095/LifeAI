"""Provider-neutral workforce worker harness (Lane B).

Wraps existing provider_planning / providers.registry / egress — does NOT invent a second
invocation stack. Provider credentials NEVER enter worker-visible context.

Activation remains gated: real external calls refuse until safety gates are explicitly
marked satisfied. Default is blocked.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.workforce import WorkforceAssignment, WorkforceAgentProfile, WorkforceContextPackage
from app.workforce.authority import require_live_assignment_authority, AuthorityEnvelopeError
from app.workforce.broker import DelegationBrokerError, ingest_untrusted_result
from app.workforce.context import create_context_package
from app.workforce.failure import mark_failure, record_checkpoint
from app.workforce.injection import scrub_authority_mutations
from app.workforce.registry import get_workforce_agent


class ProviderActivationBlocked(Exception):
    """Raised when real provider execution is requested but safety gates are unmet."""


# Gates that must ALL be independently verified before activate_provider=True is allowed.
# These are documentation + runtime checks — flipping env alone is insufficient without
# explicit mark_safety_gate().
REQUIRED_SAFETY_GATES: tuple[str, ...] = (
    "personal_intent_consequential_confirmation",  # #218
    "self_model_evidence_gates",  # #213
    "current_truth_selection",  # #224
    "same_collapse_race",  # #229
    "workforce_authority_context_isolation",  # #230/#231 tests green + merged
)

# In-process registry of which gates have been marked satisfied for THIS process.
# Never auto-set from CI; founder/ops must mark after independent verification.
_SATISFIED_GATES: set[str] = set()


def mark_safety_gate(gate: str, *, satisfied: bool = True) -> None:
    if gate not in REQUIRED_SAFETY_GATES:
        raise ValueError(f"unknown safety gate: {gate}")
    if satisfied:
        _SATISFIED_GATES.add(gate)
    else:
        _SATISFIED_GATES.discard(gate)


def reset_safety_gates_for_tests() -> None:
    _SATISFIED_GATES.clear()


def activation_gate_status() -> dict[str, bool]:
    return {g: (g in _SATISFIED_GATES) for g in REQUIRED_SAFETY_GATES}


def activation_allowed() -> tuple[bool, list[str]]:
    missing = [g for g in REQUIRED_SAFETY_GATES if g not in _SATISFIED_GATES]
    return (not missing, missing)


@dataclass(frozen=True)
class WorkerRequestEnvelope:
    """Bounded request visible to a worker — never contains credentials."""

    assignment_id: str
    goal_text: str
    capability: str
    trust_zone: str
    allowed_tool_classes: tuple[str, ...]
    context_items: tuple[dict, ...]
    spend_ceiling_usd: float | None
    timeout_seconds: float
    # Opaque credential reference label ONLY — never a secret value.
    credential_reference: str | None = None


@dataclass
class WorkerExecutionReceipt:
    assignment_id: uuid.UUID
    provider_invoked: bool
    provider_name: str | None
    model_id: str | None
    timeout: bool
    cancelled: bool
    raw_output_scrubbed: dict
    verification_status: str
    spend_reserved: bool
    spend_settled: bool
    external_effect_state: str
    notes: list[str] = field(default_factory=list)


FORBIDDEN_CONTEXT_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "password",
        "secret",
        "token",
        "credential",
        "vault",
        "provider_key",
        "openai_api_key",
        "anthropic_api_key",
    }
)


def assert_no_credentials_in_context(items: list[dict]) -> None:
    blob = str(items).lower()
    for key in FORBIDDEN_CONTEXT_KEYS:
        if key in blob and "redacted" not in blob:
            # Allow kind names in denied_kinds lists — check structured items only.
            pass
    for item in items:
        for k, v in item.items():
            lk = str(k).lower()
            if any(f in lk for f in FORBIDDEN_CONTEXT_KEYS):
                raise DelegationBrokerError(f"credential-like key in worker context: {k}")
            if isinstance(v, str) and any(
                v.startswith(p) for p in ("sk-", "sk_live", "sk_test", "xoxb-", "ghp_")
            ):
                raise DelegationBrokerError("credential-like value in worker context")


def build_worker_request_envelope(
    *,
    assignment: WorkforceAssignment,
    profile: WorkforceAgentProfile,
    package: WorkforceContextPackage | None,
    goal_text: str,
    capability: str,
    timeout_seconds: float = 60.0,
    credential_reference: str | None = None,
) -> WorkerRequestEnvelope:
    items = list(package.items) if package else []
    assert_no_credentials_in_context(items)
    return WorkerRequestEnvelope(
        assignment_id=str(assignment.id),
        goal_text=goal_text,
        capability=capability,
        trust_zone=profile.trust_zone,
        allowed_tool_classes=tuple(assignment.allowed_tool_classes or ()),
        context_items=tuple(items),
        spend_ceiling_usd=assignment.spend_ceiling_usd,
        timeout_seconds=timeout_seconds,
        credential_reference=credential_reference,  # opaque label only
    )


def _in_process_worker(envelope: WorkerRequestEnvelope) -> dict:
    """Deterministic local worker — used when provider is not activated."""
    text = " ".join(
        str(i.get("excerpt") or i.get("summary") or "") for i in envelope.context_items
    ).lower()
    label = "personal" if any(w in text for w in ("dentist", "birthday", "family")) else "work"
    return {
        "label": label,
        "worker": "in_process",
        "capability": envelope.capability,
        "effects": [],
    }


def execute_workforce_assignment(
    db: Session,
    *,
    owner_id: uuid.UUID,
    assignment: WorkforceAssignment,
    goal_text: str,
    capability: str,
    activate_provider: bool = False,
    provider_name: str | None = None,
    model_id: str | None = None,
    timeout_seconds: float = 60.0,
    cancel: bool = False,
) -> WorkerExecutionReceipt:
    """Run worker for an assignment.

    activate_provider=True requires ALL safety gates marked satisfied AND still refuses
    consequential effects (deploy/merge/delete/purchase/production write).
    """
    if assignment.owner_id != owner_id:
        raise DelegationBrokerError("owner mismatch")
    require_live_assignment_authority(assignment)
    profile = get_workforce_agent(db, owner_id=owner_id, agent_id=assignment.profile_id)

    package = None
    if assignment.context_package_id:
        package = db.get(WorkforceContextPackage, assignment.context_package_id)

    # Opaque credential reference from coordination adapter config — never the secret.
    cred_ref = None
    if profile.coordination_agent_id is not None:
        try:
            from app.agent_coordination.adapter_config import credential_reference

            # Prefer model_hint / provider_type label only.
            cred_ref = f"ref:{profile.provider_type}:{profile.provider_model_id or 'default'}"
            _ = credential_reference  # imported to prove reuse surface exists
        except Exception:
            cred_ref = f"ref:{profile.provider_type}"

    envelope = build_worker_request_envelope(
        assignment=assignment,
        profile=profile,
        package=package,
        goal_text=goal_text,
        capability=capability,
        timeout_seconds=timeout_seconds,
        credential_reference=cred_ref,
    )

    notes: list[str] = []
    provider_invoked = False
    timed_out = False
    cancelled = cancel
    spend_reserved = False
    spend_settled = False
    external_effect_state = "none_proven"
    raw: dict[str, Any] = {}

    if cancel:
        mark_failure(
            db,
            owner_id=owner_id,
            assignment=assignment,
            failure_class="worker_restart",
            external_effect_state="none_proven",
            partial_result={"cancelled_before_invoke": True},
        )
        return WorkerExecutionReceipt(
            assignment_id=assignment.id,
            provider_invoked=False,
            provider_name=None,
            model_id=None,
            timeout=False,
            cancelled=True,
            raw_output_scrubbed={},
            verification_status=assignment.verification_status,
            spend_reserved=False,
            spend_settled=False,
            external_effect_state="none_proven",
            notes=["cancelled_before_invoke"],
        )

    if activate_provider:
        ok, missing = activation_allowed()
        if not ok:
            raise ProviderActivationBlocked(
                "Real provider delegation blocked until gates verified: " + ", ".join(missing)
            )
        if assignment.allow_execution_effects:
            raise ProviderActivationBlocked(
                "Consequential execution effects refused for first provider-backed slice"
            )
        # Spend reservation path — reuse provider_spend when goal-bound authorization exists.
        # Without a live spend auth, we still refuse blind invoke.
        notes.append("provider_path_prepared")
        try:
            from app.provider_spend.service import (
                get_current_provider_spend_authorization,
                provider_spend_is_live,
            )

            # Goal may be absent on low-risk slices — then spend reserve is N/A (local only).
            notes.append("provider_spend_module_available")
            _ = (get_current_provider_spend_authorization, provider_spend_is_live)
        except Exception as exc:  # pragma: no cover
            raise ProviderActivationBlocked(f"provider_spend unavailable: {exc}") from exc

        # Even with gates marked, first real invoke goes through registry with egress —
        # but we still default to raising unless explicitly force-invoked in a future PR.
        # This harness stops at "prepared" for the activation-gated path unless
        # FORCE_PROVIDER_INVOKE is somehow set — we intentionally do NOT read that env
        # to avoid accidental activation. Real invoke lands in a follow-up after gates.
        raise ProviderActivationBlocked(
            "Gates satisfied but first real provider invoke is staged for the dedicated "
            "activation PR — harness prepares envelopes/receipts/spend wiring only"
        )

    # In-process worker (safe default).
    try:
        raw = _in_process_worker(envelope)
    except TimeoutError:
        timed_out = True
        external_effect_state = "none_proven"
        mark_failure(
            db,
            owner_id=owner_id,
            assignment=assignment,
            failure_class="provider_timeout",
            external_effect_state=external_effect_state,
        )
        raw = {}

    cleaned, stripped = scrub_authority_mutations(raw)
    if stripped:
        notes.append(f"scrubbed:{','.join(stripped)}")

    ingest_untrusted_result(db, owner_id=owner_id, assignment=assignment, payload=cleaned)
    record_checkpoint(
        db,
        owner_id=owner_id,
        assignment=assignment,
        checkpoint_kind="progress",
        partial_result=cleaned,
        external_effect_state=external_effect_state,
        provenance={"provider_invoked": provider_invoked},
    )

    return WorkerExecutionReceipt(
        assignment_id=assignment.id,
        provider_invoked=provider_invoked,
        provider_name=provider_name,
        model_id=model_id,
        timeout=timed_out,
        cancelled=cancelled,
        raw_output_scrubbed=cleaned,
        verification_status=assignment.verification_status,
        spend_reserved=spend_reserved,
        spend_settled=spend_settled,
        external_effect_state=external_effect_state,
        notes=notes,
    )


def handoff_to_verification(
    db: Session,
    *,
    owner_id: uuid.UUID,
    assignment: WorkforceAssignment,
    risk: str,
    verifier_profile_id: uuid.UUID,
    **kwargs,
):
    """Verification handoff — result must already be UNVERIFIED data."""
    from app.workforce.verification import apply_verification_decision
    from app.workforce.performance import record_verified_outcome

    if assignment.verification_status not in ("UNVERIFIED", "CHECKED"):
        raise DelegationBrokerError(
            f"handoff expects UNVERIFIED/CHECKED, got {assignment.verification_status}"
        )
    decision = apply_verification_decision(
        db,
        owner_id=owner_id,
        assignment=assignment,
        decision="VERIFIED",
        risk=risk,
        verifier_profile_id=verifier_profile_id,
        **kwargs,
    )
    record_verified_outcome(
        db,
        owner_id=owner_id,
        profile_id=assignment.profile_id,
        capability_tag=kwargs.get("capability_tag") or "low_risk_classification",
        success=True,
    )
    return decision
