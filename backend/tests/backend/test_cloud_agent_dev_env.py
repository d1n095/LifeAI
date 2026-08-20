"""Regression contracts for the Cloud Agent / local VM bootstrap (.cursor/).

These are file-level proofs, not live VM boots: a Cloud Agent that starts FastAPI and
Next.js but never `python -m app.worker` leaves import jobs, MainAI jobs, and the
account-erasure storage-deletion outbox permanently pending. A bootstrap that hardcodes
role passwords independently of backend/.env silently breaks connections as soon as
DATABASE_URL or MAINAI_APP_PASSWORD differs.
"""

from pathlib import Path
import json
import os
import re
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CURSOR_DIR = REPO_ROOT / ".cursor"
# Canonical dev founder password — must match tests/conftest.py setdefault and smoke/E2E tests.
CANONICAL_FOUNDER_PASSWORD = "TestFounderPassword123!"

# Import without adding .cursor to the app package path permanently.
sys.path.insert(0, str(CURSOR_DIR))
from derive_app_database_url import derive_app_database_url  # noqa: E402
from derive_pytest_env import derive_pytest_database_urls, read_env_keys  # noqa: E402
from parse_database_url import parse_lifeos_database_url  # noqa: E402
from sync_app_database_url import sync_env_file  # noqa: E402


def test_cloud_agent_environment_starts_the_durable_worker():
    spec = json.loads((CURSOR_DIR / "environment.json").read_text())
    commands = [terminal["command"] for terminal in spec["terminals"]]
    assert any("run-worker.sh" in cmd or "python -m app.worker" in cmd for cmd in commands), (
        "Cloud Agent environment.json must start app.worker (directly or via run-worker.sh) "
        "— otherwise library imports, MainAI jobs, and storage_deletion_tasks never run in this VM"
    )


def test_setup_services_uses_env_password_not_a_hardcoded_app_role_secret():
    script = (CURSOR_DIR / "setup-services.sh").read_text()
    assert "MAINAI_APP_PASSWORD" in script
    assert ': "${MAINAI_APP_PASSWORD:?MAINAI_APP_PASSWORD must be set in backend/.env}"' in script
    assert not re.search(r"CREATE ROLE mainai_app LOGIN PASSWORD 'mainai_app'", script), (
        "mainai_app password must come from MAINAI_APP_PASSWORD (psql :'app_pw'), not a "
        "second hardcoded secret that can drift from APP_DATABASE_URL"
    )
    assert "PASSWORD :'app_pw'" in script


def test_setup_services_uses_database_url_not_a_hardcoded_lifeos_password():
    script = (CURSOR_DIR / "setup-services.sh").read_text()
    assert "parse_database_url.py" in script
    assert "PASSWORD :'lifeos_pw'" in script
    assert not re.search(r"PASSWORD 'lifeos'", script), (
        "lifeos superuser password must come from DATABASE_URL, not a hardcoded secret"
    )


@pytest.mark.parametrize(
    "url, password, db_name",
    [
        ("postgresql://lifeos:lifeos@localhost:5432/lifeos", "lifeos", "lifeos"),
        ("postgresql://lifeos:s3cret%40pw@localhost:5432/lifeos_dev", "s3cret@pw", "lifeos_dev"),
    ],
)
def test_parse_lifeos_database_url_accepts_canonical_urls(url, password, db_name):
    assert parse_lifeos_database_url(url) == (password, db_name)


@pytest.mark.parametrize(
    "url, match",
    [
        ("", "postgresql://"),
        ("postgresql://mainai_app:x@localhost:5432/lifeos", "must be lifeos"),
        ("postgresql://lifeos@localhost:5432/lifeos", "must include a password"),
        ("postgresql://lifeos:pw@localhost:5432/", "database name"),
        ("postgresql://lifeos:pw@localhost:5432/lifeos;DROP", "simple identifier"),
    ],
)
def test_parse_lifeos_database_url_rejects_malformed_or_wrong_role(url, match):
    with pytest.raises(ValueError, match=match):
        parse_lifeos_database_url(url)


