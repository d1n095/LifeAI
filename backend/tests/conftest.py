"""Shared pytest fixtures for the backend test suite.

Env vars are set BEFORE any `app.*` import — app.config.get_settings() is `@lru_cache`d, so
whatever is in os.environ the first time it's called wins for the rest of the process. CI
(and local runs) can still override any of these by exporting them before invoking pytest;
os.environ.setdefault() only fills in what isn't already set.

Local multi-worktree safety: when DATABASE_URL is not preset, the default database name is
`lifeos_test_<pid>` so two agents/worktrees running pytest concurrently against the same local
Postgres no longer fight over DROP DATABASE on a shared `lifeos_test` (ObjectInUse). CI
continues to set DATABASE_URL explicitly to the fixed `lifeos_test` name in each job container.
Override locally with LIFEAI_TEST_DATABASE_NAME when a stable name is preferred.
"""

import os
import re
import tempfile

_test_db_name = os.environ.get("LIFEAI_TEST_DATABASE_NAME", f"lifeos_test_{os.getpid()}")
_redis_db = int(os.environ.get("LIFEAI_TEST_REDIS_DB", str(1 + (os.getpid() % 14))))

os.environ.setdefault("DATABASE_URL", f"postgresql://lifeos@localhost:5433/{_test_db_name}")
os.environ.setdefault("APP_DATABASE_URL", f"postgresql://mainai_app:mainai_app_pw@localhost:5433/{_test_db_name}")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest")
os.environ.setdefault("FOUNDER_EMAIL", "founder@lifeos.local")
os.environ.setdefault("FOUNDER_PASSWORD", "TestFounderPassword123!")
os.environ.setdefault("FRONTEND_ORIGINS", "http://127.0.0.1:3020")
os.environ.setdefault("PUBLIC_APP_URL", "http://127.0.0.1:3020")
os.environ.setdefault("ENABLE_SCHEDULED_CLEANUP", "false")  # tests trigger cleanup explicitly
os.environ.setdefault("REDIS_URL", f"redis://localhost:6379/{_redis_db}")  # isolated per pytest process; dev uses DB 0
os.environ.setdefault("OPENAI_API_KEY", "fake-key-for-tests")
# Life Library durable-worker package: a disposable directory outside the repo, unique per
# test-process invocation (tempfile.mkdtemp, not a fixed path) so parallel/rerun test
# sessions never share or collide over stored blobs — see app/storage/local_fs.py.
os.environ.setdefault("STORAGE_ROOT", tempfile.mkdtemp(prefix="lifeai-test-storage-"))
os.environ.setdefault("WORKER_LEASE_SECONDS", "5")  # short lease so reclaim-after-expiry tests stay fast

import subprocess  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402
from urllib.parse import urlparse  # noqa: E402

import psycopg2  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _server_dsn(database_url: str, dbname: str = "postgres") -> str:
    """Same host/port/user as the test DB URL, but pointed at the `postgres` maintenance
    database — needed to CREATE/DROP the test database itself, which obviously can't be
    done from a connection that's already inside it."""
    parsed = urlparse(database_url)
    auth = parsed.username if not parsed.password else f"{parsed.username}:{parsed.password}"
    return f"postgresql://{auth}@{parsed.hostname}:{parsed.port or 5432}/{dbname}"


