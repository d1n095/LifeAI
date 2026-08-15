"""Bounded provider assistance outside Safe Planner's execution authority."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Protocol

from sqlalchemy import select

from app.development_operator import service as operator
from app.intelligence_governance import record_evidence, record_execution, record_idea
from app.mainai_execution.checkpoint import (
    latest_checkpoint_for_step,
    record_checkpoint,
)
from app.models.mainai_execution import MainAIGoal, MainAITask
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


@dataclass(frozen=True)
class ProviderPlanningLimits:
    max_candidates: int = 3
    max_payload_bytes: int = 32_000
    max_output_bytes: int = 64_000
    timeout_seconds: int = 60


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


PROVIDER_SYSTEM_PROMPT = (
    "You are a planning specialist, not an execution authority. Return exactly one JSON "
    "object matching the supplied schema. Never propose shell, merge, deploy, force push, "
    "production mutation, credential access, or capabilities outside the supplied list. "
    "State assumptions and unknowns. Confidence cannot resolve founder ambiguity."
)


class RegistryPlanningAdapter:
    """Thin adapter over the canonical provider registry; no provider registry duplication."""

    def __init__(self, db, *, provider_name=None, model=None):
        self.db = db
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
            result = await provider.chat(
                messages,
                model=self.model,
                timeout=timeout_seconds,
                max_tokens=max_output_bytes // 4,
            )
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
        db.add(
            UsageLog(
                user_id=request.owner_id,
                conversation_id=None,
                role="planning",
                provider=response.provider,
                model=response.model,
                prompt_tokens=max(prompt_tokens, 0),
                completion_tokens=max(completion_tokens, 0),
                cost_usd=estimate_cost(
                    response.provider, response.model, prompt_tokens, completion_tokens
                ),
            )
        )
    return specialist, evidence, idea_ids


def _candidate_payload(candidate):
    return asdict(candidate) if candidate else None


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
    limits = limits or ProviderPlanningLimits()
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
        safe = classify_provider_exception(exc)
        detail = {
            "request_hash": request_hash,
            "reason": "provider-dependent planning call did not complete",
            "failure_category": safe.result.value,
            "failure_message": safe.message,
            "provider_hint": provider_name,
            "model_hint": model,
            "context_set_id": str(context_set.id),
            "unrelated_deterministic_work_preserved": True,
        }
        cp = _checkpoint(db, request, "WAITING_PROVIDER", detail)
        return PlanningResult(
            "WAITING_PROVIDER",
            detail,
            checkpoint_id=cp.id,
            context_set_id=context_set.id,
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
