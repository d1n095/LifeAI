import logging

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import get_settings

logger = logging.getLogger("mainai.limiter")
settings = get_settings()


def rate_limit_key(request: Request) -> str:
    """Key by authenticated user when available (set in app/deps.py), else by IP."""
    user_id = getattr(request.state, "user_id", None)
    return str(user_id) if user_id else get_remote_address(request)


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
