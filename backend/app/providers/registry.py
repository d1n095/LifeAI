from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

import logging
import uuid

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.provider_config import ProviderConfig
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import ChatResult, LLMProvider, Message, ProviderError
from app.providers.catalog import (
    ModelSelectionError,
    ModelSelectionPolicy,
    ModelSpec,
    evaluate_model,
    get_model_spec,
    model_specs,
)
from app.providers.deepseek_provider import DeepSeekProvider
from app.providers.gemini_provider import GeminiProvider
from app.providers.ollama_provider import OllamaProvider
from app.providers.openai_provider import OpenAIProvider
from app.providers.openrouter_provider import OpenRouterProvider
from app.providers.zai_provider import ZAIProvider

logger = logging.getLogger(__name__)

_PROVIDERS: dict[str, type[LLMProvider]] = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "deepseek": DeepSeekProvider,
    "openrouter": OpenRouterProvider,
    "ollama": OllamaProvider,
    "zai": ZAIProvider,
}

# Founder directive (Task #60, requirement 8): the only providers this codebase can genuinely
# call without content leaving this deployment's own network. Every other entry in _PROVIDERS
# is a real external API call. Enforced in chat_with_fallback() below via
# `settings.local_only_mode` -- never changes behavior for a non-local-only deployment.
_LOCAL_PROVIDERS: frozenset[str] = frozenset({"ollama"})

# Reasonable default chat model per provider, used only when falling back to a provider that
# has no explicit admin-configured model (the primary provider's own model comes from
# ProviderConfig / settings, never from here).
DEFAULT_CHAT_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-5",
    "gemini": "gemini-2.5-flash",
    "deepseek": "deepseek-chat",
    "openrouter": "openrouter/auto",
    "ollama": "llama3.1",
    "zai": "glm-5.3",
}


# Evaluation is task-local: it never mutates production registration/configuration.
@dataclass
class _EvaluationLease:
    provider_class: type
    models: dict[str, str]
    active: bool = True
    observation: dict = field(default_factory=dict)


_evaluation_context: ContextVar[_EvaluationLease | None] = ContextVar("provider_evaluation", default=None)


def _current_evaluation():
    lease = _evaluation_context.get()
    if lease is not None and not lease.active:
        raise ProviderError("Evaluation context has expired.", category="unsupported",
                            provider_request_may_have_left=False)
    return lease


@contextmanager
def evaluation_provider_scope(provider_class, models: dict[str, str], *, authorized: bool = False):
    if not authorized or get_settings().environment not in {"test", "development"}:
        raise ProviderError("Model evaluation is not authorized in this environment.",
                            category="unsupported", provider_request_may_have_left=False)
    if provider_class.name != "zai_eval" or set(models) != {"chat", "embedding"}:
        raise ProviderError("Invalid evaluation identity.", category="unsupported",
                            provider_request_may_have_left=False)
    lease = _EvaluationLease(provider_class, dict(models))
    token = _evaluation_context.set(lease)
    try:
        yield
    finally:
        lease.active = False  # Copied task contexts cannot outlive evaluation authorization.
        lease.observation.clear()
        _evaluation_context.reset(token)


def provider_names() -> list[str]:
    return list(_PROVIDERS.keys())


def available_providers() -> list[LLMProvider]:
    return [cls() for cls in _PROVIDERS.values()]


def get_provider(name: str) -> LLMProvider:
    context = _current_evaluation()
    if name == "zai_eval" and context is not None:
        return context.provider_class()
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise ProviderError(f"Okänd leverantör: {name}")
    return cls()


def _model_is_freshly_verified(db: Session, spec: ModelSpec, role: str) -> bool:
    """Verification is exact-model and TTL-bound; another model's PASS is not evidence."""

    from datetime import datetime, timedelta

    from app.models.provider_verification import VerificationResult

    from app.providers.verification import latest_check

    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=get_settings().provider_verification_cache_seconds)
    check = latest_check(db, spec.provider, role, model=spec.model_id)
    return bool(check is not None and check.result == VerificationResult.ok
                and cutoff <= check.checked_at <= now)



