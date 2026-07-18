import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from app.cleanup import run_token_cleanup
from app.config import get_settings
from app.db import SessionLocal

logger = logging.getLogger("mainai.scheduler")
settings = get_settings()

_scheduler: BackgroundScheduler | None = None


def _run_cleanup_job() -> None:
    db = SessionLocal()
    try:
        run_token_cleanup(db)
    except Exception:
        logger.exception("Schemalagt städjobb misslyckades.")
    finally:
        db.close()


def start_scheduler() -> None:
    """Runs the token-retention cleanup (app/cleanup.py) on a fixed interval, in-process.
    Safe to run on every backend replica: app/cleanup.py's Postgres advisory lock ensures
    only one replica's firing actually does the work per interval, so this doesn't need its
    own distributed-coordination layer."""
    global _scheduler
    if not settings.enable_scheduled_cleanup:
        logger.info("Schemalagt städjobb avstängt (ENABLE_SCHEDULED_CLEANUP=false).")
        return
    if _scheduler is not None:
        return

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        _run_cleanup_job,
        "interval",
        hours=settings.cleanup_interval_hours,
        id="token_cleanup",
        # Fire once shortly after startup too, not just after the first full interval —
        # otherwise a short-lived environment (a test run, a fresh deploy that gets
        # replaced before the interval elapses) might never see a single cleanup pass.
        next_run_time=datetime.now(timezone.utc),
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("Schemalagt städjobb aktivt, var %s:e timme.", settings.cleanup_interval_hours)


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
