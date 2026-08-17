"""Regression contracts for the Cloud Agent / local VM bootstrap (.cursor/).

These are file-level proofs, not live VM boots: a Cloud Agent that starts FastAPI and
Next.js but never `python -m app.worker` leaves import jobs, MainAI jobs, and the
account-erasure storage-deletion outbox permanently pending. A bootstrap that hardcodes
the `mainai_app` password independently of backend/.env silently breaks RLS-scoped runtime
connections as soon as MAINAI_APP_PASSWORD differs.
"""

from pathlib import Path
import json
import re

REPO_ROOT = Path(__file__).resolve().parents[3]
CURSOR_DIR = REPO_ROOT / ".cursor"


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