def _force_drop_database(cur, db_name: str) -> None:
    """Terminate other backends on `db_name`, then DROP DATABASE IF EXISTS.

    Session setup used to DROP without this, so a leftover connection from a crashed
    pytest (or a second process on a shared LIFEAI_TEST_DATABASE_NAME) raised ObjectInUse
    before the suite could even start. Teardown already terminated; setup must too.
    """
    if not re.fullmatch(r"[A-Za-z0-9_]+", db_name):
        raise ValueError(f"refusing to drop a non-identifier database name: {db_name!r}")
    cur.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
        (db_name,),
    )
    cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    """Creates a dedicated, disposable test database, applies every Alembic migration
    against it (never Base.metadata.create_all — the whole point of this test run is to
    exercise the same migration path production uses), grants the restricted runtime role
    the same privileges backend/db-init/01-app-role.sh gives it in Docker (idempotent — a
    fresh CI Postgres service container won't have run that script), applies the same
    RLS/privilege-narrowing steps app/main.py's own startup event applies in production, and
    drops the database again at the end of the session.

    The RLS/privilege step (apply_rls / apply_mainai_job_runtime_privileges /
    apply_mainai_execution_privileges) used to be missing here entirely — this fixture only
    ever ran `alembic upgrade head`, and every EXECUTE grant those three functions apply
    (e.g. erase_own_agent_coordination_children, migration 0046/0047) was left to happen as an
    ACCIDENTAL side effect of whichever test happened to instantiate the FastAPI `client`
    fixture first in the session (app/main.py's `@app.on_event("startup")` calls all three).
    That made any test using a raw SessionLocal() instead of `client` — and running early
    enough in a session/subset that no `client`-using test had gone first — silently hit
    `permission denied for function ...` for a privilege that was never actually missing in
    production, only in this fixture. Real, reproduced case: running
    `tests/backend/rag/test_bootstrap_hardening.py` (which never uses `client`) standalone, or
    as the first file in a narrower selection, failed
    test_section3_account_erasure_hard_deletes_documents_entirely with exactly this error.
    Applying the same three functions here, in the same order production's boot sequence
    does, right after migrating and before any test runs, makes every test's starting
    privilege state identical regardless of what else did or didn't run first."""
    from app.config import get_settings

    settings = get_settings()
    db_name = urlparse(settings.database_url).path.lstrip("/")
    app_role = urlparse(settings.app_database_url).username
    app_role_password = urlparse(settings.app_database_url).password or "mainai_app_pw"

    admin_conn = psycopg2.connect(_server_dsn(settings.database_url))
    admin_conn.autocommit = True
    with admin_conn.cursor() as cur:
        _force_drop_database(cur, db_name)
        cur.execute(f'CREATE DATABASE "{db_name}"')
        cur.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s",
            (app_role,),
        )
        if cur.fetchone() is None:
            cur.execute(f"CREATE ROLE {app_role} LOGIN PASSWORD %s", (app_role_password,))
    admin_conn.close()

    db_conn = psycopg2.connect(settings.database_url)
    db_conn.autocommit = True
    with db_conn.cursor() as cur:
        cur.execute(f"GRANT USAGE ON SCHEMA public TO {app_role}")
        # SELECT, INSERT, UPDATE, DELETE — matching backend/db-init/01-app-role.sh and
        # scripts/security/ensure_app_role.py exactly (this fixture's whole point is to reproduce the
        # privileges the runtime role really has), NOT "ALL PRIVILEGES". `ALL` would hand the
        # test suite's app-role sessions TRUNCATE/REFERENCES/TRIGGER that production
        # deliberately withholds — see _NEVER_GRANTED_TABLE_PRIVS in
        # backend/scripts/security/s1a_privilege_policy.py. Keeping this narrow is what makes the rest
        # of the suite a real regression test for the privilege reduction: every chat,
        # ingest, backfill, export and account-erasure test below now runs with exactly the
        # privilege set production grants, so a path that genuinely needed one of the removed
        # privileges fails here rather than in production.
        cur.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {app_role}")
        cur.execute(f"GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO {app_role}")
        cur.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {app_role}"
        )
        cur.execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL PRIVILEGES ON SEQUENCES TO {app_role}')
    db_conn.close()

    import sys

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        check=True,
        env={**os.environ},
    )

    # Mirrors app/main.py's on_startup() exactly (same three functions, same order, same
    # migration_engine) -- see this fixture's own docstring for the bug this closes.
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges, apply_rls

    apply_rls(migration_engine)
    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)

    yield

    cleanup_conn = psycopg2.connect(_server_dsn(settings.database_url))
    cleanup_conn.autocommit = True
    with cleanup_conn.cursor() as cur:
        _force_drop_database(cur, db_name)
    cleanup_conn.close()


