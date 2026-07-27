"""P1 (provider pre-flight verification): the real, minimal API call that answers "is this
provider actually usable right now" — never just app/providers/base.py's is_configured(),
which only checks that a key STRING is present, not that it works.

Used from two places:
  - app/rag/ingest.py, right before provider.embed() is ever called (ensure_verified) — a
    document whose provider isn't verified pauses on IndexStatus.awaiting_provider/
    blocked_provider instead of attempting (and failing) the real call.
  - app/worker.py's poll loop (_requeue_blocked_jobs), which re-checks the currently active
    provider and flips every ImportJobStatus.blocked job back to `pending` once it verifies
    ok — the mechanism that lets a paused import resume automatically, with no re-upload,
    once a founder fixes a key.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.provider_verification import ProviderVerificationCheck, VerificationResult
from app.providers.base import LLMProvider, Message, ProviderError
from app.providers.registry import resolve_active

VERIFICATION_PROBE_TEXT = "ping"


@dataclass
class Outcome:
    result: VerificationResult
    message: str


def classify_provider_exception(exc: Exception) -> Outcome:
    """Turns an exception raised by provider.chat()/provider.embed() into a safe, fixed
    (result, message) pair. NEVER returns str(exc) for an httpx-originated exception — that
    can contain the full request URL, and a provider's auth could in principle put a secret
    there (app/providers/gemini_provider.py used to — a real 2026-07-26 incident leaked a live
    key into the Docker log this way before it moved to header-based auth; kept safe here as
    defense in depth regardless of which provider's URL shape changes in the future). Only an
    explicit 401/403 is classified `invalid_key`; every other failure (timeout, connection
    error, 5xx, 429, an unrecognized exception type) is `unreachable`/`rate_limited` — a
    transient problem must never be reported as a definitively bad key. Shared by
    verify_provider() (the real pre-flight check), app/providers/registry.py's
    chat_with_fallback() (every per-attempt log line, not just the final raised error), and
    app/rag/ingest.py's post-preflight embedding catch-all (a call that passed verification
    but still failed for some new reason)."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return Outcome(VerificationResult.invalid_key, f"Leverantören avvisade nyckeln (HTTP {status}).")
        if status == 429:
            return Outcome(VerificationResult.rate_limited, "Leverantören svarar med för många förfrågningar just nu (HTTP 429).")
        return Outcome(VerificationResult.unreachable, f"Leverantören svarade med ett fel (HTTP {status}).")
    if isinstance(exc, httpx.TimeoutException):
        return Outcome(VerificationResult.unreachable, "Tidsgränsen överskreds vid anropet till leverantören.")
    if isinstance(exc, httpx.HTTPError):
        return Outcome(VerificationResult.unreachable, "Leverantören kunde inte nås (nätverksfel).")
    if isinstance(exc, ProviderError):
        text = str(exc)
        if "erbjuder inget embedding" in text or "fokuserar på chattmodeller" in text:
            return Outcome(VerificationResult.unsupported, "Leverantören stödjer inte den här rollen.")
        return Outcome(VerificationResult.unreachable, "Leverantören kunde inte nås.")
    # Any other, unrecognized exception type — never echo str(exc) here either. It is
    # deliberately impossible to reach this branch with a leaked key: every code path that
    # can carry request details (httpx.*) is already classified above.
    return Outcome(VerificationResult.unreachable, "Ett okänt fel inträffade vid anropet till leverantören.")


async def verify_provider(provider: LLMProvider, model: str, role: str, *, timeout: float) -> Outcome:
    """The real check — a genuine, minimal provider.chat()/provider.embed() call, never just
    a key-presence check. Chat and embedding are verified through their own real methods, so
    a provider that's fine for chat but doesn't support embeddings (Anthropic, DeepSeek,
    OpenRouter) is correctly classified `unsupported` for the embedding role rather than
    silently treated as verified."""
    if not provider.is_configured():
        return Outcome(VerificationResult.not_configured, "Ingen API-nyckel är konfigurerad.")
    try:
        if role == "chat":
            await provider.chat([Message(role="user", content=VERIFICATION_PROBE_TEXT)], model=model, timeout=timeout, max_tokens=1)
        else:
            await provider.embed([VERIFICATION_PROBE_TEXT], model=model, timeout=timeout)
        return Outcome(VerificationResult.ok, "Verifierad.")
    except Exception as exc:  # noqa: BLE001 - every failure mode must be classified, never re-raised
        return classify_provider_exception(exc)