def test_parse_database_url_cli_never_prints_url_or_password_on_stderr():
    env = {**os.environ, "DATABASE_URL": "postgresql://lifeos:super-secret@localhost:5432/lifeos"}
    proc = subprocess.run(
        [sys.executable, str(CURSOR_DIR / "parse_database_url.py")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0
    assert "LIFEOS_PASSWORD=" in proc.stdout
    assert "LIFEOS_DB=lifeos" in proc.stdout
    assert "super-secret" not in proc.stderr
    assert "DATABASE_URL" not in proc.stderr
    assert "postgresql://" not in proc.stderr


def test_parse_database_url_cli_fails_closed_without_leaking_url():
    env = {**os.environ, "DATABASE_URL": "postgresql://mainai_app:leaked-pw@localhost:5432/lifeos"}
    proc = subprocess.run(
        [sys.executable, str(CURSOR_DIR / "parse_database_url.py")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 1
    assert "leaked-pw" not in proc.stderr
    assert "postgresql://" not in proc.stderr
    assert "must be lifeos" in proc.stderr


def test_setup_services_syncs_app_database_url_from_mainai_app_password():
    script = (CURSOR_DIR / "setup-services.sh").read_text()
    assert "sync_app_database_url.py" in script
    assert "APP_DATABASE_URL must be set after sync" in script


def test_install_sh_does_not_hardcode_app_database_url_independently_of_mainai_app_password():
    script = (CURSOR_DIR / "install.sh").read_text()
    assert "sync_app_database_url.py" in script
    assert "APP_DATABASE_URL=postgresql://mainai_app:" not in script


def test_install_sh_uses_canonical_founder_password_matching_pytest_harness():
    script = (CURSOR_DIR / "install.sh").read_text()
    assert f"FOUNDER_PASSWORD={CANONICAL_FOUNDER_PASSWORD}" in script
    assert "DevFounderPass" not in script


def test_derive_app_database_url_matches_ensure_app_role():
    security_dir = REPO_ROOT / "backend" / "scripts" / "security"
    sys.path.insert(0, str(security_dir))
    import ensure_app_role  # noqa: E402

    database_url = "postgresql://lifeos:lifeos@localhost:5432/lifeos"
    app_password = "s3cret@pw"
    assert derive_app_database_url(database_url, app_password) == ensure_app_role._derive_app_database_url(
        database_url, app_password
    )


def test_sync_env_file_rewrites_drifted_app_database_url_without_logging_secrets(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DATABASE_URL=postgresql://lifeos:lifeos@localhost:5432/lifeos\n"
        "MAINAI_APP_PASSWORD=rotated-pw\n"
        "APP_DATABASE_URL=postgresql://mainai_app:stale-pw@localhost:5432/lifeos\n"
    )
    changed = sync_env_file(
        env_path,
        database_url="postgresql://lifeos:lifeos@localhost:5432/lifeos",
        app_password="rotated-pw",
    )
    assert changed is True
    updated = env_path.read_text()
    assert "APP_DATABASE_URL=postgresql://mainai_app:rotated-pw@localhost:5432/lifeos" in updated
    assert "stale-pw" not in updated


def test_sync_app_database_url_cli_never_prints_password_on_stderr(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DATABASE_URL=postgresql://lifeos:lifeos@localhost:5432/lifeos\n"
        "MAINAI_APP_PASSWORD=super-secret\n"
    )
    proc = subprocess.run(
        [sys.executable, str(CURSOR_DIR / "sync_app_database_url.py"), str(env_path)],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DATABASE_URL": "postgresql://lifeos:lifeos@localhost:5432/lifeos",
            "MAINAI_APP_PASSWORD": "super-secret",
        },
    )
    assert proc.returncode == 0
    assert "super-secret" not in proc.stderr
    assert "postgresql://" not in proc.stderr
    assert "APP_DATABASE_URL=postgresql://mainai_app:super-secret@localhost:5432/lifeos" in env_path.read_text()


def test_run_backend_tests_script_derives_pytest_env_from_backend_dotenv():
    script = (CURSOR_DIR / "run-backend-tests.sh").read_text()
    assert 'derive_pytest_env.py" "$ENV_FILE"' in script
    assert 'exec pytest "$@"' in script
    assert '. "$ENV_FILE"' not in script, (
        "run-backend-tests.sh must not source the full dev .env into pytest — REDIS_URL db 0 "
        "and STORAGE_ROOT would bypass conftest isolation defaults"
    )


def test_read_env_keys_reads_only_requested_database_credentials(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "REDIS_URL=redis://localhost:6379/0\n"
        "DATABASE_URL=postgresql://lifeos:lifeos@localhost:5432/lifeos\n"
        "MAINAI_APP_PASSWORD=mainai_app\n"
        "STORAGE_ROOT=/home/dev/uploads\n"
    )
    vals = read_env_keys(env_path)
    assert set(vals) == {"DATABASE_URL", "MAINAI_APP_PASSWORD"}
    assert vals["DATABASE_URL"].startswith("postgresql://lifeos:")
    assert vals["MAINAI_APP_PASSWORD"] == "mainai_app"


def test_derive_pytest_env_uses_cloud_agent_postgres_not_conftest_5433_default():
    database_url = "postgresql://lifeos:lifeos@localhost:5432/lifeos"
    app_password = "mainai_app"
    test_db = "lifeos_test_999"
    admin_url, app_url = derive_pytest_database_urls(database_url, app_password, test_db)
    assert admin_url == "postgresql://lifeos:lifeos@localhost:5432/lifeos_test_999"
    assert app_url == "postgresql://mainai_app:mainai_app@localhost:5432/lifeos_test_999"
    assert ":5433/" not in admin_url


@pytest.mark.parametrize("bad_name", ["", "lifeos;drop", "lifeos-test"])
def test_derive_pytest_env_rejects_non_identifier_test_database_names(bad_name):
    with pytest.raises(ValueError, match="simple identifier"):
        derive_pytest_database_urls(
            "postgresql://lifeos:lifeos@localhost:5432/lifeos",
            "mainai_app",
            bad_name,
        )


def test_derive_pytest_env_cli_never_prints_secrets_on_stderr():
    proc = subprocess.run(
        [sys.executable, str(CURSOR_DIR / "derive_pytest_env.py")],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DATABASE_URL": "postgresql://lifeos:super-secret@localhost:5432/lifeos",
            "MAINAI_APP_PASSWORD": "app-pw",
            "LIFEAI_TEST_DATABASE_NAME": "lifeos_test_cli",
        },
    )
    assert proc.returncode == 0
    assert "DATABASE_URL=" in proc.stdout
    assert "APP_DATABASE_URL=" in proc.stdout
    assert "super-secret" not in proc.stderr
    assert "app-pw" not in proc.stderr
    assert "postgresql://" not in proc.stderr


# --- Worker auto-restart wrapper ---


def test_environment_json_worker_terminal_uses_restart_wrapper():
    """The worker terminal must use run-worker.sh (restart-on-crash), not bare `python -m`."""
    env_json = json.loads((CURSOR_DIR / "environment.json").read_text())
    worker_terminals = [t for t in env_json["terminals"] if "worker" in t["name"].lower()]
    assert len(worker_terminals) == 1
    cmd = worker_terminals[0]["command"]
    assert "run-worker.sh" in cmd
    assert "python -m app.worker" not in cmd


def test_run_worker_script_restarts_on_nonzero_exit():
    """run-worker.sh must restart on non-zero exit and stop on exit 0."""
    script = (CURSOR_DIR / "run-worker.sh").read_text()
    assert "while true" in script
    assert "break" in script
    assert "$?" in script or "exit_code" in script


def test_run_worker_script_respects_graceful_shutdown():
    """Exit code 0 (graceful shutdown via SIGTERM handler) must NOT trigger a restart."""
    script = (CURSOR_DIR / "run-worker.sh").read_text()
    assert "exit_code -eq 0" in script


# --- Backend auto-restart wrapper ---


def test_environment_json_backend_terminal_uses_restart_wrapper():
    """The backend terminal must use run-backend.sh (restart-on-crash), not bare uvicorn."""
    env_json = json.loads((CURSOR_DIR / "environment.json").read_text())
    backend_terminals = [t for t in env_json["terminals"] if "backend" in t["name"].lower()]
    assert len(backend_terminals) == 1
    cmd = backend_terminals[0]["command"]
    assert "run-backend.sh" in cmd
    assert "uvicorn" not in cmd


def test_run_backend_script_has_bounded_failure_limit():
    """run-backend.sh must stop after consecutive failures to avoid masking deterministic bugs."""
    script = (CURSOR_DIR / "run-backend.sh").read_text()
    assert "MAX_CONSECUTIVE_FAILURES" in script
    assert "consecutive_failures" in script
    assert "Stopping" in script or "stopping" in script


def test_run_backend_script_resets_backoff_after_successful_run():
    """If uvicorn ran for >60s (was serving), backoff resets — it's a transient crash, not startup failure."""
    script = (CURSOR_DIR / "run-backend.sh").read_text()
    assert "run_duration" in script
    assert "delay=1" in script
