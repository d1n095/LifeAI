"""Auditable model capability catalog used by the existing provider registry.

This module is metadata and deterministic policy evaluation only.  It does not invoke a
provider, authorize spend, grant egress, or create a second routing gateway.  Dynamic facts
(configuration and exact-model verification) are joined by app.providers.registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
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
    as_of: str


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


class ModelSelectionError(ValueError):
    pass


def _price(
    input_price: str | None,
    output_price: str | None,
    *,
    source: str,
    as_of: str = "2026-11-19",
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


def evaluate_model(spec: ModelSpec, policy: ModelSelectionPolicy) -> tuple[bool, str]:
    required = set(policy.required_capabilities)
    required.add(policy.role)
    if not required.issubset(spec.capabilities):
        return False, "required capability is not declared"
    if spec.availability not in (ModelAvailability.active, ModelAvailability.variable):
        return False, f"availability is {spec.availability.value}"
    if spec.external and not policy.allow_external:
        return False, "external provider is not allowed"
    if policy.allowed_providers and spec.provider not in policy.allowed_providers:
        return False, "provider is outside the allowlist"
    if policy.allowed_models and spec.model_id not in policy.allowed_models:
        return False, "model is outside the allowlist"
    required_context = max(policy.minimum_context_tokens, policy.estimated_input_tokens)
    if required_context:
        if spec.context_window_tokens is None:
            return False, "context limit is unknown"
        if required_context > spec.context_window_tokens:
            return False, "context requirement exceeds the declared model limit"
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
    return True, "eligible"
