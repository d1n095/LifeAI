"""STEG 11: retry classification and exponential backoff with jitter for Founder Knowledge
Studio import jobs. Pure functions — no I/O, no Redis, no DB — so the policy itself is
trivially unit-testable independent of app/rag/library_import.py's orchestration.
"""

import random

from app.jobs.lock import JobLockUnavailable
from app.rag.zip_import import ZipSecurityError

DEFAULT_MAX_ATTEMPTS = 3
BASE_DELAY_SECONDS = 2.0
MAX_DELAY_SECONDS = 30.0

# Whitelisted, not blacklisted: only exception types explicitly known to be transient are
# retried. An unrecognized exception type defaults to permanent (see is_transient_error) —
# retrying an unknown failure blindly risks looping on a real bug forever; refusing to retry
# an unrecognized-but-actually-transient error just costs one extra manual re-import, a far
# smaller cost.
_TRANSIENT_EXCEPTION_TYPES = (ConnectionError, TimeoutError, OSError, JobLockUnavailable)

# Explicitly permanent even though some of these subclass a builtin that might otherwise
# look transient — a malformed/malicious ZIP will never succeed no matter how many times
# it's retried.
_PERMANENT_EXCEPTION_TYPES = (ZipSecurityError, ValueError)


def is_transient_error(exc: Exception) -> bool:
    """Whether `exc` represents a transient failure worth retrying (a dropped connection, a
    momentarily unreachable Redis) as opposed to a permanent one (malformed input, a
    programming error) that retrying can never fix."""
    if isinstance(exc, _PERMANENT_EXCEPTION_TYPES):
        return False
    return isinstance(exc, _TRANSIENT_EXCEPTION_TYPES)


def compute_backoff_seconds(attempt: int, *, base: float = BASE_DELAY_SECONDS, cap: float = MAX_DELAY_SECONDS) -> float:
    """Exponential backoff with full jitter (AWS's recommended jitter strategy — a random
    delay in [0, min(cap, base * 2^attempt)], not a fixed multiplier plus a small random
    offset) so that many jobs failing at the same moment (e.g. a brief Redis blip) don't all
    retry in lockstep and immediately re-collide."""
    if attempt < 0:
        attempt = 0
    ceiling = min(cap, base * (2**attempt))
    return random.uniform(0, ceiling)
