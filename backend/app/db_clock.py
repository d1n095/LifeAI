"""One clock domain per comparison.

`mainai_jobs.lease_expires_at`, `import_jobs.lease_expires_at` and
`agent_coordination`'s equivalent are authored ONLY by Postgres —
`now() + make_interval(secs => :lease_seconds)` in `app/jobs/mainai_job_lease.py` and
`app/jobs/lease.py`. Comparing such a value against Python's `datetime.utcnow()` mixes two
clocks: the app container's and the database server's. On a single host they agree to within
milliseconds. On the real deployment (backend container on the Strato VPS, database on its own
service; container clock stepped by NTP, or a restored snapshot booting with a stale clock)
they can differ by far more than one lease window.

Both directions are defects, never noise:

  - **app clock AHEAD** — a live lease looks expired. `_advance_mainai_execution_auto_recovery`
    then declares a genuinely running privileged attempt dead and starts a takeover of it:
    duplicate execution of the exact work the lease exists to prevent duplicating.
  - **app clock BEHIND** — an expired lease looks live. A crashed attempt is never recovered
    and its task stays `running` forever, with `TaskLiveness.running` reported the whole time.

The rule, therefore: a value authored by Postgres is compared against Postgres.

Two tools, matching the two shapes of comparison:

  - `db_now_expr()` — the SQL expression, for filters the database evaluates itself. Preferred:
    skew-free by construction and costs no extra round trip.
  - `db_now(db)` — one round trip, the database's own current time, for code that already holds
    fetched rows and must classify them in Python.

Both use `clock_timestamp()`, not `now()`: `now()` is transaction start time, and a recovery
scan that runs inside a long transaction must still see real elapsed time.

Timestamps this application authors itself (`next_retry_at`, `next_poll_at`, `completed_at`
written from `datetime.utcnow()`) stay in the Python domain and must keep being compared in the
Python domain. Mixing is the defect; the fix is consistency within each pair, not converting
everything.
"""

from datetime import datetime

from sqlalchemy import func, text
from sqlalchemy.orm import Session


def db_now_expr():
    """The database's current time, as a SQL expression for use inside a query or filter."""
    return func.clock_timestamp()


def db_now(db: Session) -> datetime:
    """The database's current time, fetched once for comparisons made in Python."""
    return db.execute(text("SELECT clock_timestamp()")).scalar_one()
