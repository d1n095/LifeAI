"""2026-07-28 incident: app/jobs/heartbeat.py's process-level worker heartbeat. See that
module's docstring for why ImportJob.last_heartbeat_at alone made an idle-but-healthy worker
indistinguishable from a dead one — this signal is written on every poll cycle regardless of
whether a job was claimed, and can only ever assert "definitely alive" or "don't know", never
"definitely dead" on its own (see worker_process_alive's own docstring for why)."""

import redis
import pytest

from app.config import get_settings
from app.jobs.heartbeat import record_worker_heartbeat, worker_process_alive


@pytest.fixture(autouse=True)
def _clean_heartbeat_key():
    """The heartbeat key lives outside any per-test DB transaction (it's Redis, not
    Postgres) — clean up before and after so tests never see another test's leftover key."""
    client = redis.from_url(get_settings().redis_url)
    client.delete("worker:heartbeat")
    yield
    client.delete("worker:heartbeat")


def test_worker_process_alive_is_none_when_no_heartbeat_has_ever_been_recorded():
    # The exact scenario this module must not regress: a test environment (or a fresh deploy
    # before the worker's first poll cycle) where no real worker process has ever run —
    # callers must fall back to their other signal, never treat this as "dead".
    assert worker_process_alive() is None


def test_worker_process_alive_is_true_immediately_after_a_recorded_heartbeat():
    record_worker_heartbeat("test-worker-1", ttl_seconds=60)
    assert worker_process_alive() is True


def test_worker_process_alive_reverts_to_none_once_the_ttl_expires():
    record_worker_heartbeat("test-worker-1", ttl_seconds=60)
    assert worker_process_alive() is True

    # Simulate expiry without sleeping in a test — set the TTL to effectively already past.
    client = redis.from_url(get_settings().redis_url)
    client.expire("worker:heartbeat", -1)

    assert worker_process_alive() is None


def test_record_worker_heartbeat_never_raises_when_redis_is_unconfigured(monkeypatch):
    monkeypatch.setattr(get_settings(), "redis_url", None)
    record_worker_heartbeat("test-worker-1", ttl_seconds=60)  # must not raise
    assert worker_process_alive() is None