def _default_verification_model(provider: LLMProvider, role: str) -> str:
    from app.providers.registry import DEFAULT_CHAT_MODELS

    settings = get_settings()
    if role == "chat":
        return DEFAULT_CHAT_MODELS.get(provider.name, settings.default_llm_model)
    if provider.name == settings.default_embedding_provider:
        return settings.default_embedding_model
    # A non-default provider being verified for the embedding role (e.g. the founder is
    # about to switch to it) has no configured model of its own to fall back on — Gemini and
    # Ollama both accept a plain model name here; verification against an unsupported
    # provider (Anthropic/DeepSeek/OpenRouter) never reaches a real call at all, see
    # classify_provider_exception's "unsupported" branch.
    return {"gemini": "text-embedding-004", "openai": "text-embedding-3-small", "ollama": "nomic-embed-text"}.get(
        provider.name, settings.default_embedding_model
    )


async def ensure_verified(db: Session, role: str, *, checked_by: str = "system") -> Outcome:
    """Resolves the currently active provider+model for `role` and returns whether it's
    usable right now — reusing a cached result within PROVIDER_VERIFICATION_CACHE_SECONDS
    instead of making a real call on every single file/poll cycle (which would hammer a
    provider that's genuinely down, once per file in a large batch). A stale or missing
    check always triggers one real, timeout-bounded call and persists a new row."""
    settings = get_settings()
    provider, model = resolve_active(db, role=role)

    cutoff = datetime.utcnow() - timedelta(seconds=settings.provider_verification_cache_seconds)
    cached = (
        db.query(ProviderVerificationCheck)
        .filter(
            ProviderVerificationCheck.provider_name == provider.name,
            ProviderVerificationCheck.role == role,
            ProviderVerificationCheck.checked_at >= cutoff,
        )
        .order_by(ProviderVerificationCheck.checked_at.desc())
        .first()
    )
    if cached is not None:
        return Outcome(cached.result, cached.message)

    outcome = await verify_provider(provider, model, role, timeout=settings.provider_verification_timeout_seconds)
    db.add(
        ProviderVerificationCheck(
            id=uuid.uuid4(),
            provider_name=provider.name,
            role=role,
            model=model,
            result=outcome.result,
            message=outcome.message,
            checked_by=checked_by,
            checked_at=datetime.utcnow(),
        )
    )
    db.commit()
    return outcome


async def verify_now(db: Session, provider_name: str, role: str, *, checked_by: str) -> Outcome:
    """Admin -> Providers' "Testa nu" action (POST /api/admin/providers/verify) — an
    explicit, founder-initiated check that ALWAYS makes a real call, bypassing the cache, for
    a specific provider (not necessarily the currently active one, so a founder can verify a
    provider before switching to it)."""
    from app.providers.registry import get_provider

    settings = get_settings()
    provider = get_provider(provider_name)
    model = _default_verification_model(provider, role)
    outcome = await verify_provider(provider, model, role, timeout=settings.provider_verification_timeout_seconds)
    db.add(
        ProviderVerificationCheck(
            id=uuid.uuid4(),
            provider_name=provider.name,
            role=role,
            model=model,
            result=outcome.result,
            message=outcome.message,
            checked_by=checked_by,
            checked_at=datetime.utcnow(),
        )
    )
    db.commit()
    return outcome


def latest_check(db: Session, provider_name: str, role: str) -> ProviderVerificationCheck | None:
    return (
        db.query(ProviderVerificationCheck)
        .filter(ProviderVerificationCheck.provider_name == provider_name, ProviderVerificationCheck.role == role)
        .order_by(ProviderVerificationCheck.checked_at.desc())
        .first()
    )
