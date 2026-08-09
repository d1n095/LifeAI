"""Founder-reported production incident (VPS deploy of PR #36's merged base): the durable-worker
container crash-looped in `apply_runtime_privileges.py` with
`psycopg2.errors.InternalError_: tuple concurrently updated` — it was running the exact same
mutating REVOKE/GRANT statements as the backend container, concurrently, against the same
catalog rows (pg_class.relacl / pg_proc.proacl). Fixed by making the worker's own boot path
read-only (see backend/docker-entrypoint.sh's RUN_PRIVILEGE_BOOT, backend/scripts/
ensure_app_role.py's `--derive-only`, backend/scripts/security/apply_runtime_privileges.py's
`--verify-only`), plus a Postgres advisory lock (`acquire_privilege_boot_lock()`) serializing
any remaining concurrent MUTATORS (e.g. two backend replicas overlapping during a rolling
deploy).

These tests exercise the real scripts against the real local Postgres test database this suite
already uses (see conftest.py's `_test_database` fixture) — not mocks — because the bug itself
was a real Postgres catalog-locking behavior no mock could reproduce.
"""

import importlib.util
import threading
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
import pytest

from app.config import get_settings

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "security"


def _load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def app_password() -> str:
    settings = get_settings()
    return urlparse(settings.app_database_url).password or "mainai_app_pw"


class _RecordingCursor:
    """psycopg2's real cursor object has a read-only `execute` attribute (a C extension
    method), so it can't be monkeypatched directly — this thin wrapper records every SQL
    statement passed to `execute()` while delegating everything else (including `execute`
    itself, and iteration/fetchone/fetchall) to the real, underlying cursor."""

    def __init__(self, real_cursor):
        self._real_cursor = real_cursor
        self.executed: list[str] = []

    def execute(self, sql, params=None):
        self.executed.append(str(sql))
        return self._real_cursor.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real_cursor, name)


def test_mutate_false_issues_no_revoke_or_grant_statements(app_password):
    """`apply_privilege_policy(..., mutate=False)` must never execute a single REVOKE/GRANT —
    only the read-only verification queries. Proven by wrapping a REAL cursor and recording
    every statement actually sent to Postgres, not by trusting the code path alone."""
    s1a_policy = _load_module("s1a_privilege_policy")
    settings = get_settings()

    # Establish a known-good, narrowed baseline first (mirrors the real boot order).
    apply_runtime_privileges = _load_module("apply_runtime_privileges")
    apply_runtime_privileges.apply_and_verify(settings.database_url)

    conn = psycopg2.connect(settings.database_url)
    try:
        with conn.cursor() as real_cur:
            cur = _RecordingCursor(real_cur)

            cur.execute("SELECT current_user")
            (expected_owner,) = cur.fetchone()
            errors = s1a_policy.apply_privilege_policy(
                cur, expected_owner=expected_owner, require_complete=True, mutate=False
            )
            assert errors == [], f"expected a clean read-only verification, got: {errors}"

        mutating = [s for s in cur.executed if s.strip().upper().startswith(("REVOKE", "GRANT"))]
        assert mutating == [], f"mutate=False issued mutating statement(s): {mutating}"
    finally:
        conn.rollback()
        conn.close()


def test_verify_only_never_acquires_the_advisory_lock(app_password):
    """A read-only verifier must never block behind a writer's lock (see
    `acquire_privilege_boot_lock()`'s own docstring) — proven here by holding the lock in one
    session and confirming `verify_only()` still completes promptly in another, rather than
    hanging until the lock-holding session releases it."""
    settings = get_settings()
    apply_runtime_privileges = _load_module("apply_runtime_privileges")

    holder_conn = psycopg2.connect(settings.database_url)
    holder_conn.autocommit = False
    try:
        with holder_conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(72197001, 1)")

            # verify_only() must complete without ever waiting on the lock the line above holds.
            apply_runtime_privileges.verify_only(settings.database_url, attempts=1, delay_seconds=0.01)
    finally:
        holder_conn.rollback()
        holder_conn.close()


