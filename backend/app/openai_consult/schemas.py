"""Request/response models for the OpenAI consultation surface.

Deliberately narrow: a caller can shape the prompt, instructions, an optional JSON-schema
structured response, and the per-request output-token ceiling. Everything security-relevant
(model allowlist, budgets, tools, storage) is operator configuration, NOT client input.
"""

from typing import Any

from pydantic import BaseModel, Field, field_validator


class ConsultIn(BaseModel):
    """A single consultation request. `prompt` is the only required field."""

    prompt: str = Field(min_length=1, max_length=32_000)
    # Optional operator-of-the-moment steering text (role/style framing). Routed through the
    # egress gate like every other outbound content — a NEVER_EGRESS-marked value denies the
    # whole call exactly like the prompt itself would.
    instructions: str | None = Field(default=None, max_length=8_000)
    # Optional model override — must be an EXACT match of one allowlisted model id
    # (openai_consult_allowed_models). None resolves to the first allowlisted model.
    model: str | None = Field(default=None, max_length=128)
    # Optional per-request ceiling — clamped to never exceed
    # openai_consult_max_output_tokens. Responses-API minimum is 16.
    max_output_tokens: int | None = Field(default=None, ge=16, le=100_000)
    # Optional structured-response contract (Responses API `text.format` json_schema). The
    # schema itself is developer configuration shipped inside the request, not content sent
    # for the model to reason about, but it still passes the egress gate's scan like every
    # other field — its redaction is a no-op for normal schemas and a hard deny for
    # NEVER_EGRESS-marked content, which is exactly the posture we want.
    response_schema: dict[str, Any] | None = None
    response_schema_name: str = Field(default="response", max_length=64)
    # Ask for OpenAI's hosted read-only web_search tool for THIS request. Only honored when
    # openai_consult_web_search_enabled is true; every other tool type is structurally
    # impossible to request (there is no field to put it in).
    allow_web_search: bool = False

    @field_validator("model")
    @classmethod
    def _strip_model(cls, value: str | None) -> str | None:
        return value.strip() if value else value

    @field_validator("response_schema")
    @classmethod
    def _validate_response_schema(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        # json_schema must at least be an object schema; deeper validation is OpenAI's job —
        # a malformed schema surfaces as a provider-side 400, which classify_provider_exception
        # maps to a safe category instead of a raw provider error body.
        if value is not None and value.get("type") != "object":
            raise ValueError("response_schema must be a JSON schema of type 'object'")
        return value


class ConsultUsageOut(BaseModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int


class ConsultOut(BaseModel):
    """A successful consultation. Never contains credential material, request URLs or raw
    provider error bodies — only the answer, its usage, and the caller's own budget state."""

    model: str
    output_text: str
    structured: bool  # True when a response_schema was requested and the answer is parsed JSON
    structured_json: Any | None = None  # parsed JSON when structured=True, else None
    usage: ConsultUsageOut
    cost_usd: str  # Decimal string from app/providers/pricing.py — "unknown" is impossible (unpriceable models are denied at the gate)
    request_id: str | None = None  # OpenAI's own response id, for the founder's dashboard reconciliation


class ConsultStatusOut(BaseModel):
    """GET /api/openai-consult/status — operator visibility WITHOUT secrets. There is no
    field here that could carry the API key, its prefix, or any request URL."""

    enabled: bool
    key_configured: bool  # bool only — never any part of the key itself
    allowed_models: list[str]
    max_output_tokens: int
    requests_per_minute: int
    daily_token_budget: int
    daily_cost_budget_usd: str
    web_search_enabled: bool
    store_responses: bool
    tokens_used_today: int
    cost_used_today_usd: str
