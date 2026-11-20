"""Auditable model capability catalog used by the existing provider registry.

This module is metadata and deterministic policy evaluation only.  It does not invoke a
provider, authorize spend, grant egress, or create a second routing gateway.  Dynamic facts
(configuration and exact-model verification) are joined by app.providers.registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from datetime import date
from enum import Enum


class ModelAvailability(str, Enum):
    active = "active"
    variable = "variable"
    deprecated = "deprecated"
    unavailable = "unavailable"


class DeploymentLocality(str, Enum):
    local = "local"
    external = "external"


@dataclass(frozen=True)
class ModelPricing:
    """Public list-price snapshot. None is intentional and must never be estimated."""

    input_per_million_usd: Decimal | None
    output_per_million_usd: Decimal | None
    source: str
    as_of: str | None


@dataclass(frozen=True)
class ModelQuota:
    """Only provider-published model limits belong here; account-tier limits stay unknown."""

    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None
    note: str = "account-tier dependent; verify with the provider"


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model_id: str
    version: str
    capabilities: frozenset[str]
    context_window_tokens: int | None
    locality: DeploymentLocality
    license_name: str
    license_url: str | None
    pricing: ModelPricing
    quota: ModelQuota
    availability: ModelAvailability = ModelAvailability.active

    @property
    def supported_capabilities(self) -> frozenset[str]:
        # Current Message/ChatResult transports only expose text chat and embeddings.
        # Model-vendor claims must not imply that tools/vision/JSON contracts are wired.
        return self.capabilities & frozenset({"chat", "embedding"})

    @property
    def external(self) -> bool:
        return self.locality == DeploymentLocality.external


@dataclass(frozen=True)
class ModelSelectionPolicy:
    """Caller-owned restrictions. A model response can never alter these values."""

    role: str = "chat"
    required_capabilities: frozenset[str] = field(default_factory=frozenset)
    minimum_context_tokens: int = 0
    estimated_input_tokens: int = 0
    allow_external: bool = False
    prefer_local: bool = True
    allowed_providers: frozenset[str] = field(default_factory=frozenset)
    allowed_models: frozenset[str] = field(default_factory=frozenset)
    max_input_price_per_million_usd: Decimal | None = None
    max_output_price_per_million_usd: Decimal | None = None
    require_verified: bool = True
    # Local runtimes can carry model tags the shared catalog cannot enumerate. This permits
    # only an exact configured local tag; external models still need a catalog entry so an
    # unknown external price/capability can never be silently admitted.
    allow_custom_local: bool = False
    # Rollout compatibility: an already-working local configuration may predate exact-model
    # verification. External models are always verified when require_verified is set.
    require_verified_local: bool = True
    estimated_output_tokens: int = 0
    max_pricing_age_days: int = 30


class ModelSelectionError(ValueError):
    pass


def _price(
    input_price: str | None,
    output_price: str | None,
    *,
    source: str,
    as_of: str | None = None,
) -> ModelPricing:
    return ModelPricing(
        input_per_million_usd=Decimal(input_price) if input_price is not None else None,
        output_per_million_usd=Decimal(output_price) if output_price is not None else None,
        source=source,
        as_of=as_of,
    )


# Ordered deliberately. It is the deterministic tie-breaker after explicit policy preferences;
# it is not a benchmark ranking and must not be described as one.
_MODEL_SPECS: tuple[ModelSpec, ...] = (
    ModelSpec(
        provider="openai",
        model_id="gpt-4o-mini",
        version="gpt-4o-mini-2024-07-18",
        capabilities=frozenset({"chat", "json", "tools", "vision"}),
        context_window_tokens=128_000,
        locality=DeploymentLocality.external,
        license_name="OpenAI service terms",
        license_url="https://openai.com/policies/services-agreement/",
        pricing=_price("0.15", "0.60", source="https://developers.openai.com/api/docs/models/gpt-4o-mini"),
        quota=ModelQuota(note="usage-tier dependent; registry does not invent an RPM/TPM value"),
    ),
    ModelSpec(
        provider="openai",
        model_id="text-embedding-3-small",
        version="text-embedding-3-small",
        capabilities=frozenset({"embedding"}),
        context_window_tokens=8_191,
        locality=DeploymentLocality.external,
        license_name="OpenAI service terms",
        license_url="https://openai.com/policies/services-agreement/",
        pricing=_price("0.02", "0", source="existing LifeAI pricing snapshot"),
        quota=ModelQuota(),
    ),
    ModelSpec(
        provider="anthropic",
        model_id="claude-sonnet-5",
        version="claude-sonnet-5",
        capabilities=frozenset({"chat", "reasoning", "json", "tools", "vision"}),
        context_window_tokens=1_000_000,
        locality=DeploymentLocality.external,
        license_name="Anthropic commercial terms",
        license_url="https://www.anthropic.com/legal/commercial-terms",
        pricing=_price("3", "15", source="existing LifeAI pricing snapshot"),
        quota=ModelQuota(),
    ),
    ModelSpec(
        provider="gemini",
        model_id="gemini-2.5-flash",
        version="gemini-2.5-flash",
        capabilities=frozenset({"chat", "reasoning", "json", "tools", "vision"}),
        context_window_tokens=1_000_000,
        locality=DeploymentLocality.external,
        license_name="Google APIs terms",
        license_url="https://ai.google.dev/gemini-api/terms",
        pricing=_price("0.075", "0.30", source="existing LifeAI pricing snapshot"),
        quota=ModelQuota(),
    ),
    ModelSpec(
        provider="gemini",
        model_id="text-embedding-004",
        version="text-embedding-004",
        capabilities=frozenset({"embedding"}),
        context_window_tokens=2_048,
        locality=DeploymentLocality.external,
        license_name="Google APIs terms",
        license_url="https://ai.google.dev/gemini-api/terms",
        pricing=_price(None, None, source="not represented in LifeAI pricing; fail closed under a price ceiling"),
        quota=ModelQuota(),
    ),
    ModelSpec(
        provider="deepseek",
        model_id="deepseek-chat",
        version="compatibility alias",
        capabilities=frozenset({"chat", "reasoning", "json", "tools"}),
        context_window_tokens=128_000,
        locality=DeploymentLocality.external,
        license_name="DeepSeek platform terms",
        license_url="https://platform.deepseek.com/downloads/DeepSeek%20Open%20Platform%20Terms%20of%20Use.html",
        pricing=_price("0.27", "1.10", source="existing LifeAI pricing snapshot"),
        quota=ModelQuota(note="legacy configured model; verify availability before selection"),
        availability=ModelAvailability.variable,
    ),
    ModelSpec(
        provider="deepseek",
        model_id="deepseek-flash",
        version="DeepSeek-V4.1-Flash-1017",
        capabilities=frozenset({"chat", "reasoning", "json", "tools", "vision"}),
        context_window_tokens=1_000_000,
        locality=DeploymentLocality.external,
        license_name="DeepSeek platform terms",
        license_url="https://platform.deepseek.com/downloads/DeepSeek%20Open%20Platform%20Terms%20of%20Use.html",
        pricing=_price("0.30", "1.20", source="https://api-docs.deepseek.com/quick_start/pricing/ (peak, cache miss)"),
        quota=ModelQuota(note="provider publishes concurrency, not an account-independent RPM/TPM"),
    ),
    ModelSpec(
        provider="openrouter",
        model_id="openrouter/auto",
        version="provider-managed routing alias",
        capabilities=frozenset({"chat"}),
        context_window_tokens=None,
        locality=DeploymentLocality.external,
        license_name="OpenRouter terms plus selected model terms",
        license_url="https://openrouter.ai/terms",
        pricing=_price(None, None, source="route-dependent; must remain unknown"),
        quota=ModelQuota(note="route/account dependent"),
        availability=ModelAvailability.variable,
    ),
    ModelSpec(
        provider="ollama",
        model_id="llama3.1",
        version="local tag resolved by Ollama",
        capabilities=frozenset({"chat"}),
        context_window_tokens=128_000,
        locality=DeploymentLocality.local,
        license_name="model-dependent; Llama Community License for this tag",
        license_url="https://www.llama.com/llama3_1/license/",
        pricing=_price("0", "0", source="local inference; provider charge is zero"),
        quota=ModelQuota(note="host capacity controlled; no external provider quota"),
    ),
    ModelSpec(
        provider="ollama",
        model_id="nomic-embed-text",
        version="local tag resolved by Ollama",
        capabilities=frozenset({"embedding"}),
        context_window_tokens=8_192,
        locality=DeploymentLocality.local,
        license_name="Apache-2.0",
        license_url="https://huggingface.co/nomic-ai/nomic-embed-text-v1.5",
        pricing=_price("0", "0", source="local inference; provider charge is zero"),
        quota=ModelQuota(note="host capacity controlled; no external provider quota"),
    ),
    ModelSpec(
        provider="ollama", model_id="qwen2.5:0.5b", version="local mutable tag",
        capabilities=frozenset({"chat"}), context_window_tokens=None,
        locality=DeploymentLocality.local, license_name="Apache-2.0",
        license_url="https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct",
        pricing=_price("0", "0", source="local inference; no provider charge"), quota=ModelQuota(),
    ),
    ModelSpec(
        provider="ollama", model_id="gemma3:4b", version="local mutable tag",
        capabilities=frozenset({"chat"}), context_window_tokens=None,
        locality=DeploymentLocality.local, license_name="Gemma terms of use",
        license_url="https://ai.google.dev/gemma/terms",
        pricing=_price("0", "0", source="local inference; no provider charge"), quota=ModelQuota(),
    ),
    ModelSpec(
        provider="ollama", model_id="all-minilm", version="local mutable tag",
        capabilities=frozenset({"embedding"}), context_window_tokens=None,
        locality=DeploymentLocality.local, license_name="Apache-2.0",
        license_url="https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2",
        pricing=_price("0", "0", source="local inference; no provider charge"), quota=ModelQuota(),
    ),
    ModelSpec(
        provider="zai",
        model_id="glm-5.3",
        version="glm-5.3",
        capabilities=frozenset({"chat", "reasoning", "json", "tools"}),
        context_window_tokens=1_000_000,
        locality=DeploymentLocality.external,
        license_name="Z.ai API service terms",
        license_url="https://z.ai/terms-of-service",
        pricing=_price(None, None, source="official public page did not expose a stable list price; fail closed under a price ceiling"),
        quota=ModelQuota(note="account/subscription dependent; verify from Z.ai"),
    ),
)

_BY_KEY = {(spec.provider, spec.model_id): spec for spec in _MODEL_SPECS}


def model_specs() -> tuple[ModelSpec, ...]:
    return _MODEL_SPECS


def provider_model_specs(provider: str) -> tuple[ModelSpec, ...]:
    return tuple(spec for spec in _MODEL_SPECS if spec.provider == provider)


def get_model_spec(provider: str, model_id: str) -> ModelSpec:
    try:
        return _BY_KEY[(provider, model_id)]
    except KeyError as exc:
        raise ModelSelectionError(f"unregistered provider/model: {provider}/{model_id}") from exc


def custom_local_model_spec(provider: str, model_id: str, role: str) -> ModelSpec:
    """Describe an exact local model tag that is not in the shared catalog.

    This is metadata only, never a vendor claim: local inference has no provider charge and
    no catalog-published context limit. Only the local Ollama transport is eligible; an
    unregistered external model still fails closed because its price and capabilities are
    not independently verified.
    """

    if provider != "ollama":
        raise ModelSelectionError(f"unregistered provider/model: {provider}/{model_id}")
    if role not in {"chat", "embedding"}:
        raise ModelSelectionError("unsupported role")
    model_id = model_id.strip()
    if not model_id or len(model_id) > 128 or any(ord(c) < 32 for c in model_id):
        raise ModelSelectionError("invalid local model tag")
    return ModelSpec(
        provider=provider,
        model_id=model_id,
        version="configured local tag; catalog metadata unavailable",
        capabilities=frozenset({role}),
        context_window_tokens=None,
        locality=DeploymentLocality.local,
        license_name="model-dependent; verify with the local runtime",
        license_url=None,
        pricing=_price("0", "0", source="local inference; no provider charge"),
        quota=ModelQuota(note="host capacity controlled; no external provider quota"),
    )


def resolve_model_spec(
    provider: str,
    model_id: str,
    *,
    role: str,
    allow_custom_local: bool = False,
) -> ModelSpec:
    """Return the catalog spec, or an exact local custom spec when policy permits it."""

    try:
        return get_model_spec(provider, model_id)
    except ModelSelectionError:
        if not allow_custom_local:
            raise
        return custom_local_model_spec(provider, model_id, role)


def evaluate_model(spec: ModelSpec, policy: ModelSelectionPolicy) -> tuple[bool, str]:
    if policy.role not in {"chat", "embedding"}:
        return False, "unsupported role"
    if min(policy.minimum_context_tokens, policy.estimated_input_tokens,
           policy.estimated_output_tokens, policy.max_pricing_age_days) < 0:
        return False, "negative policy bound"
    for ceiling in (policy.max_input_price_per_million_usd, policy.max_output_price_per_million_usd):
        if ceiling is not None and (not Decimal(str(ceiling)).is_finite() or ceiling < 0):
            return False, "invalid price ceiling"
    required = set(policy.required_capabilities)
    required.add(policy.role)
    if not required.issubset(spec.supported_capabilities):
        return False, "required capability is not declared"
    if spec.availability not in (ModelAvailability.active, ModelAvailability.variable):
        return False, f"availability is {spec.availability.value}"
    if spec.external and not policy.allow_external:
        return False, "external provider is not allowed"
    if policy.allowed_providers and spec.provider not in policy.allowed_providers:
        return False, "provider is outside the allowlist"
    if policy.allowed_models and spec.model_id not in policy.allowed_models:
        return False, "model is outside the allowlist"
    required_context = max(policy.minimum_context_tokens, policy.estimated_input_tokens + policy.estimated_output_tokens)
    if required_context:
        if spec.context_window_tokens is None:
            return False, "context limit is unknown"
        if required_context > spec.context_window_tokens:
            return False, "context requirement exceeds the declared model limit"
    if required_context and not spec.external:
        # Vendor maximum != effective runtime num_ctx. No runtime context evidence is captured yet.
        return False, "effective local context limit is unknown"
    if policy.max_input_price_per_million_usd is not None:
        if spec.pricing.input_per_million_usd is None:
            return False, "input price is unknown"
        if spec.pricing.input_per_million_usd > policy.max_input_price_per_million_usd:
            return False, "input price exceeds the policy ceiling"
    if policy.max_output_price_per_million_usd is not None:
        if spec.pricing.output_per_million_usd is None:
            return False, "output price is unknown"
        if spec.pricing.output_per_million_usd > policy.max_output_price_per_million_usd:
            return False, "output price exceeds the policy ceiling"
    bounded_price = (policy.max_input_price_per_million_usd is not None
                     or policy.max_output_price_per_million_usd is not None)
    if bounded_price and spec.external:
        try:
            age = (date.today() - date.fromisoformat(spec.pricing.as_of or "")).days
        except ValueError:
            return False, "price snapshot date is unknown"
        if age < 0 or age > policy.max_pricing_age_days:
            return False, "price snapshot is stale"
    return True, "eligible"
