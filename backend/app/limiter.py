import ipaddress
import logging
import re

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import get_settings

logger = logging.getLogger("mainai.limiter")
settings = get_settings()

_TRUSTED_PROXY_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_SCOPE_RE = re.compile(r"[^a-z0-9._-]+")


def _peer_host(request: Request) -> str:
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client is not None else None
    return str(host or "")


def _plausible_ip(value: str) -> bool:
    text = (value or "").strip()
    if not text or text.lower() in {"unknown", "undefined"}:
        return False
    if any(ch.isspace() or ch in {",", ";", "\0"} for ch in text):
        return False
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def trusted_client_ip(request: Request) -> str:
    """Client identity for rate limits.

    Client-supplied X-Forwarded-For is ignored unless the immediate peer is the
    same-origin proxy (PX-9). Authenticated Founder traffic is still split by
    endpoint class so one surface cannot lock the others (PX-10/PX-11).
    """
    peer = _peer_host(request)
    if peer in _TRUSTED_PROXY_HOSTS:
        custom = request.headers.get("x-mainai-client-ip")
        if custom and _plausible_ip(custom):
            return custom.strip()
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            first = forwarded.split(",", 1)[0].strip()
            if _plausible_ip(first):
                return first
    return peer or get_remote_address(request)


def rate_limit_scope(request: Request) -> str:
    path = str(getattr(getattr(request, "url", None), "path", "") or "")
    parts = [part.lower() for part in path.split("/") if part]
    if parts[:1] == ["api"] and len(parts) >= 2:
        if parts[1] == "auth" and len(parts) >= 3:
            raw = f"auth.{parts[2]}"
        else:
            raw = parts[1]
    else:
        raw = parts[0] if parts else "unknown"
    return _SCOPE_RE.sub("", raw) or "unknown"


def rate_limit_key(request: Request) -> str:
    """Key by user+surface when authenticated, else by trusted client IP+surface."""
    scope = rate_limit_scope(request)
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        return f"user:{user_id}:{scope}"
    return f"ip:{trusted_client_ip(request)}:{scope}"


def _storage_uri() -> str | None:
    """Redis-backed storage is required for correctness with more than one backend
    replica: slowapi's default in-memory storage is process-local, so each replica would
    enforce the limit independently (an attacker split across N replicas gets N times the
    allowance) and every counter resets on restart or deploy. redis_url unset falls back to
    in-memory, which is fine for exactly one instance (local dev, this repo's E2E test
    harness) but must not be the case in production — see docs/OPERATIONS.md."""
    if not settings.redis_url:
        if settings.environment == "production":
            logger.warning(
                "REDIS_URL är inte satt i en produktionsmiljö — rate limiting körs "
                "process-lokalt (in-memory) och ger INTE ett delat skydd över flera "
                "backend-instanser. Sätt REDIS_URL (se .env.example)."
            )
        return None
    return settings.redis_url


limiter = Limiter(key_func=rate_limit_key, storage_uri=_storage_uri())
