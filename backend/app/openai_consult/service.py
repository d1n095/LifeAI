"""The consultation pipeline — every call passes through ALL of these gates, in order:

1. CONFIG GATE   openai_consult_enabled + real (non-placeholder) OPENAI_API_KEY +
                 non-empty model allowlist. Any miss → ConsultationConfigError before
                 anything else happens (and before any Redis/DB/network touch).
2. MODEL GATE    exact allowlist match + a pricing entry in app/providers/pricing.py.
                 Unpriceable models are refused outright: an unpriceable model is an
                 unbudgetable one, and this module never spends what it cannot count.
3. EGRESS GATE   app/egress_policy/service.enforce_egress_policy() — default-deny,
                 NEVER_EGRESS hard-deny, secret-pattern redaction, one ProviderDisclosureEvent
                 row per call. ONLY the redacted payload is used to build the outbound request.
4. BUDGET GATE   reserve estimated tokens/cost for the caller's UTC day (BudgetExceededError
                 before any network I/O).
5. HTTP CALL     POST {base}/responses with a hard httpx timeout, store=False by default,
                 at most the hosted read-only web_search tool. No client-supplied tools are
                 representable in the request model.
6. SETTLE        reconcile the reservation with real usage; an unusable/missing usage object
                 keeps the full reservation in place (conservative: under-admission, never
                 over-admission).

Every failure surfaced to a caller is either one of this module's own error types or a
safe ProviderError category — never a raw httpx exception (which can embed the request URL)
and never credential material.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from decimal import Decimal
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.providers.base import looks_like_placeholder_secret
from app.providers.pricing import estimate_cost

logger = logging.getLogger(__name__)

BASE_URL = "https://api.openai.com/v1"
RESPONSES_PATH = "/responses"

# Structured-output answer size guard — a model answer is data, never authority; this only
# stops an absurd answer from being json-parsed wholesale. 1 MiB is far above any legitimate
# structured response for this surface.
_MAX_STRUCTURED_JSON_BYTES = 1_000_000

# Rough pre-call prompt estimate for the budget reservation (settled with real usage after
# the call). ~4 chars/token is the usual English/code heuristic; non-Latin scripts can run
# several chars/token, so this UNDER-estimates deliberately only in the direction the
# settle-step corrects — the max_output_tokens half of the reservation is the hard worst
# case and is always charged in full up front.
_CHARS_PER_TOKEN = 4


class ConsultationConfigError(RuntimeError):
    """The integration is not (fully) configured — always fail-closed, always the caller's
    signal to stop rather than retry. Message is safe to show an authenticated founder:
    it names the missing configuration, never any secret value."""


class ConsultationServiceError(RuntimeError):
    """A gated provider-call failure mapped to a safe category (same family as
    app.providers.base.ProviderError.category: "unreachable", "rate_limited",
    "invalid_key", "unsupported", "not_configured", "timeout"). Message never contains
    the request URL or credential material."""


def _allowed_models(settings: Settings) -> list[str]:
    return [m.strip() for m in settings.openai_consult_allowed_models.split(",") if m.strip()]


def resolve_gate(settings: Settings) -> tuple[str, bool]:
    """CONFIG GATE. Returns (api_key, web_search_allowed). Raises ConsultationConfigError
    with a founder-safe message when anything is missing. Order matters: the enabled flag
    is checked first so an unconfigured deployment never even evaluates the other checks."""
    if not settings.openai_consult_enabled:
        raise ConsultationConfigError(
            "OpenAI-konsultation är avstängd (OPENAI_CONSULT_ENABLED=false). "
            "Aktivera den explicit efter grundarens godkännande."
        )
    key = settings.openai_api_key
    if not key or looks_like_placeholder_secret(key):
        raise ConsultationConfigError(
            "OPENAI_API_KEY är inte konfigurerad med en riktig nyckel — konsultation vägras (fail-closed)."
        )
    if not _allowed_models(settings):
        raise ConsultationConfigError(
            "OPENAI_CONSULT_ALLOWED_MODELS är tom — ingen modell är tillåten (fail-closed)."
        )
    return key, settings.openai_consult_web_search_enabled


def resolve_model(settings: Settings, requested: str | None) -> str:
    """MODEL GATE: exact allowlist match (or first allowlisted when unrequested) + proof the
    model is priceable. Fail-closed on both."""
    allowed = _allowed_models(settings)
    if requested is not None:
        if requested not in allowed:
            raise ConsultationServiceError(
                f"Modell '{requested}' är inte allowlistad för konsultation.",  # noqa: no secret; model id is operator config
            )
        model = requested
    else:
        model = allowed[0]
    # Pricing proof: a pricing entry makes estimate_cost return a Decimal for any token
    # count; None means unbudgetable → refuse (fail-closed) before any spend is possible.
    if estimate_cost("openai", model, 1_000, 1_000) is None:
        raise ConsultationServiceError(
            f"Modell '{model}' saknar prisdata i app/providers/pricing.py — konsultation vägras "
            "tills priset är konfigurerat (fail-closed: en oprisad modell kan inte budgeteras)."
        )
    return model


def build_responses_payload(
    *,
    model: str,
    prompt: str,
    instructions: str | None,
    max_output_tokens: int,
    store: bool,
    web_search: bool,
    response_schema: dict[str, Any] | None,
    response_schema_name: str,
) -> dict[str, Any]:
    """Build the Responses API request body from ALREADY-EGRESS-REDACTED content only.

    No `tools` field exists unless web_search is true — and web_search is the only tool this
    builder can ever emit. There is deliberately no parameter through which a caller could
    request code_interpreter, file_search, computer_use, function calls or any custom tool:
    the request schema has no such field, and the settings gate is operator-controlled."""
    payload: dict[str, Any] = {
        "model": model,
        "input": [{"role": "user", "content": prompt}],
        "max_output_tokens": max_output_tokens,
        "store": store,
    }
    if instructions:
        payload["instructions"] = instructions
    if response_schema is not None:
        payload["text"] = {
            "format": {
                "type": "json_schema",
                "name": response_schema_name or "response",
                "schema": response_schema,
                "strict": True,
            }
        }
    if web_search:
        payload["tools"] = [{"type": "web_search"}]
    return payload


def _create_client(timeout: float) -> httpx.AsyncClient:
    """One client per call (mirrors app/providers/openai_provider.py's pattern). Tests
    monkeypatch this to inject an httpx.MockTransport-backed client."""
    return httpx.AsyncClient(timeout=timeout)


def _extract_output_text(data: dict[str, Any]) -> str:
    """Walk the documented Responses API shape: output[] items of type "message" carrying
    content[] items of type "output_text". Unknown item types are skipped; an answer with
    NO text at all is a failure, never silently empty (fail-closed)."""
    parts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
    if not parts:
        raise ConsultationServiceError(
            "Svarsvaret saknade uttext (output_text) — vägrar returnera ett tomt eller okänt "
            "svarsformat (fail-closed)."
        )
    return "".join(parts)


def _extract_usage(data: dict[str, Any]) -> tuple[int, int]:
    """Responses API usage object: usage.input_tokens / usage.output_tokens. Missing usage is
    a hard failure — the caller keeps its reservation (conservative) and the call is not
    treated as a success the budget never saw."""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        raise ConsultationServiceError(
            "Svarsvaret saknade usage-objekt — budgeten kan inte stämmas av, anropet behandlas "
            "som misslyckat (fail-closed)."
        )
    try:
        return int(usage["input_tokens"]), int(usage["output_tokens"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConsultationServiceError(
            "Svarsvarets usage-objekt var ofullständigt — budgeten kan inte stämmas av (fail-closed)."
        ) from exc


def _map_http_error(status_code: int) -> ConsultationServiceError:
    """Status → safe category + founder-safe message. The provider's raw error body is
    deliberately never surfaced (it can echo request content; classify_provider_exception's
    docstring documents the same stance for exception text)."""
    if status_code in (401, 403):
        return ConsultationServiceError(
            "OpenAI avvisade autentiseringen (ogiltig/otillåten API-nyckel) — kontrollera "
            "OPENAI_API_KEY i miljön (endast servern)."
        )
    if status_code == 429:
        return ConsultationServiceError(
            "OpenAI rate-limitade anropet (kategori: rate_limited) — försök igen senare.",
        )
    return ConsultationServiceError(
        f"OpenAI svarade med oförväntad status {status_code} (kategori: unreachable)."
    )


async def consult_openai(
    db,
    *,
    owner_id: uuid.UUID,
    payload_in,  # ConsultIn — typed loosely to keep this module import-light for tests
    settings: Settings | None = None,
    budget_store=None,
    requested_by: str = "app.openai_consult.service.consult_openai",
    purpose: str = "openai_consult",
    task_id: uuid.UUID | None = None,
    goal_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
) -> tuple[Any, dict[str, int]]:
    """Run one gated consultation. Returns (ConsultOut, {"reserved_tokens", "settled_in",
    "settled_out"}) for the router's logging. Raises ConsultationConfigError,
    ConsultationServiceError, BudgetExceededError or EgressDeniedError — the router maps
    each to a safe HTTP response.

    `purpose`/`task_id`/`goal_id`/`job_id` flow into the egress disclosure ledger so
    agent-initiated consultations (app/openai_consult/mainai.py) are auditable against
    their goal/task/job context; the founder HTTP path keeps the defaults.

    `db` is the request's SQLAlchemy session (egress gate needs it for the disclosure
    ledger). The module writes NOTHING else to the database."""
    settings = settings or get_settings()

    # 1. CONFIG GATE
    api_key, web_search_allowed = resolve_gate(settings)

    # 2. MODEL GATE
    model = resolve_model(settings, payload_in.model)
    max_output = min(
        payload_in.max_output_tokens if payload_in.max_output_tokens is not None else settings.openai_consult_max_output_tokens,
        settings.openai_consult_max_output_tokens,
    )

    # 3. EGRESS GATE — default-deny, redacts secrets, hard-denies NEVER_EGRESS content.
    #    Only the redacted values flow into the outbound request.
    from app.egress_policy import enforce_egress_policy

    outbound = enforce_egress_policy(
        db,
        owner_id=owner_id,
        provider="openai",
        model=model,
        purpose=purpose,
        requested_by=requested_by,
        task_id=task_id,
        goal_id=goal_id,
        job_id=job_id,
        payload={
            "prompt": payload_in.prompt,
            "instructions": payload_in.instructions,
            "response_schema": payload_in.response_schema,
        },
    )
    prompt = outbound.get("prompt") or ""
    instructions = outbound.get("instructions")
    response_schema = outbound.get("response_schema")

    # 4. BUDGET GATE — reserve worst case: estimated prompt + the FULL max_output_tokens.
    est_prompt_tokens = math.ceil(len(prompt) / _CHARS_PER_TOKEN) + math.ceil(
        len(instructions or "") / _CHARS_PER_TOKEN
    )
    reserved_tokens = est_prompt_tokens + max_output
    estimated_cost = estimate_cost("openai", model, est_prompt_tokens, max_output)
    assert estimated_cost is not None  # MODEL GATE proved priceability; keep the type checker honest
    store = budget_store  # already built by the router from settings
    store.reserve(owner_id, estimated_tokens=reserved_tokens, estimated_cost_usd=estimated_cost)

    # 5. HTTP CALL — the only place this module touches the network.
    body = build_responses_payload(
        model=model,
        prompt=prompt,
        instructions=instructions,
        max_output_tokens=max_output,
        store=settings.openai_consult_store_responses,
        web_search=web_search_allowed and payload_in.allow_web_search,
        response_schema=response_schema,
        response_schema_name=payload_in.response_schema_name,
    )
    def _release_reservation() -> None:
        """Release the reservation after a call that consumed nothing (provider unreachable,
        timeout, HTTP error). Zero tokens were spent — keeping the worst-case reservation
        would let a provider outage silently consume the owner's whole day (an availability
        loss with no security gain: the conservative leak only makes sense for calls that
        actually consumed budget). Best-effort: if the budget store itself is unreachable,
        the reservation leaks (conservative direction) and the provider error still wins."""
        try:
            store.settle(
                owner_id,
                reserved_tokens=reserved_tokens,
                actual_tokens=0,
                reserved_cost_usd=estimated_cost,
                actual_cost_usd="0",
            )
        except Exception:  # noqa: BLE001 — never mask the original provider error
            pass

    try:
        async with _create_client(settings.openai_consult_timeout_seconds) as client:
            resp = await client.post(
                f"{BASE_URL}{RESPONSES_PATH}",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
    except asyncio.CancelledError:
        # Cancellation (client disconnect, harness timeout, shutdown) inherits from
        # BaseException, not Exception — without this handler a cancelled call would
        # leak its worst-case reservation until UTC midnight. Release, then propagate.
        _release_reservation()
        raise
    except httpx.TimeoutException as exc:
        _release_reservation()
        # Nothing about the URL/key is surfaced.
        raise ConsultationServiceError(
            "OpenAI svarade inte inom tidsgränsen (kategori: timeout). Reservationen har "
            "släppts tillbaka till dagens budget."
        ) from exc
    except httpx.HTTPStatusError as exc:
        _release_reservation()
        raise _map_http_error(exc.response.status_code) from exc
    except ConsultationServiceError:
        _release_reservation()
        raise
    except Exception as exc:  # noqa: BLE001 — never leak a raw exception (may embed the URL)
        _release_reservation()
        logger.warning("openai_consult: provider call failed: %s", type(exc).__name__)
        raise ConsultationServiceError(
            "OpenAI-anropet misslyckades (kategori: unreachable) — inget svar applicerat. "
            "Reservationen har släppts tillbaka till dagens budget."
        ) from exc

    # 6. PARSE + SETTLE
    output_text = _extract_output_text(data)
    input_tokens, output_tokens = _extract_usage(data)
    actual_cost = estimate_cost("openai", model, input_tokens, output_tokens)
    assert actual_cost is not None
    store.settle(
        owner_id,
        reserved_tokens=reserved_tokens,
        actual_tokens=input_tokens + output_tokens,
        reserved_cost_usd=estimated_cost,
        actual_cost_usd=actual_cost,
    )
    # Revenue & Compute Engine (P1 review fix): the consultation surface's own budgets
    # (openai_consult/budgets.py) stay authoritative for THAT surface, but every provider
    # call must ALSO land in the platform ledger — one accounting pipeline, no dark spend.
    from app.billing import record_usage as _record_usage

    _record_usage(
        db,
        user_id=owner_id,
        tenant_id=None,
        role="consult",
        provider="openai",
        model=model,
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        agent="openai-consult",
        task="consultation",
    )

    structured = response_schema is not None
    structured_json = None
    if structured:
        # Fail-closed structured parsing: a claimed-JSON answer that isn't JSON is an error,
        # never raw passthrough of unvalidated text into a caller expecting a schema.
        try:
            if len(output_text.encode("utf-8")) > _MAX_STRUCTURED_JSON_BYTES:
                raise ValueError("structured answer exceeds size guard")
            structured_json = json.loads(output_text)
        except ValueError as exc:
            raise ConsultationServiceError(
                "Det strukturerade svaret var inte giltig JSON — vägrar returnera det "
                "(fail-closed). Reservationen finns kvar på dagens budget."
            ) from exc

    response_id = data.get("id") if isinstance(data.get("id"), str) else None
    return (
        _build_consult_out(
            model=model,
            output_text=output_text,
            structured=structured,
            structured_json=structured_json,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=actual_cost,
            request_id=response_id,
        ),
        {
            "reserved_tokens": reserved_tokens,
            "settled_in": input_tokens,
            "settled_out": output_tokens,
        },
    )


def _build_consult_out(*, model, output_text, structured, structured_json, input_tokens, output_tokens, cost, request_id):
    from app.openai_consult.schemas import ConsultOut, ConsultUsageOut

    return ConsultOut(
        model=model,
        output_text=output_text,
        structured=structured,
        structured_json=structured_json,
        usage=ConsultUsageOut(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
        cost_usd=str(cost),
        request_id=request_id,
    )


def status_snapshot(settings: Settings, budget_store, owner_id: uuid.UUID):
    """Founder-safe status: configuration shape + the caller's own remaining budget. There
    is deliberately no field through which any part of the API key could be returned."""
    from app.openai_consult.schemas import ConsultStatusOut

    enabled = False
    key_configured = False
    try:
        resolve_gate(settings)
        enabled = True
        key_configured = True
    except ConsultationConfigError:
        key_configured = bool(settings.openai_api_key) and not looks_like_placeholder_secret(
            settings.openai_api_key
        )
    tokens_left, cost_left = budget_store.remaining(str(owner_id))
    return ConsultStatusOut(
        enabled=enabled,
        key_configured=key_configured,
        allowed_models=_allowed_models(settings),
        max_output_tokens=settings.openai_consult_max_output_tokens,
        requests_per_minute=settings.openai_consult_requests_per_minute,
        daily_token_budget=settings.openai_consult_daily_token_budget,
        daily_cost_budget_usd=str(settings.openai_consult_daily_cost_budget_usd),
        web_search_enabled=settings.openai_consult_web_search_enabled,
        store_responses=settings.openai_consult_store_responses,
        tokens_used_today=max(settings.openai_consult_daily_token_budget - tokens_left, 0),
        cost_used_today_usd=str(
            max(
                Decimal(str(settings.openai_consult_daily_cost_budget_usd)) - Decimal(cost_left),
                Decimal("0"),
            ).quantize(Decimal("0.000001"))
        ),
    )
