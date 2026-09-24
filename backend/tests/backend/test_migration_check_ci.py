"""Regression: CI migration-check must provision mainai_app before historical GRANTs.

Migration 0070 (and later) GRANT privileges TO mainai_app. A fresh GitHub Actions
Postgres service has no such role, so `alembic upgrade head` without the documented
bootstrap (`ensure_app_role.py`) fails with `role "mainai_app" does not exist`.

This is a file-level contract on `.github/workflows/ci.yml`, not a live cluster boot.
The live proof is the migration-check job itself.
"""

from pathlib import Path
import re

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _migration_check_block() -> str:
    text = CI_YML.read_text()
    match = re.search(
        r"(?ms)^  migration-check:.*?(?=^  [a-z0-9-]+:|\Z)",
        text,
    )
    assert match, "migration-check job missing from .github/workflows/ci.yml"
    return match.group(0)


def test_migration_check_uses_documented_role_bootstrap_before_alembic():
    block = _migration_check_block()
    assert "MAINAI_APP_PASSWORD: ci_migration_check_app_pw" in block
    assert "scripts/security/ensure_app_role.py" in block
    assert "scripts/security/apply_runtime_privileges.py" in block
    assert "alembic upgrade head" in block

    first_ensure = block.find("python scripts/security/ensure_app_role.py")
    first_alembic = block.find("alembic upgrade head")
    assert first_ensure != -1 and first_alembic != -1
    assert first_ensure < first_alembic, (
        "ensure_app_role.py must run before the first alembic upgrade head "
        "so 0070 GRANT TO mainai_app has a role to grant to"
    )

    assert not re.search(r"CREATE ROLE\s+mainai_app\b", block), (
        "migration-check must not provision mainai_app with ad-hoc SQL — "
        "the documented bootstrap is backend/scripts/security/ensure_app_role.py"
    )
    assert "MAINAI_APP_ROTATE_PASSWORD" not in block, (
        "do not rotate the cluster-global role password between migration-check databases"
    )


def test_migration_check_proves_least_privilege_before_alembic():
    block = _migration_check_block()
    assert "rolsuper IS FALSE" in block
    assert "rolcreatedb IS FALSE" in block
    assert "rolcreaterole IS FALSE" in block
    assert "rolbypassrls IS FALSE" in block
    assert "WHERE rolname = 'mainai_app'" in block