def _require_policy_candidate(
    db: Session,
    spec: ModelSpec,
    policy: ModelSelectionPolicy,
) -> tuple[LLMProvider, str, ModelSpec]:
    eligible, reason = evaluate_model(spec, policy)
    if not eligible:
        raise ModelSelectionError(f"{spec.provider}/{spec.model_id}: {reason}")
    provider = get_provider(spec.provider)
    if not provider.is_configured():
        raise ModelSelectionError(f"{spec.provider}/{spec.model_id}: provider is not configured")
    if policy.require_verified and not _model_is_freshly_verified(db, spec, policy.role):
        raise ModelSelectionError(
            f"{spec.provider}/{spec.model_id}: exact model has no fresh successful verification"
        )
    return provider, spec.model_id, spec


def select_model(
    db: Session,
    policy: ModelSelectionPolicy,
    *,
    requested_provider: str | None = None,
    requested_model: str | None = None,
) -> tuple[LLMProvider, str, ModelSpec]:
    """Deterministic manual or automatic model selection over the shared provider registry.

    This function does not authorize provider spend or egress. Callers must still pass the
    selected provider call through those existing authority boundaries.
    """

    if (requested_provider is None) != (requested_model is None):
        raise ModelSelectionError("requested_provider and requested_model must be supplied together")
    if requested_provider is not None and requested_model is not None:
        return _require_policy_candidate(
            db, get_model_spec(requested_provider, requested_model), policy
        )

    eligible: list[tuple[int, ModelSpec]] = []
    for index, spec in enumerate(model_specs()):
        allowed, _ = evaluate_model(spec, policy)
        if not allowed:
            continue
        provider = get_provider(spec.provider)
        if not provider.is_configured():
            continue
        if policy.require_verified and not _model_is_freshly_verified(db, spec, policy.role):
            continue
        eligible.append((index, spec))

    if not eligible:
        raise ModelSelectionError("no configured model satisfies the verified policy")

    def _sort_key(item: tuple[int, ModelSpec]):
        index, spec = item
        local_rank = 0 if policy.prefer_local and not spec.external else 1
        input_price = spec.pricing.input_per_million_usd
        price_rank = input_price if input_price is not None else float("inf")
        return local_rank, price_rank, index

    selected = sorted(eligible, key=_sort_key)[0][1]
    return get_provider(selected.provider), selected.model_id, selected


def resolve_active(db: Session, role: str) -> tuple[LLMProvider, str]:
    """Resolve the active provider + model for a given role ("chat" | "embedding").

    Order of precedence: DB config (set via admin panel) -> settings defaults.
    This is the single place the rest of the platform depends on — swapping a
    provider never requires touching routers, RAG code or the frontend.
    """
    context = _current_evaluation()
    if context is not None and role in context.models:
        return context.provider_class(), context.models[role]
    settings = get_settings()
    config = db.query(ProviderConfig).filter_by(role=role, is_active=True).first()
    if config:
        return get_provider(config.provider), config.model

    if role == "embedding":
        return get_provider(settings.default_embedding_provider), settings.default_embedding_model
    return get_provider(settings.default_llm_provider), settings.default_llm_model


