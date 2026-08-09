"""STEG 11: app/jobs/lock.py's distributed lock primitive, against a REAL local Redis (not
mocked — the whole point is verifying genuine Redis atomicity semantics: NX/EX, and the
Lua-script compare-and-swap for renew/release)."""

import time
import uuid

import pytest

from app.jobs.lock import JobLock, JobLockUnavailable


def _unique_key() -> str:
    return f"test-{uuid.uuid4()}"


def test_acquire_succeeds_when_nobody_holds_the_lock():
    lock = JobLock(_unique_key(), lease_seconds=5)
    assert lock.acquire() is True
    lock.release()


def test_second_acquire_by_a_different_holder_fails_while_the_first_holds_it():
    """The core "two concurrent workers" scenario STEG 11 explicitly asks to be tested:
    two independent JobLock instances (simulating two workers/processes) racing for the
    SAME key — only one may ever hold it at a time."""
    key = _unique_key()
    worker_a = JobLock(key, lease_seconds=5)
    worker_b = JobLock(key, lease_seconds=5)

    assert worker_a.acquire() is True
    assert worker_b.acquire() is False  # worker B must not also succeed

    worker_a.release()


def test_release_lets_a_new_holder_acquire_immediately():
    key = _unique_key()
    worker_a = JobLock(key, lease_seconds=30)
    worker_b = JobLock(key, lease_seconds=5)

    assert worker_a.acquire() is True
    worker_a.release()
    assert worker_b.acquire() is True  # no need to wait for the (much longer) original lease

    worker_b.release()


def test_a_holder_cannot_release_another_holders_lock():
    """Defense against the classic distributed-lock bug: worker A's lease expires, worker B
    acquires the now-free lock, and worker A's delayed release() call must NOT delete B's
    active lock out from under it — the owner-token compare-and-swap (app/jobs/lock.py's Lua
    script) is what prevents this."""
    key = _unique_key()
    worker_a = JobLock(key, lease_seconds=1)
    assert worker_a.acquire() is True
    time.sleep(1.2)  # let A's lease genuinely expire

    worker_b = JobLock(key, lease_seconds=30)
    assert worker_b.acquire() is True  # B legitimately took over the abandoned lock

    worker_a.release()  # A's stale release must be a no-op, not delete B's lock

    worker_c = JobLock(key, lease_seconds=5)
    assert worker_c.acquire() is False  # B must still hold it

    worker_b.release()


def test_abandoned_lock_can_be_reacquired_after_its_lease_expires():
    """"Säkert övertagande av övergivna jobb" — an abandoned lock (holder crashed, never
    called release()) must free itself once its lease elapses, without any manual
    intervention. This is what makes the lock never-permanent, per app/jobs/lock.py's
    docstring."""
    key = _unique_key()
    abandoned = JobLock(key, lease_seconds=1)
    assert abandoned.acquire() is True
    # Deliberately never released — simulates a crashed worker.
    time.sleep(1.2)

    rescuer = JobLock(key, lease_seconds=5)
    assert rescuer.acquire() is True
    rescuer.release()


def test_renew_extends_the_lease_for_the_same_holder():
    key = _unique_key()
    holder = JobLock(key, lease_seconds=1)
    assert holder.acquire() is True

    time.sleep(0.7)  # most of the original 1s lease has elapsed, but not expired yet
    assert holder.renew() is True  # heartbeat resets the lease to another 1s from THIS point

    time.sleep(0.6)  # total elapsed since acquire() is now 1.3s — past the ORIGINAL lease,
    # but well within the renewed one (renew() reset the clock at the 0.7s mark)
    competitor = JobLock(key, lease_seconds=5)
    assert competitor.acquire() is False  # renewal genuinely extended it past the original TTL

    holder.release()


def test_renew_fails_once_another_holder_has_taken_over():
    key = _unique_key()
    original = JobLock(key, lease_seconds=1)
    assert original.acquire() is True
    time.sleep(1.2)

    new_holder = JobLock(key, lease_seconds=5)
    assert new_holder.acquire() is True

    assert original.renew() is False  # original's token no longer matches what's stored
    new_holder.release()


def test_job_lock_unavailable_when_redis_url_is_not_configured(monkeypatch):
    class _FakeSettings:
        redis_url = None

    monkeypatch.setattr("app.jobs.lock.get_settings", lambda: _FakeSettings())
    lock = JobLock(_unique_key())
    with pytest.raises(JobLockUnavailable):
        lock.acquire()