def test_verify_only_retries_and_then_succeeds_once_the_mutating_apply_commits(app_password):
    """Reproduces the exact "worker's read-only verify runs concurrently with the backend's
    own first-time narrowing" window: deliberately leaves privileges WIDE (unnarrowed), starts
    `verify_only()` in a background thread (which must see errors and retry, not fail
    instantly), then runs the real mutating `apply_and_verify()` shortly after. Confirms the
    background verify eventually succeeds — the retry budget smooths over the ordering gap
    instead of treating a transiently-not-yet-narrowed read as fatal."""
    settings = get_settings()
    from sqlalchemy import create_engine
    from sqlalchemy import text as sa_text

    apply_runtime_privileges = _load_module("apply_runtime_privileges")

    engine = create_engine(settings.database_url)
    try:
        # Deliberately widen mainai_app back open on one protected table, simulating a boot
        # right after ensure_app_role.py's own wide GRANT ALL, before anything has narrowed it.
        with engine.begin() as conn:
            conn.execute(sa_text("GRANT UPDATE ON TABLE public.memory_source_units TO mainai_app"))

        result: dict[str, object] = {}

        def _verify_in_background():
            try:
                apply_runtime_privileges.verify_only(settings.database_url, attempts=10, delay_seconds=0.3)
                result["ok"] = True
            except SystemExit as exc:
                result["ok"] = False
                result["exc"] = exc

        thread = threading.Thread(target=_verify_in_background)
        thread.start()

        # Give the background verify at least one real "not yet narrowed" attempt before the
        # mutating apply runs, so this test actually exercises the retry path, not just a
        # first-attempt pass.
        import time

        time.sleep(0.5)
        apply_runtime_privileges.apply_and_verify(settings.database_url)

        thread.join(timeout=15)
        assert not thread.is_alive(), "verify_only() background thread never finished"
        assert result.get("ok") is True, f"verify_only() did not eventually succeed: {result}"
    finally:
        engine.dispose()


