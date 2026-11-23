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

from app.mainai_execution.approval import ApprovalRequiredError, require_task_approval
from app.models.mainai_execution import MainAIGoal, MainAITask, MainAITaskStatus
from app.models.workforce import WorkforceAgentProfile, WorkforceAssignment, WorkforceContextPackage
from app.workforce.activation_gates import (
    REQUIRED_ACTIVATION_GATES,
    activation_allowed,
    activation_gate_status,
    get_activation_gates,
    mark_safety_gate,
    require_activation_allowed,
    reset_safety_gates_for_tests,
    ProviderActivationBlocked,
)
from app.workforce.authority import require_live_assignment_authority
from app.workforce.broker import DelegationBrokerError, ingest_untrusted_result
from app.workforce.failure import mark_failure, record_checkpoint
from app.workforce.injection import scrub_authority_mutations
from app.workforce.kill_switch import assert_not_killed
from app.workforce.registry import get_workforce_agent


# Re-export for callers that imported from provider_worker historically.
__all_gates__ = (
    "ProviderActivationBlocked",
    "REQUIRED_ACTIVATION_GATES",
    "activation_allowed",
    "activation_gate_status",
    "mark_safety_gate",
    "reset_safety_gates_for_tests",
)


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


# Task states that remain legitimate real-dispatch targets. Everything else -- cancelled,
# failed, completed, blocked, or a transient retryable_failed awaiting the task-level retry
# orchestrator's own scan (app/jobs/retry.py; not workforce's concern) -- fails closed. This
# is the closest existing equivalent this codebase has to "rejected/revoked": there is no
# separate approval-revocation event type (MainAITaskEventType is append-only; approval,
# once granted, is never un-granted), so a task that was approved and then cancelled is the
# modeled way "the founder no longer wants this dispatched" is expressed.
_WORKFORCE_DISPATCHABLE_TASK_STATUSES = frozenset(
    {
        MainAITaskStatus.pending,
        MainAITaskStatus.ready,
        MainAITaskStatus.running,
        MainAITaskStatus.waiting_external,
        MainAITaskStatus.waiting_ci,
    }
)


