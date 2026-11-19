import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.jobs.heartbeat import worker_process_alive
from app.redis_client import get_redis_client

logger = logging.getLogger("mainai.health")
settings = get_settings()

router = APIRouter(prefix="/api/health", tags=["health"])

# Providers that run ON THIS MACHINE. Their availability is a local operational fact (a model
# that was never pulled, a runtime that was restarted), which a founder otherwise only learns
# from a failing chat with a generic message. Reported explicitly instead — see
# _check_local_model().
LOCAL_RUNTIME_PROVIDERS = frozenset({"ollama"})

# /api/health must answer fast even when the local runtime is wedged, so the model probe gets a
# short, separate budget rather than any inference timeout.
_LOCAL_MODEL_PROBE_TIMEOUT_SECONDS = 2.0


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

    try:
        # The shared process-wide client (app/redis_client.py): the health endpoint runs
        # on every monitoring poll, and the old per-request from_url()+close() churned
        # one TCP connection per check — the same pattern app/jobs/heartbeat.py's fix
        # (2026-11-18) removed for the worker loop.
        get_redis_client().ping()
        return True
    except Exception:
        logger.exception("Health check: Redis svarade inte.")
        return False


def _fetch_local_model_names(base_url: str) -> set[str]:
    """The model names the local runtime actually has installed (GET /api/tags).

    Raises on any transport/status/shape problem; the caller turns that into
    `reachable: false`. Never called from outside this module's guarded check.
    """
    resp = httpx.get(f"{base_url}/api/tags", timeout=_LOCAL_MODEL_PROBE_TIMEOUT_SECONDS)
    resp.raise_for_status()
    data = resp.json()
    entries = data.get("models") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError("oväntat svar från /api/tags")
    names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for key in ("name", "model"):
            value = entry.get(key)
            if isinstance(value, str) and value:
                names.add(value)
    return names


def _is_installed(names: set[str], configured: str) -> bool:
    """Whether a configured model name is installed, tolerating Ollama's tag shorthand.

    Ollama lists `llama3.1:latest` for a model configured as `llama3.1` (and vice versa), so an
    exact string compare would report a false `missing_models` entry for the most common case.
    """
    if configured in names:
        return True
    if f"{configured}:latest" in names:
        return True
    return configured.endswith(":latest") and configured[: -len(":latest")] in names


def _check_local_model(db: Session) -> dict[str, Any] | None:
    """Readiness of the local model runtime for the models this instance is configured to use.

    Returns None when no role resolves to a local provider, so a cloud-only deployment's
    /api/health payload is exactly what it was before this field existed.

    `reachable` is tri-state on purpose: True (probe succeeded), False (probe failed), None
    (no model is configured, so nothing could be probed — never a guess in either direction).
    """
    from app.providers.registry import resolve_active

    configured: list[str] = []
    runtime: str | None = None
    for role in ("chat", "embedding"):
        try:
            provider, model = resolve_active(db, role=role)
        except Exception:
            # A role that cannot even be resolved is a real problem, but not this check's to
            # classify — log it and report only what this probe can actually attest to.
            logger.warning("Health check: kunde inte lösa aktiv leverantör för rollen %s.", role, exc_info=True)
            continue
        if provider.name not in LOCAL_RUNTIME_PROVIDERS:
            continue
        runtime = provider.name
        if model and model not in configured:
            configured.append(model)

    if runtime is None:
        return None
    if not configured:
        return {"runtime": runtime, "reachable": None, "configured_models": []}
    try:
        names = _fetch_local_model_names(settings.ollama_base_url)
    except Exception:
        logger.warning("Health check: lokal modellruntime svarade inte.", exc_info=True)
        # `missing_models` is deliberately OMITTED rather than filled with `configured_models`:
        # nothing was checked, so "these are missing" would be a guess dressed up as a fact.
        # Absent means unknown here, exactly like this endpoint's `worker: "unknown"`.
        return {"runtime": runtime, "reachable": False, "configured_models": configured}
    return {
        "runtime": runtime,
        "reachable": True,
        "configured_models": configured,
        "missing_models": [model for model in configured if not _is_installed(names, model)],
    }


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

    When any role resolves to a LOCAL provider (Ollama), additionally reports `local_model`:
    `runtime`, tri-state `reachable`, the `configured_models` this instance will ask for, and
    `missing_models` (only when the runtime answered). This is what makes "the API is up but
    the model was never pulled / the runtime was restarted" visible without opening a chat and
    reading a provider error. A local runtime that is merely absent never 503s a live API —
    same principle as `worker`.
    """
    healthy = _check_database(db) and _check_redis()
    if not healthy:
        response.status_code = 503
        return {"status": "unavailable"}
    payload: dict[str, Any] = {
        "status": "ok",
        "worker": "alive" if worker_process_alive() is True else "unknown",
    }
    local_model = _check_local_model(db)
    if local_model is not None:
        payload["local_model"] = local_model
    return payload
