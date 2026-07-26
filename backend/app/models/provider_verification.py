import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class VerificationResult(str, enum.Enum):
    """The classified outcome of one real, minimal API call against a provider (see
    app/providers/verification.py) — never a guess from `is_configured()` alone.

    `invalid_key` is reserved for an explicit 401/403 from the provider — every other
    failure mode (timeout, connection error, 5xx, 429) is `unreachable`/`rate_limited`, since
    a transient network problem must never be reported as a definitively bad key (see
    docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §0/§4.7). `unsupported` covers a structurally
    wrong provider/role pairing (e.g. Anthropic chosen for the embedding role) — a
    configuration mistake, not a network or auth problem, but still surfaced through the same
    resumable path since fixing the *configuration* is what unblocks it. `not_configured`
    means no key was even present — the ONE case verify_provider() never makes a network call
    for at all."""

    ok = "ok"
    invalid_key = "invalid_key"
    unreachable = "unreachable"
    rate_limited = "rate_limited"
    unsupported = "unsupported"
    not_configured = "not_configured"


class ProviderVerificationCheck(Base):
    """One row per real verification attempt against a provider+role — the record both
    Admin -> Providers (GET /api/admin/providers/status) and the worker's automatic requeue
    (app/worker.py's _requeue_blocked_jobs) read to answer "is this usable right now" without
    re-verifying on every single call. Not RLS-protected — see this migration's docstring
    (0013): provider verification is founder-wide configuration state, like provider_config,
    not per-owner data.

    `message` is ALWAYS a fixed, classified template string (see
    app/providers/verification.py) — NEVER the raw exception text. A provider's error
    response, or even just `str(exc)` on an httpx.HTTPStatusError, can contain the full
    request URL — and app/providers/gemini_provider.py puts the API key directly in the URL
    query string. Storing anything other than a pre-classified message here would risk
    persisting the key itself in a queryable table."""

    __tablename__ = "provider_verification_checks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_name: Mapped[str] = mapped_column(String(32), index=True)
    role: Mapped[str] = mapped_column(String(16), index=True)  # "chat" | "embedding"
    model: Mapped[str] = mapped_column(String(128))
    result: Mapped[VerificationResult] = mapped_column(Enum(VerificationResult))
    message: Mapped[str] = mapped_column(String(256))
    checked_by: Mapped[str] = mapped_column(String(16))  # "system" | "founder"
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
