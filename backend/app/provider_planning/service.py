"""Bounded provider assistance outside Safe Planner's execution authority."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select

from app.development_operator import service as operator
from app.intelligence_governance import record_evidence, record_execution, record_idea
from app.mainai_execution.checkpoint import (
    latest_checkpoint_for_step,
    record_checkpoint,
)
from app.models.mainai_execution import MainAIGoal, MainAITask
from app.billing import record_usage as record_usage_event
from app.models.usage import UsageLog
from app.models.work_intelligence import WorkStrategyExecution
from app.providers.base import Message, ProviderError
from app.providers.pricing import estimate_cost
from app.providers.verification import classify_provider_exception
from app.safe_planner.service import (
    CandidateValidationError,
    FounderPlanningRequest,
    PlanCandidate,
    PlanningResult,
    assess_authority,
    assemble_planning_context,
    deterministic_candidate,
    parse_candidate,
    plan_founder_request,
    safe_provider_prompt,
)
from app.work_intelligence import record_specialist_contribution


class ProviderPlanningError(RuntimeError):
    pass


# Local inference runs on the same CPU as the worker. Measured on the 2026-11-27 local stack
# (llama3.1, 8 cores): ~22 prompt tokens/s and ~6.5 generated tokens/s, i.e. one complete Safe
# Planner candidate costs 3-5 minutes. The hosted 60 s default would time out on every single
# call and park the task as WAITING_PROVIDER even though the model was answering correctly --
# measured twice before this bound existed. The local floor is deliberately generous and still
# bounded; it is never applied to a non-local deployment, and it can never lower an explicitly
# configured longer budget.
LOCAL_ONLY_PLANNING_TIMEOUT_SECONDS = 900


@dataclass(frozen=True)
class ProviderPlanningLimits:
    max_candidates: int = 3
    max_payload_bytes: int = 32_000
    max_output_bytes: int = 64_000
    timeout_seconds: int = 60

    @classmethod
    def from_settings(cls) -> "ProviderPlanningLimits":
        """The budget for this deployment -- local inference gets its own latency class."""
        from app.config import get_settings

        settings = get_settings()
        timeout = max(1, int(settings.provider_planning_timeout_seconds))
        if settings.local_only_mode:
            timeout = max(timeout, LOCAL_ONLY_PLANNING_TIMEOUT_SECONDS)
        return cls(timeout_seconds=timeout)


@dataclass(frozen=True)
class ProviderResponse:
    content: str
    provider: str
    model: str
    model_version: str | None = None
    raw_usage: dict = field(default_factory=dict)


class PlanningAdapter(Protocol):
    async def propose(
        self, request_payload: dict, *, timeout_seconds: int, max_output_bytes: int
    ) -> ProviderResponse: ...


@dataclass(frozen=True)
class CandidateComponent:
    component_id: str
    kind: str
    content: str
    disposition: str = "unknown"
    reason: str | None = None


@dataclass(frozen=True)
class ProviderCandidateEnvelope:
    candidate: PlanCandidate | None
    clarification_required: bool = False
    clarification_question: str | None = None
    capability_gaps: tuple[str, ...] = ()
    useful_components: tuple[CandidateComponent, ...] = ()
    confidence: float | None = None


@dataclass(frozen=True)
class CandidateEvaluation:
    provider: str
    model: str
    disposition: str
    reason: str
    useful_component_ids: tuple[str, ...]
    result: PlanningResult | None = None


# The exact envelope and argument contract, in the prompt rather than only in prose, because a
# local 8B model measured on 2026-11-27 did two different things with the old wording: first it
# refused outright ("I can't assist with tasks that require me to write or generate code" -- the
# old prompt never said the answer is a PLAN, so a coding goal read as a coding request), and
# once that framing was fixed it emitted a plausible but unparseable step map. Neither failure
# may be fixed by loosening `parse_provider_envelope()`: the model has to meet the schema the
# executor actually contracts, so the schema and every argument shape are stated literally here.
PROVIDER_SYSTEM_PROMPT = (
    "You are MainAI's planning specialist. You do not answer in prose and you do not write a "
    "code answer in chat: you return exactly one JSON object describing the ordered steps an "
    "executor will perform. This is a planning task, not code generation. "
    "Return exactly this shape, with no extra keys and no markdown fences: "
    '{"candidate": {"interpretation": "...", "requested_outcome": "...", "rationale": "...", '
    '"steps": [{"step_id": "s1", "purpose": "...", "expected_result": "...", '
    '"capability": "create_file", "arguments": {}, "required_risk": "LOCAL_WRITE", '
    '"verification_required": false}], "assumptions": [], "unknowns": []}, '
    '"clarification_required": false, "clarification_question": null, "capability_gaps": [], '
    '"useful_components": [], "confidence": 0.5}. '
    "required_risk is READ_ONLY, LOCAL_WRITE or LOCAL_EXECUTION. Argument contract: "
    'create_file/patch_file -> {"path": "<allowed path>", "content": "<complete file content>"}; '
    'read_file/list_files -> {"path": "<allowed path>"}; '
    'run_focused_test -> {"profile_name": "focused_pytest", "arguments": ["<test file>.py"]}; '
    'stage_scoped_changes -> {"paths": ["<allowed path>", "..."]}; '
    'commit_scoped_changes -> {"message": "<one line, max 200 characters>"}. '
    "arguments.content must be the COMPLETE, runnable file content: never a placeholder, never "
    '"...", "<implementation here>", "pass" or a TODO body -- write the real function and the '
    "real test. A plan that writes a file MUST also stage and commit it (stage_scoped_changes "
    "then commit_scoped_changes); an uncommitted write is discarded by the next task and the "
    "plan is rejected. A step that writes a file MUST set verification_required: true, and the plan MUST end "
    'with a final step {"step_id": "verify", "purpose": "record verification", '
    '"expected_result": "verification recorded", "capability": "verification_evaluate", '
    '"arguments": {}, "required_risk": "READ_ONLY", "verification_required": false} -- without '
    "it the run ends as VERIFICATION_REQUIRED instead of COMPLETE. Add a run_focused_test step "
    "before the final step whenever the test file already exists or this plan creates it. "
    "Use only capabilities from the supplied list and only paths inside "
    "repository_scope.allowed_paths. Never propose shell, merge, deploy, force push, production "
    "mutation or credential access. Keep the plan minimal and ordered. State assumptions and "
    "unknowns; confidence cannot resolve founder ambiguity."
)


def _configured_default_provider() -> tuple[str | None, str | None]:
    """The provider/model this deployment would call first anyway, or (None, None).

    Used only when a caller supplies neither an explicit provider nor model. It never invents
    a provider: it reads the same configured defaults `chat_with_fallback()` selects first, so
    a spend reservation and the invocation it guards can name the identical pair.

    LOCAL_ONLY_MODE is checked here because the explicit-provider path in
    `RegistryPlanningAdapter.propose()` calls `get_provider()` directly and therefore never
    passes through `chat_with_fallback()`'s local-only filtering. A non-local configured
    default is returned as (None, None) in that mode, so the caller keeps failing closed
    instead of quietly reaching an external API.
    """
    from app.config import get_settings
    from app.providers.registry import local_provider_names

    settings = get_settings()
    provider = (settings.default_llm_provider or "").strip() or None
    model = (settings.default_llm_model or "").strip() or None
    if provider is None or model is None:
        return None, None
    if settings.local_only_mode and provider not in local_provider_names():
        return None, None
    return provider, model


class RegistryPlanningAdapter:
    """Thin adapter over the canonical provider registry; no provider registry duplication."""

    def __init__(self, db, *, provider_name=None, model=None):
        self.db = db
        # The Supervisor's ordinary production bindings carry no provider_adapter (see
        # app.development_supervisor.service.WorkBinding), so this adapter is constructed with
        # neither provider nor model. `plan_with_provider()` requires BOTH before it can
        # reserve founder spend -- without this resolution every non-recipe task parked as
        # PROVIDER_SPEND_NOT_AUTHORIZED even with a live grant, i.e. the configured model was
        # structurally unreachable for real development work. Resolving the configured default
        # keeps the reservation and the invocation on exactly the same provider/model.
        if provider_name is None and model is None:
            provider_name, model = _configured_default_provider()
        self.provider_name = provider_name
        self.model = model

    async def propose(self, request_payload, *, timeout_seconds, max_output_bytes):
        from app.providers.registry import chat_with_fallback, get_provider

        serialized = json.dumps(request_payload, sort_keys=True, separators=(",", ":"))
        messages = [
            Message(role="system", content=PROVIDER_SYSTEM_PROMPT),
            Message(role="user", content=serialized),
        ]
        if self.provider_name:
            if not self.model:
                raise ProviderError(
                    "An explicit planning provider requires an explicit model.",
                    category="not_configured",
                )
            provider = get_provider(self.provider_name)
            call_kwargs: dict = {
                "timeout": timeout_seconds,
                "max_tokens": max_output_bytes // 4,
            }
            # A planning answer must be reproducible: on the local runtime the provider's own
            # sampling default (0.8) made this exact prompt alternate between a parseable
            # candidate and prose -- measured on 2026-11-27. Deterministic decoding is requested
            # only for local inference; hosted providers keep their own defaults, so no external
            # behavior changes.
            from app.providers.registry import local_provider_names

            if self.provider_name in local_provider_names():
                call_kwargs["temperature"] = 0
            result = await provider.chat(messages, model=self.model, **call_kwargs)
        else:
            result, _attempted = await chat_with_fallback(
                self.db,
                messages,
                timeout=timeout_seconds,
                max_tokens=max_output_bytes // 4,
            )
        return ProviderResponse(
            content=result.content,
            provider=result.provider,
            model=result.model,
            raw_usage=result.raw_usage or {},
        )


def planning_mode(
    db, request: FounderPlanningRequest
) -> tuple[str, PlanningResult | None]:
    authority = assess_authority(db, request)
    if authority:
        return authority.classification, authority
    if deterministic_candidate(request.original_instruction) is not None:
        return "DETERMINISTIC_PLAN_AVAILABLE", None
    return "PROVIDER_ASSISTANCE_REQUIRED", None


def build_provider_request(
    db,
    *,
    request: FounderPlanningRequest,
    operator_context,
    limits: ProviderPlanningLimits,
):
    if not 1 <= request.max_provider_attempts <= 3:
        raise ProviderPlanningError("provider-attempt bound is invalid")
    if not 1 <= limits.max_candidates <= 5:
        raise ProviderPlanningError("candidate-count bound is invalid")
    for path in operator_context.allowed_paths:
        lowered = {part.lower() for part in path.split("/")}
        if lowered & operator.SENSITIVE_PARTS or any(
            part.startswith(".env") for part in lowered
        ):
            raise ProviderPlanningError(
                "sensitive credential paths cannot enter provider planning scope"
            )
    context_set, context_refs = assemble_planning_context(db, request)
    payload = safe_provider_prompt(request, context_refs)
    payload.update(
        {
            "authority_kind": request.authority_kind,
            "requested_outcome": operator.redact(request.requested_outcome),
            "constraints": operator._redact_value(list(request.constraints)),
            "prohibitions": operator._redact_value(list(request.prohibitions)),
            "repository_scope": {
                "identity": hashlib.sha256(
                    str(operator_context.repository_root.resolve()).encode()
                ).hexdigest(),
                "allowed_paths": list(operator_context.allowed_paths),
                "expected_branch": operator_context.expected_branch,
                "expected_base_sha": operator_context.expected_base_sha,
            },
            "candidate_envelope_schema": {
                "candidate": "Safe Planner PlanCandidate object or null",
                "clarification_required": "boolean",
                "clarification_question": "string or null",
                "capability_gaps": "array of capability names",
                "useful_components": "array of {component_id, kind, content, disposition, reason}",
                "confidence": "number 0..1 or null; never authority",
            },
            "provider_reason": "No registered deterministic recipe safely decomposes this request.",
            "max_candidates": limits.max_candidates,
        }
    )
    redacted = operator._redact_value(payload)
    encoded = json.dumps(redacted, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > limits.max_payload_bytes:
        raise ProviderPlanningError("provider planning payload exceeds byte bound")
    return context_set, redacted


def _strict_strings(value, label, *, maximum=50):
    if (
        not isinstance(value, list)
        or len(value) > maximum
        or not all(isinstance(item, str) for item in value)
    ):
        raise CandidateValidationError(f"{label} must be a bounded string array")
    return tuple(value)


def parse_provider_envelope(content: str, *, max_output_bytes: int):
    if not isinstance(content, str) or len(content.encode()) > max_output_bytes:
        raise CandidateValidationError(
            "provider output is missing or exceeds byte bound"
        )
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as exc:
        raise CandidateValidationError("provider output is not valid JSON") from exc
    allowed = {
        "candidate",
        "clarification_required",
        "clarification_question",
        "capability_gaps",
        "useful_components",
        "confidence",
    }
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise CandidateValidationError("provider envelope has unknown shape")
    clarification = raw.get("clarification_required", False)
    if not isinstance(clarification, bool):
        raise CandidateValidationError("clarification_required must be boolean")
    question = raw.get("clarification_question")
    if question is not None and (not isinstance(question, str) or len(question) > 1000):
        raise CandidateValidationError("clarification question is malformed")
    confidence = raw.get("confidence")
    if confidence is not None and (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0 <= confidence <= 1
    ):
        raise CandidateValidationError("confidence must be between zero and one")
    gaps = _strict_strings(raw.get("capability_gaps", []), "capability_gaps")
    raw_components = raw.get("useful_components", [])
    if not isinstance(raw_components, list) or len(raw_components) > 20:
        raise CandidateValidationError("useful_components exceeds bounds")
    components = []
    component_ids = set()
    for item in raw_components:
        if not isinstance(item, dict) or set(item) - {
            "component_id",
            "kind",
            "content",
            "disposition",
            "reason",
        }:
            raise CandidateValidationError("provider component is malformed")
        try:
            component = CandidateComponent(
                component_id=str(item["component_id"]),
                kind=str(item["kind"]),
                content=str(item["content"]),
                disposition=str(item.get("disposition", "unknown")),
                reason=item.get("reason"),
            )
        except KeyError as exc:
            raise CandidateValidationError(
                "provider component lacks required field"
            ) from exc
        if (
            not component.component_id.strip()
            or component.component_id in component_ids
            or not component.kind.strip()
            or component.disposition
            not in {"accepted", "rejected", "deferred", "unknown"}
            or (
                component.disposition in {"accepted", "rejected"}
                and not component.reason
            )
            or len(component.content) > 2000
        ):
            raise CandidateValidationError(
                "provider component identity/content is invalid"
            )
        component_ids.add(component.component_id)
        components.append(component)
    candidate_payload = raw.get("candidate")
    if candidate_payload is not None:
        if not isinstance(candidate_payload, dict):
            raise CandidateValidationError("candidate must be an object")
        for step in candidate_payload.get("steps", []):
            if not isinstance(step, dict) or not isinstance(
                step.get("arguments", {}), dict
            ):
                raise CandidateValidationError("provider candidate step is malformed")
            for boolean_name in (
                "verification_required",
                "independent_of_provider",
            ):
                if boolean_name in step and not isinstance(step[boolean_name], bool):
                    raise CandidateValidationError(f"{boolean_name} must be boolean")
    candidate = (
        parse_candidate(candidate_payload) if candidate_payload is not None else None
    )
    candidate_data = asdict(candidate) if candidate else None
    if candidate and json.dumps(
        operator._redact_value(candidate_data),
        sort_keys=True,
        separators=(",", ":"),
    ) != json.dumps(candidate_data, sort_keys=True, separators=(",", ":")):
        raise CandidateValidationError(
            "provider candidate contains secret-like material"
        )
    if not clarification and candidate is None and not gaps:
        raise CandidateValidationError(
            "provider envelope contains no actionable result"
        )
    return ProviderCandidateEnvelope(
        candidate=candidate,
        clarification_required=clarification,
        clarification_question=question,
        capability_gaps=gaps,
        useful_components=tuple(components),
        confidence=float(confidence) if confidence is not None else None,
    )


def _scope(db, request):
    goal = db.execute(
        select(MainAIGoal).where(
            MainAIGoal.id == request.goal_id, MainAIGoal.owner_id == request.owner_id
        )
    ).scalar_one()
    task = db.execute(
        select(MainAITask).where(
            MainAITask.id == request.task_id, MainAITask.owner_id == request.owner_id
        )
    ).scalar_one()
    return goal, task


def _checkpoint(db, request, classification, detail):
    goal, task = _scope(db, request)
    return record_checkpoint(
        db,
        task=task,
        goal=goal,
        job_id=request.job_id,
        step="provider_planning",
        data={"classification": classification, "provider_planning": detail},
    )


def _request_hash(request, payload, provider_hint=None, model_hint=None):
    semantic = {
        "task_id": str(request.task_id),
        "source_sha256": hashlib.sha256(
            request.original_instruction.encode()
        ).hexdigest(),
        "payload": payload,
        "provider_hint": provider_hint,
        "model_hint": model_hint,
    }
    return hashlib.sha256(
        json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _evidence_key(namespace, *parts):
    """Keep replay identities inside the canonical 128-character DB contract."""
    digest = hashlib.sha256(
        json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"{namespace}:{digest}"


def _record_participation(
    db,
    *,
    request,
    operator_context,
    response,
    envelope,
    disposition,
    reason,
    request_hash,
    latency_ms,
    record_usage=True,
):
    response_hash = hashlib.sha256(response.content.encode()).hexdigest()
    specialist = record_execution(
        db,
        owner_id=request.owner_id,
        task_id=request.task_id,
        job_id=request.job_id,
        provider=response.provider,
        model=response.model,
        model_version=response.model_version,
        agent_identity="provider_planning_specialist",
        execution_environment="external_provider",
        available_tools=[],
        capabilities=["plan_candidate"],
        role="planner",
        participation_mode="reviewer",
        task_type="development_planning",
        context={"request_hash": request_hash},
        classification_basis="deterministic",
        idempotency_key=_evidence_key("provider-planning", request_hash, response_hash),
    )
    evidence = record_evidence(
        db,
        owner_id=request.owner_id,
        execution_id=specialist.id,
        evidence_kind="provider_plan_candidate",
        payload={
            "response_sha256": response_hash,
            "disposition": disposition,
            "reason": reason,
            "confidence": envelope.confidence,
            "capability_gaps": list(envelope.capability_gaps),
            "latency_ms": latency_ms,
        },
        source_type="mainai_goal",
        source_ref=str(request.goal_id),
        idempotency_key=_evidence_key("provider-candidate", response_hash),
        deterministic=False,
        review_kind="unknown",
    )
    idea_ids = []
    for component in envelope.useful_components:
        safe_content = operator.redact(component.content)[:2000]
        idea_kind = (
            component.kind
            if component.kind
            in {
                "idea",
                "proposal",
                "approach",
                "risk",
                "test_strategy",
                "architectural_pattern",
                "optimization",
                "assumption",
            }
            else "idea"
        )
        idea = record_idea(
            db,
            owner_id=request.owner_id,
            execution_id=specialist.id,
            idea_kind=idea_kind,
            content=safe_content,
            evidence_id=evidence.id,
            disposition=component.disposition,
            disposition_reason=(
                operator.redact(component.reason)[:1000] if component.reason else None
            ),
            classification_basis="inferred",
            confidence=envelope.confidence,
            idempotency_key=_evidence_key(
                "provider-component", response_hash, component.component_id
            ),
        )
        idea_ids.append(str(idea.id))
    binding = db.execute(
        select(WorkStrategyExecution).where(
            WorkStrategyExecution.id == operator_context.strategy_execution_id,
            WorkStrategyExecution.owner_id == request.owner_id,
        )
    ).scalar_one()
    record_specialist_contribution(
        db,
        owner_id=request.owner_id,
        strategy_execution_id=binding.id,
        specialist_execution_id=specialist.id,
        purpose="bounded founder-request planning",
        contribution=(
            "confirmed_finding"
            if disposition.startswith("ACCEPTED")
            else "no_contribution"
        ),
        evidence_available_before={
            "source_sha256": hashlib.sha256(
                request.original_instruction.encode()
            ).hexdigest()
        },
        evidence_id=evidence.id,
        duration_ms=latency_ms,
        idempotency_key=_evidence_key(
            "provider-specialist", request_hash, response_hash
        ),
    )
    if record_usage:
        usage = response.raw_usage or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        # Revenue & Compute Engine: the SINGLE accounting writer (audit row + rollups).
        record_usage_event(
            db,
            user_id=request.owner_id,
            tenant_id=None,
            role="planning",
            provider=response.provider,
            model=response.model,
            prompt_tokens=max(prompt_tokens, 0),
            completion_tokens=max(completion_tokens, 0),
            agent="provider-specialist",
            task=getattr(request, "task_kind", None) or "provider_planning",
        )
    return specialist, evidence, idea_ids


def _candidate_payload(candidate):
    return asdict(candidate) if candidate else None


def _provider_failure_allows_spend_release(exc: BaseException) -> bool:
    """After propose() was entered, release only when failure is proven pre-external-effect.

    Invariant: UNKNOWN EXTERNAL EFFECT != NO EXTERNAL EFFECT.

    - Explicit ``provider_request_may_have_left=False`` (e.g. test fakes that fail before
      any transport) → release allowed.
    - Default / True / unmarked exceptions (timeouts, transport errors, ambiguous crashes)
      → do not release; leave the reservation held for Window-A recovery.
    """
    flag = getattr(exc, "provider_request_may_have_left", None)
    return flag is False


def _allocate_provider_spend_source_ref(db, *, owner_id, base_ref: str) -> str:
    """Pick a spend source_ref that is safe to reserve under append-only usage rows.

    - No prior → reuse base_ref (first attempt).
    - Reserved → reuse base_ref (Window A crash reconcile; do not mint a free retry key).
    - Settled or released → allocate base_ref:aN for a fresh hold (never resurrect a
      released key; never re-invoke under an already-settled key as if it were free).
    """
    from app.models.provider_spend import (
        ProviderSpendUsageEvent,
        ProviderSpendUsageStatus,
    )
    from app.provider_spend import ProviderSpendError

    prior = db.execute(
        select(ProviderSpendUsageEvent).where(
            ProviderSpendUsageEvent.owner_id == owner_id,
            ProviderSpendUsageEvent.source_ref == base_ref,
        )
    ).scalar_one_or_none()
    if prior is None or prior.status == ProviderSpendUsageStatus.reserved.value:
        return base_ref
    for n in range(1, 1000):
        cand = f"{base_ref}:a{n}"
        row = db.execute(
            select(ProviderSpendUsageEvent).where(
                ProviderSpendUsageEvent.owner_id == owner_id,
                ProviderSpendUsageEvent.source_ref == cand,
            )
        ).scalar_one_or_none()
        if row is None or row.status == ProviderSpendUsageStatus.reserved.value:
            return cand
    raise ProviderSpendError(
        "exhausted provider spend source_ref attempt space for this request"
    )


async def plan_with_provider(
    db,
    *,
    request: FounderPlanningRequest,
    operator_context,
    adapter: PlanningAdapter | None = None,
    provider_name: str | None = None,
    model: str | None = None,
    limits: ProviderPlanningLimits | None = None,
):
    limits = limits or ProviderPlanningLimits.from_settings()
    mode, terminal = planning_mode(db, request)
    if terminal:
        return terminal
    if mode == "DETERMINISTIC_PLAN_AVAILABLE":
        return plan_founder_request(
            db, request=request, operator_context=operator_context
        )
    context_set, payload = build_provider_request(
        db,
        request=request,
        operator_context=operator_context,
        limits=limits,
    )
    request_hash = _request_hash(request, payload, provider_name, model)
    prior = latest_checkpoint_for_step(
        db,
        task_id=request.task_id,
        job_id=request.job_id,
        step="provider_planning",
    )
    prior_detail = prior.executor_state.get("provider_planning", {}) if prior else {}
    if (
        prior_detail.get("request_hash") == request_hash
        and prior_detail.get("disposition", "").startswith("ACCEPTED")
        and prior_detail.get("candidate")
    ):
        replay = parse_candidate(prior_detail["candidate"])
        return plan_founder_request(
            db,
            request=request,
            operator_context=operator_context,
            candidate=replay,
        )
    _checkpoint(
        db,
        request,
        "PROVIDER_ASSISTANCE_REQUIRED",
        {
            "request_hash": request_hash,
            "reason": payload["provider_reason"],
            "context_set_id": str(context_set.id),
            "provider_hint": provider_name,
            "model_hint": model,
            "attempt_limit": request.max_provider_attempts,
        },
    )
    selected_adapter = adapter or RegistryPlanningAdapter(
        db, provider_name=provider_name, model=model
    )
    planned_provider = (
        provider_name
        or getattr(selected_adapter, "provider_name", None)
        or getattr(selected_adapter, "default_provider", None)
    )
    planned_model = (
        model
        or getattr(selected_adapter, "model", None)
        or getattr(selected_adapter, "default_model", None)
    )
    # Spend gate: a live boolean alone must never open a billed call. Reserve first.
    # Explicit provider+model required so ceilings can be bound before money is spent.
    from app.provider_spend import (
        ProviderSpendError,
        release_provider_spend_call,
        reserve_provider_spend_call,
        settle_provider_spend_call,
    )
    from app.models.provider_spend import (
        ProviderSpendUsageEvent,
        ProviderSpendUsageStatus,
    )

    spend_base_ref = (
        f"provider-planning:{request.owner_id}:{request.task_id}:{request.job_id}:{request_hash}"
    )
    spend_source_ref = _allocate_provider_spend_source_ref(
        db, owner_id=request.owner_id, base_ref=spend_base_ref
    )
    spend_reserved = False
    if not planned_provider or not planned_model:
        detail = {
            "request_hash": request_hash,
            "reason": (
                "provider-assisted planning requires an explicit provider and model before "
                "invocation so spend can be reserved against founder ceilings"
            ),
            "provider_hint": planned_provider,
            "model_hint": planned_model,
            "context_set_id": str(context_set.id),
            "unrelated_deterministic_work_preserved": True,
        }
        cp = _checkpoint(db, request, "PROVIDER_SPEND_NOT_AUTHORIZED", detail)
        return PlanningResult(
            "PROVIDER_SPEND_NOT_AUTHORIZED",
            detail,
            checkpoint_id=cp.id,
            context_set_id=context_set.id,
        )

    preexisting = db.execute(
        select(ProviderSpendUsageEvent).where(
            ProviderSpendUsageEvent.owner_id == request.owner_id,
            ProviderSpendUsageEvent.source_ref == spend_source_ref,
        )
    ).scalar_one_or_none()
    try:
        reserved_event, reservation_created = reserve_provider_spend_call(
            db,
            owner_id=request.owner_id,
            goal_id=request.goal_id,
            source_ref=spend_source_ref,
            provider=planned_provider,
            model=planned_model,
            task_id=request.task_id,
            job_id=request.job_id,
            evidence={"request_hash": request_hash, "phase": "pre_call"},
        )
        spend_reserved = True
    except ProviderSpendError as exc:
        detail = {
            "request_hash": request_hash,
            "reason": str(exc),
            "provider_hint": planned_provider,
            "model_hint": planned_model,
            "context_set_id": str(context_set.id),
            "unrelated_deterministic_work_preserved": True,
        }
        cp = _checkpoint(db, request, "PROVIDER_SPEND_NOT_AUTHORIZED", detail)
        return PlanningResult(
            "PROVIDER_SPEND_NOT_AUTHORIZED",
            detail,
            checkpoint_id=cp.id,
            context_set_id=context_set.id,
        )

    # Crash window A: a prior ticket left this source_ref reserved (invoke may have
    # completed before settle). Never re-invoke. Consume reserved holds as conservative
    # actuals so the ambiguous call is not labelled unspent, then wait honestly.
    if (
        preexisting is not None
        and preexisting.status == ProviderSpendUsageStatus.reserved.value
        and reserved_event.status == ProviderSpendUsageStatus.reserved.value
    ):
        settle_provider_spend_call(
            db,
            owner_id=request.owner_id,
            source_ref=spend_source_ref,
            prompt_tokens=int(reserved_event.reserved_prompt_tokens or 0),
            completion_tokens=int(reserved_event.reserved_completion_tokens or 0),
            cost_usd=reserved_event.reserved_cost_usd or Decimal("0"),
            evidence={
                "request_hash": request_hash,
                "phase": "crash_before_settle_reconcile",
                "reason": "unresolved reservation; refusing re-invoke",
            },
        )
        detail = {
            "request_hash": request_hash,
            "reason": (
                "prior provider spend reservation was still reserved after a crash window; "
                "refusing to re-invoke the provider for the same source_ref"
            ),
            "failure_category": "unresolved_reservation",
            "provider_hint": planned_provider,
            "model_hint": planned_model,
            "context_set_id": str(context_set.id),
            "spend_source_ref": spend_source_ref,
            "unrelated_deterministic_work_preserved": True,
        }
        cp = _checkpoint(db, request, "WAITING_PROVIDER", detail)
        return PlanningResult(
            "WAITING_PROVIDER",
            detail,
            checkpoint_id=cp.id,
            context_set_id=context_set.id,
        )

    # Concurrent first-reserve race: another caller won the INSERT for this source_ref
    # and the hold is still live. Do not settle their hold (they may still be inside
    # propose), and do not invoke. Settled non-creators should not reach here — allocate
    # mints a fresh :aN after settle/release so a new attempt creates its own row.
    if (
        not reservation_created
        and reserved_event.status == ProviderSpendUsageStatus.reserved.value
    ):
        detail = {
            "request_hash": request_hash,
            "reason": (
                "another caller already holds the provider spend reservation for this "
                "source_ref; refusing a concurrent second invoke"
            ),
            "failure_category": "concurrent_reservation",
            "provider_hint": planned_provider,
            "model_hint": planned_model,
            "context_set_id": str(context_set.id),
            "spend_source_ref": spend_source_ref,
            "unrelated_deterministic_work_preserved": True,
        }
        cp = _checkpoint(db, request, "WAITING_PROVIDER", detail)
        return PlanningResult(
            "WAITING_PROVIDER",
            detail,
            checkpoint_id=cp.id,
            context_set_id=context_set.id,
        )

    # Life Vault / External-AI Egress Control (docs/LIFE_VAULT_EGRESS_CONTROL.md): spend
    # authority governs WHETHER a call may happen; this governs WHAT CONTENT that call may
    # carry -- MODEL_REQUESTED_CONTEXT != AUTHORIZED_EGRESS_CONTEXT is a separate boundary
    # from PROVIDER_SPEND_NOT_AUTHORIZED, checked here immediately before the payload leaves
    # the process, never assumed satisfied just because spend was authorized above.
    from app.egress_policy import EgressDeniedError, enforce_egress_policy

    try:
        payload = enforce_egress_policy(
            db,
            owner_id=request.owner_id,
            provider=planned_provider,
            model=planned_model,
            purpose="development_planning",
            requested_by="provider_planning.plan_with_provider",
            payload=payload,
            task_id=request.task_id,
            goal_id=request.goal_id,
            job_id=request.job_id,
        )
    except EgressDeniedError as exc:
        if spend_reserved:
            release_provider_spend_call(
                db,
                owner_id=request.owner_id,
                source_ref=spend_source_ref,
                evidence={"request_hash": request_hash, "phase": "egress_denied"},
            )
        detail = {
            "request_hash": request_hash,
            "reason": exc.reason,
            "provider_hint": planned_provider,
            "model_hint": planned_model,
            "context_set_id": str(context_set.id),
            "unrelated_deterministic_work_preserved": True,
        }
        cp = _checkpoint(db, request, "EGRESS_DENIED", detail)
        return PlanningResult(
            "EGRESS_DENIED",
            detail,
            checkpoint_id=cp.id,
            context_set_id=context_set.id,
        )

    started = time.monotonic()
    try:
        response = await asyncio.wait_for(
            selected_adapter.propose(
                payload,
                timeout_seconds=limits.timeout_seconds,
                max_output_bytes=limits.max_output_bytes,
            ),
            timeout=limits.timeout_seconds + 1,
        )
    except Exception as exc:  # noqa: BLE001 - provider boundary must checkpoint every failure
        # Window B: once propose() is entered, release only if failure is proven
        # pre-external-effect. Ambiguous post-boundary failures leave the reservation
        # held so retry takes the Window A refuse-re-invoke path.
        if spend_reserved and _provider_failure_allows_spend_release(exc):
            release_provider_spend_call(
                db,
                owner_id=request.owner_id,
                source_ref=spend_source_ref,
                evidence={"request_hash": request_hash, "phase": "provider_failure"},
            )
        safe = classify_provider_exception(exc)
        detail = {
            "request_hash": request_hash,
            "reason": "provider-dependent planning call did not complete",
            "failure_category": safe.result.value,
            "failure_message": safe.message,
            "provider_hint": provider_name,
            "model_hint": model,
            "context_set_id": str(context_set.id),
            "spend_source_ref": spend_source_ref,
            "spend_released": bool(
                spend_reserved and _provider_failure_allows_spend_release(exc)
            ),
            "unrelated_deterministic_work_preserved": True,
        }
        cp = _checkpoint(db, request, "WAITING_PROVIDER", detail)
        return PlanningResult(
            "WAITING_PROVIDER",
            detail,
            checkpoint_id=cp.id,
            context_set_id=context_set.id,
        )
    usage = response.raw_usage or {}
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    settled_cost = estimate_cost(
        response.provider, response.model, prompt_tokens, completion_tokens
    )
    if settled_cost is None:
        settled_cost = Decimal("0")
    settle_provider_spend_call(
        db,
        owner_id=request.owner_id,
        source_ref=spend_source_ref,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=settled_cost,
        evidence={
            "request_hash": request_hash,
            "phase": "settled",
            "provider": response.provider,
            "model": response.model,
        },
    )
    latency_ms = int((time.monotonic() - started) * 1000)
    try:
        envelope = parse_provider_envelope(
            response.content, max_output_bytes=limits.max_output_bytes
        )
    except CandidateValidationError as exc:
        detail = {
            "request_hash": request_hash,
            "disposition": "PROVIDER_FAILED",
            "reason": str(exc),
            "provider": response.provider,
            "model": response.model,
            "response_sha256": hashlib.sha256(response.content.encode()).hexdigest(),
        }
        cp = _checkpoint(db, request, "PROVIDER_FAILED", detail)
        return PlanningResult(
            "PROVIDER_FAILED",
            detail,
            checkpoint_id=cp.id,
            context_set_id=context_set.id,
        )
    attributed = (
        replace(
            envelope.candidate,
            provider=response.provider,
            model=response.model,
            model_version=response.model_version,
            provider_role="planning_specialist",
        )
        if envelope.candidate
        else None
    )
    envelope = replace(envelope, candidate=attributed)
    if envelope.clarification_required:
        disposition = "NEEDS_CLARIFICATION"
        reason = (
            envelope.clarification_question
            or "provider identified unresolved ambiguity"
        )
        result = None
    elif envelope.capability_gaps and attributed is None:
        disposition = "CAPABILITY_MISSING"
        reason = (
            f"provider identified unavailable capability: {envelope.capability_gaps[0]}"
        )
        result = None
    else:
        try:
            result = plan_founder_request(
                db,
                request=request,
                operator_context=operator_context,
                candidate=attributed,
            )
            disposition = result.classification
            reason = result.explanation.get(
                "reason", "deterministic validation completed"
            )
        except CandidateValidationError as exc:
            text = str(exc)
            unsafe = any(
                marker in text
                for marker in ("forbidden", "raw command", "secret", "remote-write")
            )
            disposition = "REJECTED_UNSAFE" if unsafe else "REJECTED_UNSUPPORTED"
            reason = text
            result = None
    specialist, evidence, idea_ids = _record_participation(
        db,
        request=request,
        operator_context=operator_context,
        response=response,
        envelope=envelope,
        disposition=disposition,
        reason=reason,
        request_hash=request_hash,
        latency_ms=latency_ms,
    )
    detail = {
        "request_hash": request_hash,
        "disposition": disposition,
        "reason": reason,
        "provider": response.provider,
        "model": response.model,
        "model_version": response.model_version,
        "confidence": envelope.confidence,
        "candidate": _candidate_payload(attributed),
        "capability_gaps": list(envelope.capability_gaps),
        "useful_component_idea_ids": idea_ids,
        "specialist_execution_id": str(specialist.id),
        "evidence_id": str(evidence.id),
        "latency_ms": latency_ms,
        "context_set_id": str(context_set.id),
    }
    cp = _checkpoint(db, request, disposition, detail)
    if result is not None:
        return replace(
            result,
            explanation={**result.explanation, "provider_planning": detail},
        )
    return PlanningResult(
        disposition, detail, checkpoint_id=cp.id, context_set_id=context_set.id
    )


def evaluate_supplied_candidates(
    db,
    *,
    request,
    operator_context,
    candidates: list[tuple[ProviderResponse, ProviderCandidateEnvelope]],
    limits=None,
):
    """Evaluate explicitly supplied candidates independently; never auto-select a winner."""
    limits = limits or ProviderPlanningLimits()
    if not 1 <= len(candidates) <= limits.max_candidates:
        raise ProviderPlanningError("explicit candidate count exceeds bounds")
    evaluations = []
    for response, envelope in candidates:
        attributed = (
            replace(
                envelope.candidate,
                provider=response.provider,
                model=response.model,
                model_version=response.model_version,
                provider_role="planning_specialist",
            )
            if envelope.candidate
            else None
        )
        try:
            result = plan_founder_request(
                db,
                request=request,
                operator_context=operator_context,
                candidate=attributed,
            )
            disposition, reason = (
                result.classification,
                "deterministic validation completed",
            )
        except CandidateValidationError as exc:
            result = None
            disposition, reason = "REJECTED_UNSAFE", str(exc)
        attributed_envelope = replace(envelope, candidate=attributed)
        request_hash = hashlib.sha256(
            f"explicit:{request.task_id}:{response.provider}:{response.model}".encode()
        ).hexdigest()
        _specialist, _evidence, idea_ids = _record_participation(
            db,
            request=request,
            operator_context=operator_context,
            response=response,
            envelope=attributed_envelope,
            disposition=disposition,
            reason=reason,
            request_hash=request_hash,
            latency_ms=0,
            record_usage=False,
        )
        evaluations.append(
            CandidateEvaluation(
                provider=response.provider,
                model=response.model,
                disposition=disposition,
                reason=reason,
                useful_component_ids=tuple(idea_ids),
                result=result,
            )
        )
    return evaluations