@pytest.fixture(autouse=True)
def _clean_tables(_test_database):
    """Truncates every application table before each test — cheap at this size and gives
    every test a known-empty starting point without the complexity of nested
    transaction/savepoint rollback (which the app's own RLS session-variable wiring, tied to
    a contextvar set per-request, doesn't interact well with)."""
    from app.config import get_settings

    settings = get_settings()
    engine = create_engine(settings.database_url)
    with engine.begin() as conn:
        tables = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename != 'alembic_version'")
        ).scalars().all()
        if tables:
            conn.execute(text(f"TRUNCATE TABLE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    engine.dispose()
    yield


@pytest.fixture(autouse=True)
def _clean_rate_limits():
    """Flushes the rate limiter's Redis DB before each test — unlike Postgres (truncated
    above), nothing else resets it between tests, so without this, tests that make several
    /api/auth/login calls (e.g. the rotation/reuse tests) would eventually trip the real
    per-IP rate limit purely as a side effect of test order and fail with 429 instead of the
    status they're actually asserting on. Rate limiting itself is exercised deliberately and
    explicitly in tests/account/test_rate_limiting.py."""
    from app.config import get_settings

    settings = get_settings()
    if settings.redis_url:
        import redis

        client = redis.from_url(settings.redis_url)
        client.flushdb()
        client.close()
    yield


@pytest.fixture
def sent_emails(monkeypatch):
    """Captures every email the app "sends" during a test instead of hitting real SMTP (none
    is configured anyway in the test env) or writing to the dev-mail-outbox path. Returns the
    list itself; each entry is {"to", "subject", "body"}."""
    import app.routers.auth as auth_router

    captured: list[dict] = []

    def _fake_send_email(to: str, subject: str, body_text: str) -> None:
        captured.append({"to": to, "subject": subject, "body": body_text})

    monkeypatch.setattr(auth_router, "send_email", _fake_send_email)
    return captured


@pytest.fixture
def client(sent_emails):
    """A TestClient using `with` so FastAPI's startup/shutdown events actually fire
    (bootstrap admin creation, scheduler start/stop) — a bare `TestClient(app)` without the
    context manager does NOT trigger them.

    base_url is deliberately https://, not the default http://testserver: the session
    cookies are set with Secure=true (see app/cookies.py, by design — see
    docs/AUTH_THREAT_MODEL.md), and httpx's cookie jar — correctly mimicking real browser
    behavior — refuses to re-send a Secure cookie on a follow-up request it considers
    plain HTTP. Without this, every multi-request test would silently lose its session
    cookie after the first call, for a reason that has nothing to do with the app's own
    correctness."""
    from app.main import app

    with TestClient(app, base_url="https://testserver") as c:
        yield c


@pytest.fixture
def db_session():
    """A raw SQLAlchemy session for tests that need to set up or inspect state directly
    (bypassing the API), through the restricted runtime role — same connection the app
    itself uses, so RLS is exercised identically to production."""
    from app.db import SessionLocal

    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def superuser_db():
    """A session bound to the SUPERUSER connection (app.db.migration_engine), which bypasses
    Row-Level Security unconditionally (see app/db.py) — for tests that need to verify what
    actually exists in the database regardless of RLS, e.g. confirming a row was genuinely
    deleted rather than merely hidden from a query that RLS happens to be filtering. Using
    db_session (the restricted runtime role) for that kind of check is a trap: a query
    returning zero rows there is ambiguous between "really deleted" and "exists, but RLS is
    hiding it because no app.current_user_id is set for this ad-hoc test session" — the
    latter is a false pass, not a real one."""
    from sqlalchemy.orm import sessionmaker

    from app.db import migration_engine

    session = sessionmaker(bind=migration_engine)()
    yield session
    session.close()


@pytest.fixture
def make_verified_user(db_session):
    """Creates a verified, active user directly in the DB (bypassing the register/verify
    HTTP flow, which is tested in its own right elsewhere) — for tests that just need *a*
    working account to log in as."""
    from app.models.user import User
    from app.security import hash_password, utcnow_seconds_baseline

    def _make(email: str | None = None, password: str = "CorrectHorseBattery9!", role: str = "member") -> tuple[User, str]:
        email = email or f"user-{uuid.uuid4().hex[:10]}@example.com"
        now = utcnow_seconds_baseline()  # must match JWT iat precision — see app/security.py
        user = User(
            email=email,
            password_hash=hash_password(password),
            role=role,
            email_verified=True,
            email_verified_at=now,
            sessions_valid_after=now,
        )
        db_session.add(user)
        db_session.commit()
        return user, password

    return _make