async def embed_with_policy(
    db: Session,
    provider: LLMProvider,
    texts: list[str],
    *,
    model: str,
    owner_id: uuid.UUID | None = None,
    purpose: str | None = None,
    requested_by: str | None = None,
    task_id: uuid.UUID | None = None,
    goal_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    effect_guard: bool = False,
) -> list[list[float]]:
    """`provider.embed(texts, model=model)`, gated the same way `chat_with_fallback()` gates
    `.chat()` (Life Vault / External-AI Egress Control, V2 — see
    docs/LIFE_VAULT_EGRESS_CONTROL.md): when `owner_id` is passed, `texts` is routed through
    `enforce_egress_policy()` first -- default-deny, hard-deny on NEVER_EGRESS-marked content
    (the whole batch, never a partial embed with the poisoned chunk silently dropped), one
    disclosure-ledger row per call. `owner_id` defaults to None (gate skipped, unchanged
    legacy behavior) for the same incremental-adoption reason as `chat_with_fallback()`.

    Takes an already-resolved `provider`/`model` rather than resolving them itself: every
    existing call site already calls `resolve_active(db, role="embedding")` earlier in its own
    flow (for provider verification / pre-flight checks that must happen before this), and
    resolving a second time here would be redundant, not just extra work -- a provider config
    change landing between the two resolutions could then embed with a provider different from
    the one that was actually verified.
    """
    # The vector-only adapter contract cannot reconcile paid usage or an uncertain
    # embedding outcome. Do not repeat an unmetered external effect on chat retry.
    # Eval adapters require their own active lease and are never production providers.
    if effect_guard and provider.name != "ollama":
        lease = _current_evaluation()
        if lease is None or provider.name != "zai_eval" or not isinstance(provider, lease.provider_class):
            raise ProviderError("External query embedding requires a metered effect contract.",
                                category="unsupported", provider_request_may_have_left=False)
    outbound_texts = texts
    if owner_id is not None:
        from app.egress_policy import enforce_egress_policy

        outbound_texts = enforce_egress_policy(
            db,
            owner_id=owner_id,
            provider=provider.name,
            model=model,
            purpose=purpose or "embedding",
            requested_by=requested_by or "providers.registry.embed_with_policy",
            payload=texts,
            task_id=task_id,
            goal_id=goal_id,
            job_id=job_id,
        )
    return await provider.embed(outbound_texts, model=model)


def resolve_chat_chain(
    db: Session,
    selection_policy: ModelSelectionPolicy | None = None,
) -> list[tuple[LLMProvider, str]]:
    """Primary chat provider first (DB config / settings default, as usual), then the
    remaining providers in CHAT_FALLBACK_ORDER that are actually configured (have an API
    key) and aren't already the primary. Embedding has no fallback in MainAI 0.1 — an
    inconsistent embedding provider mid-conversation would silently corrupt retrieval
    quality, so a failed embedding call should surface as an error, not swap providers.
    """
    settings = get_settings()
    primary_provider, primary_model = resolve_active(db, role="chat")
    if _current_evaluation() is not None:
        # Evaluation failures can never fall through to a credentialed production API.
        if selection_policy is not None:
            raise ModelSelectionError("evaluation cannot satisfy production selection policy")
        return [(primary_provider, primary_model)]
    chain = []
    if selection_policy is not None:
        if selection_policy.role != "chat":
            raise ModelSelectionError("chat fallback requires a chat selection policy")
        try:
            primary_provider, primary_model, _ = select_model(
                db, selection_policy, requested_provider=primary_provider.name,
                requested_model=primary_model,
            )
            chain.append((primary_provider, primary_model))
        except ModelSelectionError:
            # The preference cannot waive policy. Only eligible configured fallbacks
            # below may replace an unavailable/incompatible primary.
            pass
    elif primary_provider.is_configured():
        chain.append((primary_provider, primary_model))
    seen_names = {primary_provider.name}

    for name in [p.strip() for p in settings.chat_fallback_order.split(",") if p.strip()]:
        if name in seen_names or name not in _PROVIDERS:
            continue
        provider = get_provider(name)
        if not provider.is_configured():
            continue
        model = DEFAULT_CHAT_MODELS.get(name, primary_model)
        if selection_policy is not None:
            try:
                provider, model, _ = select_model(
                    db,
                    selection_policy,
                    requested_provider=name,
                    requested_model=model,
                )
            except ModelSelectionError:
                continue
        chain.append((provider, model))
        seen_names.add(name)

    if not chain:
        if selection_policy is not None:
            raise ModelSelectionError("no configured model satisfies the verified chat policy")
        chain.append((primary_provider, primary_model))
    return chain


