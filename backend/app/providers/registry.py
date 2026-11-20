from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

import logging
import uuid
from typing import TYPE_CHECKING

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
    model_specs,
    resolve_model_spec,
)
from app.providers.deepseek_provider import DeepSeekProvider
from app.providers.gemini_provider import GeminiProvider
from app.providers.ollama_provider import OllamaProvider
from app.providers.openai_provider import OpenAIProvider
from app.providers.openrouter_provider import OpenRouterProvider
from app.providers.zai_provider import ZAIProvider

if TYPE_CHECKING:
    from app.egress_policy.service import WorkerLease

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
    # No nesting: an evaluation scope may never wrap (or be wrapped by) another scope, so
    # an evaluation identity cannot be silently swapped or re-entered over production.
    if _evaluation_context.get() is not None:
        raise ProviderError("An evaluation scope is already active.", category="unsupported",
                            provider_request_may_have_left=False)
    if provider_class.name != "zai_eval" or set(models) != {"chat", "embedding"}:
        raise ProviderError("Invalid evaluation identity.", category="unsupported",
                            provider_request_may_have_left=False)
    if any(not isinstance(model, str) or not model.strip() or model.strip() != model
           or any(char in model for char in "*?")
           for model in models.values()):
        raise ProviderError("Evaluation models must be exact, non-empty identifiers.",
                            category="unsupported", provider_request_may_have_left=False)
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



def _requires_verification(spec: ModelSpec, policy: ModelSelectionPolicy) -> bool:
    return policy.require_verified and (spec.external or policy.require_verified_local)


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
    if _requires_verification(spec, policy) and not _model_is_freshly_verified(db, spec, policy.role):
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
            db,
            resolve_model_spec(
                requested_provider,
                requested_model,
                role=policy.role,
                allow_custom_local=policy.allow_custom_local,
            ),
            policy,
        )

    eligible: list[tuple[int, ModelSpec]] = []
    for index, spec in enumerate(model_specs()):
        allowed, _ = evaluate_model(spec, policy)
        if not allowed:
            continue
        provider = get_provider(spec.provider)
        if not provider.is_configured():
            continue
        if _requires_verification(spec, policy) and not _model_is_freshly_verified(db, spec, policy.role):
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
    if effect_guard and not provider.is_local:
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


async def chat_with_fallback(
    db: Session,
    messages: list[Message],
    *,
    owner_id: uuid.UUID | None = None,
    worker_lease: WorkerLease | None = None,
    purpose: str | None = None,
    requested_by: str | None = None,
    task_id: uuid.UUID | None = None,
    goal_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    selection_policy: ModelSelectionPolicy | None = None,
    effect_guard: bool = False,
    attempt_observer=None,
    **kwargs,
) -> tuple[ChatResult, list[str]]:
    """Try the primary chat provider, then fall back through CHAT_FALLBACK_ORDER on failure.

    Returns the successful result plus the list of provider names that were attempted
    (including the one that succeeded), so callers can log/audit exactly what happened
    instead of it being invisible.

    Life Vault / External-AI Egress Control (docs/LIFE_VAULT_EGRESS_CONTROL.md, V1): an
    external chain now requires either `owner_id` or a verified `worker_lease`; an ownerless
    external call is denied before any provider is contacted. A local-only chain (Ollama) may
    still run without an owner because nothing leaves the host. When an owner is present,
    every attempt is routed through `enforce_egress_policy()` first -- default-deny gate,
    hard-deny on NEVER_EGRESS-marked content (never partial-send, and never treated as "try
    the next provider" -- the content itself is what's denied, not a specific provider being
    unreachable), otherwise secret-pattern redacted, with exactly one disclosure-ledger row
    per real attempt. A worker lease is re-verified against `mainai_jobs` immediately before
    the call; a revoked, expired or taken-over lease fails closed.
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

    if selection_policy is not None and owner_id is None and worker_lease is None:
        # New controlled-routing calls must carry authoritative caller context; selection
        # permission alone must never opt into the legacy egress bypass.
        raise ModelSelectionError("controlled chat routing requires owner_id for egress")
    chain = resolve_chat_chain(db, selection_policy=selection_policy)

    # External providers are never allowed to run on the legacy ownerless path. A local-only
    # chain may still run without an owner because nothing leaves the host. A worker presents
    # its existing mainai_jobs lease, which is re-verified here (owner, worker, generation,
    # running state, expiry) immediately before any provider call.
    from app.egress_policy.service import resolve_egress_owner

    if any(not provider.is_local for provider, _model in chain) or worker_lease is not None:
        owner_id = resolve_egress_owner(
            db, owner_id=owner_id, worker_lease=worker_lease
        )

    attempted: list[str] = []
    last_error: Exception | None = None

    for provider, model in chain:
        attempted.append(provider.name)
        invoked = False
        try:
            if selection_policy is not None:
                # The chain was built earlier; a concurrent verification failure may now exist.
                _require_policy_candidate(
                    db,
                    resolve_model_spec(
                        provider.name,
                        model,
                        role=selection_policy.role,
                        allow_custom_local=selection_policy.allow_custom_local,
                    ),
                    selection_policy,
                )
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
            if not provider.is_configured():
                raise ProviderError("Provider is not configured.", category="not_configured",
                                    provider_request_may_have_left=False)
            if attempt_observer is not None:
                attempt_observer.before(provider.name, model)
                # Disclosure and durable reply claim precede the external effect.
                db.commit()
                # Durable invocation boundary: after this commit the attempt can no longer
                # be proven pre-egress, so recovery will not release it blind.
                marker = getattr(attempt_observer, "invoking", None)
                if marker is not None:
                    marker()
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
        except Exception as exc:  # noqa: BLE001 - any provider failure should trigger fallback, not just ProviderError
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