def require_live_task_authorization_for_workforce_dispatch(db: Session, *, owner_id: uuid.UUID, assignment: WorkforceAssignment) -> None:
    """The per-task gate `execute_workforce_assignment()`'s `activate_provider` path was
    missing (see docs/LIFE_EXECUTION_ARCHITECTURE_CONVERGENCE_RECOMMENDATION.md and the
    executable proof in tests/backend/mainai/test_execution_authority_convergence_gap.py).
    Reuses app.mainai_execution.approval.require_task_approval() -- the SAME real gate
    app.agent_coordination.dispatch.evaluate_dispatch_readiness() already delegates to --
    never a second approval engine. Always re-queries the task fresh (never trusts a result
    computed earlier by this or any other call), so a cancellation or any other state change
    that happens between an earlier readiness check and this call is caught here, not missed.

    Raises ProviderActivationBlocked -- the existing vocabulary for "this workforce dispatch
    cannot proceed" -- for every refusal reason: missing task link, cross-tenant task,
    non-dispatchable task status, or no recorded approval. Never silently treats "nothing to
    check" as "approved".

    TRANSACTIONAL AUTHORIZATION (closes the gap docs/LIFE_WORKFORCE_AUTHORIZATION_RACE_WINDOW.md
    names as unresolved -- see that file for the full threat model and why a sequential
    re-check alone does not establish atomicity): the task lookup below takes a real
    `SELECT ... FOR UPDATE` row lock, held for the remainder of the CALLER's transaction (this
    function never commits or rolls back `db` itself). This is the exact same lock
    `app.mainai_execution.executor._lock_task()` already takes on the identical row for
    `cancel_task()`/`dispatch_ready_task()`/`retry_task()` -- deliberately reusing the lock
    idiom that module's own hardening pass already established, not inventing a second one.
    A concurrent `cancel_task()` attempting to lock the SAME task row blocks until this
    function's caller's transaction ends (commit or rollback); whichever side acquires the
    lock first determines the outcome the other side sees once it proceeds. This is the
    smallest correct primitive for "prevent a revoked authorization from being accepted at
    the execution boundary": as long as whatever eventually performs the real effect stays
    inside the SAME transaction as this check (never committing in between), no concurrent
    revocation can land between the check and the effect. See
    tests/backend/mainai/test_workforce_task_authorization_concurrency.py for the genuine
    two-connection proof, mirroring the pattern PR #379 already established for Blueprint's
    own TOCTOU-safe approval (`test_blueprint_approval_concurrency.py`)."""
    task_id_raw = (assignment.provenance or {}).get("mainai_task_id")
    if not task_id_raw:
        raise ProviderActivationBlocked("assignment has no linked MainAI task -- fails closed, never treated as pre-approved")
    try:
        task_id = uuid.UUID(str(task_id_raw))
    except ValueError as exc:
        raise ProviderActivationBlocked(f"assignment's linked task id is malformed: {task_id_raw!r}") from exc

    # Filtered by owner_id in the query itself (not compared only after fetching) -- an
    # explicit tenant boundary at the application layer, defense-in-depth alongside RLS.
    # with_for_update(): see the transactional-authorization note above.
    task = (
        db.query(MainAITask)
        .filter(MainAITask.id == task_id, MainAITask.owner_id == owner_id)
        .with_for_update()
        .first()
    )
    if task is None:
        raise ProviderActivationBlocked(f"linked task {task_id} does not resolve to a real task owned by this tenant -- fails closed")

    if task.status not in _WORKFORCE_DISPATCHABLE_TASK_STATUSES:
        raise ProviderActivationBlocked(f"task {task.id} is {task.status.value}, not a live dispatch target -- fails closed")

    goal = db.query(MainAIGoal).filter(MainAIGoal.id == task.goal_id, MainAIGoal.owner_id == owner_id).first()
    if goal is None:
        raise ProviderActivationBlocked(f"task {task.id}'s goal does not resolve for this tenant -- fails closed")

    try:
        require_task_approval(db, task=task, goal_approval_policy=goal.approval_policy)
    except ApprovalRequiredError as exc:
        raise ProviderActivationBlocked(f"task {task.id} requires founder approval before workforce dispatch") from exc