def _verify_lease_assertion(
    db: Session,
    *,
    job_id: uuid.UUID | None,
    lease_worker_id: str | None,
    lease_generation: int | None,
) -> None:
    """Shared by `chat_with_fallback()` and `prepare_single_provider_chat()` — see
    `chat_with_fallback()`'s own docstring (requirement 2) for the full rationale. Raises
    `EgressDeniedError` on an incomplete or dead lease assertion; returns None otherwise
    (including the "no assertion at all" case, which is unchanged legacy behavior)."""
    from app.egress_policy.service import EgressDeniedError
    from app.jobs.mainai_job_lease import verify_active_lease

    lease_assertion_fields = [lease_worker_id is not None, lease_generation is not None]
    if any(lease_assertion_fields) and not (all(lease_assertion_fields) and job_id is not None):
        raise EgressDeniedError(
            "incomplete worker-lease assertion: job_id/lease_worker_id/lease_generation must be given "
            "all together or not at all -- refusing to guess which ones were meant."
        )
    if all(lease_assertion_fields):
        if not verify_active_lease(db, job_id=job_id, worker_id=lease_worker_id, lease_generation=lease_generation):
            raise EgressDeniedError(
                f"worker lease for job {job_id} (worker={lease_worker_id!r}, generation={lease_generation}) "
                "is no longer active -- reclaimed, expired, or never existed. Refusing egress under a dead "
                "job's claimed identity."
            )


def _filter_local_only(chain: list[tuple[LLMProvider, str]]) -> list[tuple[LLMProvider, str]]:
    """Shared LOCAL_ONLY_MODE enforcement (Task #60, requirement 8) — see
    `chat_with_fallback()`'s own docstring for the full rationale. A no-op unless the setting
    is on; raises `ProviderError` if it filters the chain down to nothing."""
    if not get_settings().local_only_mode:
        return chain
    filtered = [(provider, model) for provider, model in chain if provider.name in _LOCAL_PROVIDERS]
    if not filtered:
        raise ProviderError(
            "LOCAL_ONLY_MODE is enabled and no local provider is configured/reachable -- refusing to "
            "fall back to an external provider.",
            # Found via the streaming endpoint's own test suite (the first caller to actually
            # persist this category to a real DB row rather than just assert the exception):
            # "local_only_mode_no_local_provider" is 33 characters, one over Message.error_category's
            # String(32) column -- every call through _finalize_failed_reply() would have crashed
            # with a raw DataError instead of the intended clean failed-reply response, on BOTH the
            # streaming and non-streaming chat endpoints, the moment LOCAL_ONLY_MODE was ever
            # actually enabled with no local provider reachable. Shortened to fit; not referenced by
            # exact string anywhere else in the codebase (verified by search) before this fix.
            category="local_only_mode_no_provider",
        )
    return filtered


async def prepare_single_provider_chat(
    db: Session,
    messages: list[Message],
    *,
    owner_id: uuid.UUID,
    purpose: str | None = None,
    requested_by: str | None = None,
    task_id: uuid.UUID | None = None,
    goal_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    lease_worker_id: str | None = None,
    lease_generation: int | None = None,
) -> tuple[LLMProvider, str, list[Message]]:
    """The streaming equivalent of `chat_with_fallback()`'s pre-attempt gate, for exactly ONE
    provider rather than a fallback chain.

    Streaming cannot fall back across providers mid-response: once tokens from provider A have
    already reached the client, silently restarting the same turn against provider B would
    either duplicate visible text or require discarding what the founder already saw — neither
    is an improvement over a single, clearly-failed attempt they can retry. So this resolves
    and gates ONLY the primary candidate (`resolve_chat_chain()`'s first entry, after the same
    LOCAL_ONLY_MODE filter `chat_with_fallback()` applies), through the exact same two central
    checks (worker-lease re-verification, LOCAL_ONLY_MODE), and the exact same egress
    enforcement, BEFORE the caller ever opens a streaming connection to it — `owner_id` is
    required here (unlike `chat_with_fallback()`'s optional legacy-compatible default) because
    every streaming call site is a live, authenticated founder request, never one of the
    pre-existing unauthenticated-context callers that justified that default there.

    Raises `EgressDeniedError`/`ProviderError`/`ModelSelectionError` exactly as
    `chat_with_fallback()` would for the same candidate — callers should handle them
    identically (see `app/routers/chat.py`'s shared `_finalize_failed_reply()`).
    """
    from app.egress_policy.service import enforce_egress_policy

    _verify_lease_assertion(db, job_id=job_id, lease_worker_id=lease_worker_id, lease_generation=lease_generation)

    chain = _filter_local_only(resolve_chat_chain(db))
    provider, model = chain[0]

    payload = [{"role": m.role, "content": m.content} for m in messages]
    redacted = enforce_egress_policy(
        db,
        owner_id=owner_id,
        provider=provider.name,
        model=model,
        purpose=purpose or "chat",
        requested_by=requested_by or "providers.registry.prepare_single_provider_chat",
        payload=payload,
        task_id=task_id,
        goal_id=goal_id,
        job_id=job_id,
    )
    outbound_messages = [Message(role=m["role"], content=m["content"]) for m in redacted]
    if not provider.is_configured():
        raise ProviderError("Provider is not configured.", category="not_configured",
                            provider_request_may_have_left=False)
    return provider, model, outbound_messages


