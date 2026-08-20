import logging

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.jobs.heartbeat import worker_process_alive

logger = logging.getLogger("mainai.health")
settings = get_settings()

router = APIRouter(prefix="/api/health", tags=["health"])


def _check_database(db: Session) -> bool:
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception:
        # Full exception (connection string details, driver error text) stays in the
        # server-side log only — see health()'s docstring for why that boundary matters.
        logger.exception("Health check: databasen svarade inte.")
        return False


def _check_redis() -> bool:
    if not settings.redis_url:
        # No Redis configured is a valid deployment mode (single instance, in-memory rate
        # limiting — see app/limiter.py), not a failure to report as unhealthy.
        return True
    import redis

    try:
        client = redis.from_url(settings.redis_url, socket_connect_timeout=3, socket_timeout=3)
        try:
            client.ping()
        finally:
            client.close()
        return True
    except Exception:
        logger.exception("Health check: Redis svarade inte.")
        return False


@router.get("")
def health(response: Response, db: Session = Depends(get_db)):
    """Genuinely checks dependencies (database, Redis if configured) instead of returning a
    static "ok" regardless of actual state. Consumed through
    frontend/app/api/[...path]/route.ts's same-origin proxy — this is what makes checking
    /api/health from the browser actually verify the whole chain, not just that Next.js can
    reach FastAPI.

    Never leaks *why* something failed: connection details and the exact exception are
    logged server-side only (see _check_database/_check_redis above) — a 503 body stays
    `{"status": "unavailable"}` with no extra fields.

    When healthy, also reports `worker`: `alive` if the process-level Redis heartbeat
    (app/jobs/heartbeat.py) is present, else `unknown`. Missing worker never 503s this
    endpoint — an idle API without a worker is still a live API; founder ops_status remains
    the place that classifies unreachable workers. `unknown` is the only negative this
    public probe is allowed to say (the heartbeat helper never returns False).
    """
    healthy = _check_database(db) and _check_redis()
    if not healthy:
        response.status_code = 503
        return {"status": "unavailable"}
    worker = "alive" if worker_process_alive() is True else "unknown"
    return {"status": "ok", "worker": worker}
