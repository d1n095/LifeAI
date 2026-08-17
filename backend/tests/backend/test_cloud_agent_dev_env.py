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

# Import without adding .cursor to the app package path permanently.
sys.path.insert(0, str(CURSOR_DIR))
from derive_app_database_url import derive_app_database_url  # noqa: E402
from parse_database_url import parse_lifeos_database_url  # noqa: E402
from sync_app_database_url import sync_env_file  # noqa: E402


def test_cloud_agent_environment_starts_the_durable_worker():
    spec = json.loads((CURSOR_DIR / "environment.json").read_text())
    commands = [terminal["command"] for terminal in spec["terminals"]]
    assert any("python -m app.worker" in command for command in commands), (
        "Cloud Agent environment.json must start app.worker — otherwise library imports, "
        "MainAI jobs, and storage_deletion_tasks never run in this VM"
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


def test_derive_app_database_url_matches_ensure_app_role(monkeypatch):
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