def test_concurrent_backend_mutation_and_worker_verification_never_race(app_password):
    """The founder's exact required proof: start a backend-style (mutating) boot and a
    worker-style (read-only verify) boot AT THE SAME TIME — plus a SECOND concurrent
    backend-style mutation, simulating two backend replicas overlapping during a rolling
    deploy — and confirm:
      - exactly the two mutating callers ever touch REVOKE/GRANT (the read-only verifier never
        does, see test_mutate_false_issues_no_revoke_or_grant_statements above for that half);
      - neither mutating caller raises psycopg2.errors.InternalError_ ("tuple concurrently
        updated") or ANY exception — the advisory lock serializes them instead of racing;
      - the read-only verifier completes successfully (its own bounded retries absorb the
        ordering uncertainty of which mutator finishes first);
      - the final privilege state is correctly narrowed.
    Uses a real threading.Barrier so all three actually start their DB work in the same
    instant, not just "close enough" via a sleep."""
    settings = get_settings()
    apply_runtime_privileges = _load_module("apply_runtime_privileges")

    barrier = threading.Barrier(3, timeout=10)
    results: dict[str, object] = {}
    errors: list[str] = []

    def _backend(name: str):
        try:
            barrier.wait()
            apply_runtime_privileges.apply_and_verify(settings.database_url)
            results[name] = "ok"
        except Exception as exc:  # noqa: BLE001 - captured for the assertion below
            results[name] = "error"
            errors.append(f"{name}: {exc!r}")

    def _worker():
        try:
            barrier.wait()
            apply_runtime_privileges.verify_only(settings.database_url, attempts=15, delay_seconds=0.3)
            results["worker"] = "ok"
        except SystemExit as exc:
            results["worker"] = "error"
            errors.append(f"worker: {exc!r}")
        except Exception as exc:  # noqa: BLE001
            results["worker"] = "error"
            errors.append(f"worker: {exc!r}")

    threads = [
        threading.Thread(target=_backend, args=("backend_replica_a",)),
        threading.Thread(target=_backend, args=("backend_replica_b",)),
        threading.Thread(target=_worker),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "a concurrent privilege-boot participant never finished — possible deadlock"

    assert errors == [], f"concurrent privilege boot raised: {errors}"
    assert results == {"backend_replica_a": "ok", "backend_replica_b": "ok", "worker": "ok"}
    for _name, outcome in results.items():
        assert "tuple concurrently updated" not in str(outcome).lower()

    # Final state must still be correctly narrowed, not left half-applied by the race.
    conn = psycopg2.connect(settings.database_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT has_table_privilege('mainai_app', 'memory_source_units', 'UPDATE')")
            has_update = cur.fetchone()[0]
        assert has_update is False, "final privilege state left mainai_app with UPDATE it should never have"
    finally:
        conn.close()


def test_derive_only_never_connects_to_postgres(monkeypatch, tmp_path):
    """`--derive-only` (the worker's path for computing APP_DATABASE_URL) must be pure string
    computation — proven by making psycopg2.connect raise if `derive_only()` ever calls it."""
    module = _load_module("ensure_app_role")
    settings = get_settings()
    app_pw = urlparse(settings.app_database_url).password or "mainai_app_pw"

    def _connect_should_never_be_called(*args, **kwargs):
        raise AssertionError("derive_only() must never call psycopg2.connect")

    monkeypatch.setattr(module.psycopg2, "connect", _connect_should_never_be_called)
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    monkeypatch.setenv("MAINAI_APP_PASSWORD", app_pw)
    env_file = tmp_path / "render_env"
    monkeypatch.setenv("RENDER_ENV_FILE", str(env_file))

    module.derive_only()

    content = env_file.read_text()
    assert "APP_DATABASE_URL=" in content


def test_derive_only_computes_the_same_url_as_the_mutating_path(monkeypatch, tmp_path):
    """The worker's derived APP_DATABASE_URL must be byte-identical to what the backend's own
    mutating path would derive — both containers share the same DATABASE_URL/
    MAINAI_APP_PASSWORD, so the app role connection string must never differ by which path
    computed it."""
    module = _load_module("ensure_app_role")
    settings = get_settings()
    app_pw = urlparse(settings.app_database_url).password or "mainai_app_pw"

    expected = module._derive_app_database_url(settings.database_url, app_pw)

    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    monkeypatch.setenv("MAINAI_APP_PASSWORD", app_pw)
    env_file = tmp_path / "render_env"
    monkeypatch.setenv("RENDER_ENV_FILE", str(env_file))

    module.derive_only()

    content = env_file.read_text()
    assert f'export APP_DATABASE_URL="{expected}"' in content


def test_verify_only_fails_closed_when_privilege_state_is_genuinely_wrong(app_password):
    """The worker must still refuse to boot (SystemExit, non-zero) if the privilege state is
    genuinely incorrect and never becomes correct within the retry budget — not just "usually
    passes eventually." Uses a short, bounded retry budget so this test stays fast."""
    settings = get_settings()
    from sqlalchemy import create_engine
    from sqlalchemy import text as sa_text

    apply_runtime_privileges = _load_module("apply_runtime_privileges")

    engine = create_engine(settings.database_url)
    try:
        with engine.begin() as conn:
            conn.execute(sa_text("GRANT UPDATE ON TABLE public.memory_source_units TO mainai_app"))

        with pytest.raises(SystemExit):
            apply_runtime_privileges.verify_only(settings.database_url, attempts=2, delay_seconds=0.05)
    finally:
        # Restore a clean, narrowed state for any tests that run after this one.
        apply_runtime_privileges.apply_and_verify(settings.database_url)
        engine.dispose()