async def chat_with_fallback(
    db: Session,
    messages: list[Message],
    *,
    owner_id: uuid.UUID | None = None,
    purpose: str | None = None,
    requested_by: str | None = None,
    task_id: uuid.UUID | None = None,
    goal_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    selection_policy: ModelSelectionPolicy | None = None,
    effect_guard: bool = False,
    attempt_observer=None,
    lease_worker_id: str | None = None,
    lease_generation: int | None = None,
    **kwargs,
) -> tuple[ChatResult, list[str]]:
    """Try the primary chat provider, then fall back through CHAT_FALLBACK_ORDER on failure.

    Returns the successful result plus the list of provider names that were attempted
    (including the one that succeeded), so callers can log/audit exactly what happened
    instead of it being invisible.

    Life Vault / External-AI Egress Control (docs/LIFE_VAULT_EGRESS_CONTROL.md, V1): when
    `owner_id` is passed, every attempt is routed through `enforce_egress_policy()` first --
    default-deny gate, hard-deny on NEVER_EGRESS-marked content (never partial-send, and never
    treated as "try the next provider" -- the content itself is what's denied, not a specific
    provider being unreachable), otherwise secret-pattern redacted, with exactly one disclosure-
    ledger row per real attempt. `owner_id` is optional and defaults to None (gate skipped,
    unchanged legacy behavior) so existing callers migrate incrementally -- see the threat
    model's V1/V4 blocker list for which callers still need to pass it.

    Founder directive ("RESUME LIFEAI FIRST-CYCLE REMEDIATION" -> "FOUNDER DECISION -- EGRESS
    AUTHORIZATION & FIRST-CYCLE INTEGRATION", Task #60): two ADDITIONAL, independent central
    checks, both evaluated BEFORE any provider is ever attempted (zero provider calls on
    denial), neither bypassable by a caller that simply omits them where they don't apply:

    1. LOCAL_ONLY_MODE (requirement 8): when `settings.local_only_mode` is True, the chain is
       filtered down to local-only providers (`_LOCAL_PROVIDERS`) before anything is attempted.
       An empty resulting chain raises `ProviderError` immediately -- a local provider's own
       failure must surface as an error, NEVER silently escalate to a real external provider.
       No `owner_id`/authorization context can override this; it is evaluated unconditionally,
       even when `owner_id` is None, because this is a deployment-wide network posture, not a
       per-call egress decision the Life Vault gate already owns.
    2. Worker-lease verification (requirement 2): `job_id` already exists as a plain
       attribution field for the disclosure ledger. When a caller ALSO supplies
       `lease_worker_id`/`lease_generation` alongside `job_id`, this function re-verifies
       (`app.jobs.mainai_job_lease.verify_active_lease()`, a read-only check, performed fresh
       against the CURRENT row -- never trusting an in-memory job object that may be stale)
       that the exact (job_id, worker_id, lease_generation) triple is still the job's genuinely
       active lease, right now, immediately before the first provider attempt. A lease that
       expired or was reclaimed between dispatch and this call -- otherwise invisible, since
       the caller's own in-memory `MainAIJob` object would not reflect it -- denies the whole
       call (`EgressDeniedError`, same hard-stop semantics as a Life Vault denial) rather than
       let a dead job's claimed identity still reach a real provider. Supplying only one of
       `lease_worker_id`/`lease_generation` without `job_id` (or vice versa) is an incomplete
       lease assertion and is ALSO treated as a denial -- never silently ignored as "not
       asked for." Omitting all three preserves prior behavior exactly (no lease check at all)
       for call sites that are not job-backed (e.g. a live, synchronously-authenticated request
       with no `mainai_jobs` row behind it at all).
    """
    # Deferred import: app/providers/verification.py imports resolve_active from this module,
    # so importing it at module load time would be a circular import. classify_provider_exception
    # turns an exception into a safe (category, message) pair — used for EVERY log line below,
    # never str(exc)/%s-on-the-exception directly, since an httpx-originated exception embeds
    # the full request URL in its default message, and a provider can put its API key directly
    # in that URL (historically app/providers/gemini_provider.py's query-string auth — see its
    # own module docstring for why it no longer does that; this logging fix stands regardless
    # of which provider's URL shape changes in the future). Real incident, 2026-07-26: this
    # exact line logged a raw httpx.HTTPStatusError straight to the Docker log, and it happened
    # to contain a live Gemini key.
    from app.providers.verification import classify_provider_exception

    # Deferred for the same reason: keep app.egress_policy's own import graph (which reaches
    # into app.development_operator.service) from ever becoming a load-time dependency of this
    # already-central module.
    # Bind the transport boundary to the canonical implementation module, not the package
    # re-export, which can become a stale alias under development/test module reloads.
    from app.egress_policy.service import EgressDeniedError, enforce_egress_policy

    # Task #60 worker-lease re-verification (requirement 2) -- evaluated before anything else,
    # including selection_policy/LOCAL_ONLY_MODE below, since a dead job's claimed identity
    # must never reach even the chain-resolution step.
    # `job_id` alone (neither `lease_worker_id` nor `lease_generation` given) is the
    # pre-existing, unrelated "plain attribution for the disclosure ledger" use (see this
    # function's own docstring) and must raise nothing here -- that was a real regression
    # found reconciling this merge: treating job_id as a third member of the lease triple
    # denied every caller that only ever passed job_id for attribution, which is most of
    # them. The lease assertion is specifically about lease_worker_id/lease_generation;
    # job_id only joins the "incomplete" check once at least one of those two is present.
    _verify_lease_assertion(db, job_id=job_id, lease_worker_id=lease_worker_id, lease_generation=lease_generation)

    if selection_policy is not None and owner_id is None:
        # New controlled-routing calls must carry authoritative caller context; selection
        # permission alone must never opt into the legacy egress bypass.
        raise ModelSelectionError("controlled chat routing requires owner_id for egress")
    chain = _filter_local_only(resolve_chat_chain(db, selection_policy=selection_policy))
    attempted: list[str] = []
    last_error: Exception | None = None

    for provider, model in chain:
        attempted.append(provider.name)

        # Checked BEFORE egress enforcement, deliberately (reconciliation fix: this used to run
        # inside the try block below, AFTER the egress-policy block, which meant a candidate
        # that selection_policy had already rejected still incurred a real owner-scoped
        # disclosure-ledger write for a provider that was never actually going to be attempted
        # -- semantically wrong (no genuine attempt happened) and, found empirically, fatal
        # whenever the caller's session has no matching `app.current_user_id` RLS context (the
        # disclosure table's INSERT policy requires owner_id to match it), surfacing as a raw
        # RLS/DB error instead of the intended ModelSelectionError/ProviderError. A policy
        # rejection here is specific to THIS candidate, unlike an EgressDeniedError's content-
        # wide denial below, so it is caught and continues the loop over the chain rather than
        # aborting the whole call.
        if selection_policy is not None:
            try:
                _require_policy_candidate(db, get_model_spec(provider.name, model), selection_policy)
            except ModelSelectionError as exc:
                safe = classify_provider_exception(exc)
                logger.warning("Chat provider %s rejected by selection policy (%s): %s",
                                provider.name, safe.result.value, safe.message)
                last_error = exc
                continue

        # Hardening finding (Task #60 security-acceptance testing): egress-policy enforcement
        # is a policy/authorization decision, NOT a provider-connectivity concern -- so it is
        # DELIBERATELY outside the try block below that classifies ordinary provider failures
        # for fallback. Before this fix, an exception raised here (e.g. a real FK violation from
        # a non-existent owner_id, or a real RLS denial from a cross-tenant owner_id under the
        # wrong session context -- neither of which is an `EgressDeniedError`, both of which are
        # genuine database-level denials) was silently swallowed by the generic handler and
        # misclassified as "this provider is unreachable, try the next one" -- which is both
        # misleading (masks a real authorization failure as a connectivity blip) and, with more
        # than one provider configured, would have RETRIED the identical doomed write against
        # every remaining provider in the chain. Any exception here -- EgressDeniedError or
        # otherwise -- now aborts the WHOLE call immediately; it is never provider-specific and
        # no other provider in the chain would succeed where this one didn't.
        outbound_messages = messages
        if owner_id is not None:
            payload = [{"role": m.role, "content": m.content} for m in messages]
            redacted = enforce_egress_policy(
                db,
                owner_id=owner_id,
                provider=provider.name,
                model=model,
                purpose=purpose or "chat",
                requested_by=requested_by or "providers.registry.chat_with_fallback",
                payload=payload,
                task_id=task_id,
                goal_id=goal_id,
                job_id=job_id,
            )
            outbound_messages = [Message(role=m["role"], content=m["content"]) for m in redacted]
        invoked = False
        try:
            if not provider.is_configured():
                raise ProviderError("Provider is not configured.", category="not_configured",
                                    provider_request_may_have_left=False)
            if attempt_observer is not None:
                attempt_observer.before(provider.name, model)
                # Disclosure and durable reply claim precede the external effect.
                db.commit()
            invoked = True
            result = await provider.chat(outbound_messages, model=model, **kwargs)
            if effect_guard and (result.provider != provider.name or result.model != model):
                raise ProviderError("Provider response identity mismatch.", category="external_effect_unknown",
                                    provider_request_may_have_left=True)
            if effect_guard and (not isinstance(result.content, str) or not result.content.strip()):
                raise ProviderError("Provider returned no valid reply.", category="external_effect_unknown",
                                    provider_request_may_have_left=True)
            if effect_guard and attempt_observer is not None and getattr(attempt_observer, "requires_usage", True):
                usage = result.raw_usage
                if (not isinstance(usage, dict)
                        or type(usage.get("prompt_tokens")) is not int or usage["prompt_tokens"] < 0
                        or type(usage.get("completion_tokens")) is not int or usage["completion_tokens"] <= 0):
                    raise ProviderError("Provider usage is unverified.", category="external_effect_unknown",
                                        provider_request_may_have_left=True)
            if attempt_observer is not None:
                attempt_observer.succeeded(result)
            if len(attempted) > 1:
                logger.warning(
                    "Chat fallback engaged: %s failed over to %s (attempted: %s)",
                    attempted[0],
                    provider.name,
                    attempted,
                )
            return result, attempted
        except EgressDeniedError:
            # A denial is about the CONTENT, not this specific provider -- every other
            # provider in the chain would deny identically. Never treated as a per-provider
            # failure to fall back from; abort the whole call immediately.
            raise
        except Exception as exc:  # noqa: BLE001 - any REAL provider-call failure should trigger fallback, not just ProviderError
            # Preserve a hard content denial even when a development/test module reload has
            # produced a second EgressDeniedError class identity. This is deliberately
            # fail-closed: a provider can at most stop fallback by forging the marker; it can
            # never make denied content eligible for another provider.
            if getattr(exc, "is_egress_denial", False):
                raise
            if attempt_observer is not None:
                if not invoked:
                    # Budget/config refusal is authoritative; do not route around it.
                    raise
                attempt_observer.failed(exc)
            if effect_guard and invoked and getattr(exc, "provider_request_may_have_left", None) is not False:
                raise ProviderError(
                    "Provider outcome is uncertain; automatic retry and fallback are blocked.",
                    category="external_effect_unknown", provider_request_may_have_left=True,
                ) from None
            safe = classify_provider_exception(exc)
            logger.warning("Chat provider %s failed (%s): %s", provider.name, safe.result.value, safe.message)
            last_error = exc

    outcome = classify_provider_exception(last_error) if last_error is not None else None
    category = outcome.result.value if outcome else "unreachable"
    safe_detail = outcome.message if outcome else "Okänt fel inträffade."
    raise ProviderError(
        f"Alla konfigurerade leverantörer misslyckades ({', '.join(attempted)}). {safe_detail}",
        category=category,
    )