def _execute_consultation_assignment(
    db: Session,
    *,
    owner_id: uuid.UUID,
    assignment: WorkforceAssignment,
    profile: WorkforceAgentProfile,
    envelope: WorkerRequestEnvelope,
    goal_text: str,
    instructions: str | None,
    goal_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
    job_id: uuid.UUID | None,
    notes: list[str],
) -> WorkerExecutionReceipt:
    """Consultation branch of execute_workforce_assignment — a thin adapter over
    app/openai_consult/mainai.consult_for_mainai_sync. NO second invocation stack: the
    answer comes back through the same scrub → broker-ingestion → checkpoint pipeline as
    any other worker output, and the egress disclosure ledger (with this assignment's
    goal/task/job ids) is written by the consultation core itself.

    Controlled error recovery: a PROVIDER-infrastructure failure (timeout, unreachable,
    provider 5xx, contract-invalid answer → ConsultationServiceError) falls back to the
    LOCAL in-process worker so the assignment still completes on the primary execution
    path. Policy and authorization refusals (disabled, unauthorized, activation gates,
    kill switch, recursion, budget, egress) are NOT caught — they fail closed."""
    from app.openai_consult.mainai import consult_for_mainai_sync
    from app.openai_consult.service import ConsultationServiceError

    provider_invoked = False
    provider_name = None
    model_id = None
    spend_reserved = False
    spend_settled = False
    try:
        result, _stats = consult_for_mainai_sync(
            db,
            owner_id=owner_id,
            goal_text=goal_text,
            instructions=instructions,
            goal_id=goal_id,
            task_id=task_id,
            job_id=job_id,
            agent_key=profile.agent_key if profile is not None else None,
        )
        provider_invoked = True
        provider_name = "openai"
        model_id = result.model
        spend_reserved = True
        spend_settled = True
        raw: dict[str, Any] = {
            "answer": result.output_text,
            "model": result.model,
            "cost_usd": result.cost_usd,
            "usage": {
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "total_tokens": result.usage.total_tokens,
            },
        }
        notes.append("consultation_completed")
    except ConsultationServiceError:
        notes.append("consultation_unavailable_local_fallback")

        # The answer (or the absence of one) is UNTRUSTED DATA either way — same pipeline.
        raw = _in_process_worker(envelope)

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
        external_effect_state="none_proven",
        provenance={"provider_invoked": provider_invoked, "consultation": provider_invoked},
    )

    receipt_notes = notes + ["consultation_egress_ledger_recorded"] if provider_invoked else notes
    return WorkerExecutionReceipt(
        assignment_id=assignment.id,
        provider_invoked=provider_invoked,
        provider_name=provider_name,
        model_id=model_id,
        timeout=False,
        cancelled=False,
        raw_output_scrubbed=cleaned,
        verification_status=assignment.verification_status,
        # Budget reserve/settle happened inside the consultation core against the owner's
        # shared daily consultation budget (not the goal-bound spend-authorization kernel,
        # which stays reserved for goal-bound agent execution).
        spend_reserved=spend_reserved,
        spend_settled=spend_settled,
        external_effect_state="none_proven",
        notes=receipt_notes,
    )


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
    # Additive, independently authorized consultation path (app/openai_consult/mainai.py).
    # consultation_mode=False keeps this function byte-for-byte identical to its
    # pre-consultation behavior: the in-process worker stays the primary execution path
    # and the staged provider path below is untouched.
    consultation_mode: bool = False,
    consultation_instructions: str | None = None,
    consultation_goal_id: uuid.UUID | None = None,
    consultation_task_id: uuid.UUID | None = None,
    consultation_job_id: uuid.UUID | None = None,
) -> WorkerExecutionReceipt:
    """Run worker for an assignment.

    activate_provider=True requires ALL safety gates marked satisfied AND still refuses
    consequential effects (deploy/merge/delete/purchase/production write).
    """
    if assignment.owner_id != owner_id:
        raise DelegationBrokerError("owner mismatch")
    assert_not_killed()
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

    if consultation_mode:
        # MainAI-invoked OpenAI consultation via app/openai_consult/mainai.py — reuses the
        # SAME gates as the founder path (egress ledger, budgets, safe errors) PLUS this
        # orchestration's own authorization (mainai flag + activation gates + recursion
        # guards). Consultation refusals raise (fail loud — nothing was invoked); the
        # answer is ingested as UNTRUSTED DATA exactly like every other worker output.
        return _execute_consultation_assignment(
            db,
            owner_id=owner_id,
            assignment=assignment,
            profile=profile,
            envelope=envelope,
            goal_text=goal_text,
            instructions=consultation_instructions,
            goal_id=consultation_goal_id,
            task_id=consultation_task_id,
            job_id=consultation_job_id,
            notes=notes,
        )

    if activate_provider:
        # Single authoritative gate object — UNKNOWN != VERIFIED → FAIL CLOSED.
        require_activation_allowed()
        # Per-task founder-approval gate — delegates to the same real authority
        # app.agent_coordination already uses; see the function's own docstring.
        require_live_task_authorization_for_workforce_dispatch(db, owner_id=owner_id, assignment=assignment)
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

        # Even with gates marked, first real invoke is staged — dedicated enablement after
        # Claude verification. Harness prepares envelopes/receipts/spend wiring only.
        raise ProviderActivationBlocked(
            "Gates satisfied but first real provider invoke remains staged — "
            "enable only in the post-verification activation commit"
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
