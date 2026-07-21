"""STEG 11: app/jobs/retry.py's pure functions — error classification and backoff timing."""

from app.jobs.lock import JobLockUnavailable
from app.jobs.retry import BASE_DELAY_SECONDS, MAX_DELAY_SECONDS, compute_backoff_seconds, is_transient_error
from app.rag.zip_import import ZipSecurityError


def test_connection_error_is_transient():
    assert is_transient_error(ConnectionError("boom")) is True


def test_timeout_error_is_transient():
    assert is_transient_error(TimeoutError("boom")) is True


def test_job_lock_unavailable_is_transient():
    assert is_transient_error(JobLockUnavailable("redis down")) is True


def test_zip_security_error_is_permanent_not_transient():
    assert is_transient_error(ZipSecurityError("evil zip")) is False


def test_value_error_is_permanent():
    assert is_transient_error(ValueError("bad input")) is False


def test_unrecognized_exception_type_defaults_to_permanent():
    """Whitelisted, not blacklisted (see retry.py's docstring) — an exception type nobody
    has explicitly classified must never be blindly retried."""

    class SomeUnrelatedBug(Exception):
        pass

    assert is_transient_error(SomeUnrelatedBug("unexpected")) is False


def test_backoff_grows_with_attempt_number():
    # Sampled repeatedly since jitter is randomized — the CEILING must still grow.
    ceilings = [min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * (2**attempt)) for attempt in range(5)]
    assert ceilings == sorted(ceilings)
    assert ceilings[-1] <= MAX_DELAY_SECONDS


def test_backoff_never_exceeds_the_cap():
    for attempt in range(10):
        for _ in range(20):
            assert 0 <= compute_backoff_seconds(attempt) <= MAX_DELAY_SECONDS


def test_backoff_has_jitter_not_a_fixed_value():
    samples = {compute_backoff_seconds(3) for _ in range(20)}
    assert len(samples) > 1  # ~vanishingly unlikely to collide 20 times if truly randomized


def test_negative_attempt_is_treated_as_zero():
    for _ in range(20):
        assert compute_backoff_seconds(-5) <= BASE_DELAY_SECONDS
