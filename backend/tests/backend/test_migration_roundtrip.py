"""DEL 9 (STEG 9) of the Founder Knowledge Studio work order: a migration round-trip test —
upgrade -> downgrade -> upgrade — run against the SAME session-scoped test database every
other test in the suite uses (see conftest.py's `_test_database`, which already ran
`alembic upgrade head` once at session start). This test downgrades one step (0006 -> 0005,
removing Founder Knowledge Studio v1's tables/columns) and upgrades back to head, verifying
both the schema teardown and the schema restore are genuinely reversible — not just that
`downgrade()` runs without raising.

Runs via subprocess (same pattern as conftest.py's `_test_database` fixture) rather than
calling alembic's Python API directly, so it exercises the exact command path
`docs/OPERATIONS.md` documents for a real rollback."""

import os
import subprocess
import sys

from sqlalchemy import inspect

from app.db import migration_engine

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run_alembic(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "alembic", *args], cwd=BACKEND_ROOT, check=True, env={**os.environ})


def _table_names() -> set[str]:
    migration_engine.dispose()  # drop pooled connections so the inspector sees the current schema, not a stale cached one
    return set(inspect(migration_engine).get_table_names())


def _document_columns() -> set[str]:
    migration_engine.dispose()
    return {col["name"] for col in inspect(migration_engine).get_columns("documents")}


def test_migration_0006_upgrade_downgrade_upgrade_round_trip():
    """Leaves the database at `head` when it finishes, whether it passes or fails — every
    other test in the suite depends on that schema being present."""
    fks_tables = {"knowledge_versions", "knowledge_import_jobs", "source_relationships"}
    fks_columns = {"checksum", "classification", "active_truth_status", "project_id", "deleted_at", "import_job_id", "version_number"}

    before = _table_names()
    assert fks_tables <= before, "test database must already be at head (see conftest.py's _test_database)"
    assert fks_columns <= _document_columns()

    try:
        _run_alembic("downgrade", "-1")
        after_downgrade_tables = _table_names()
        assert fks_tables.isdisjoint(after_downgrade_tables), "downgrade must actually remove the new tables, not just claim to"
        assert fks_columns.isdisjoint(_document_columns()), "downgrade must actually remove the new documents columns"

        _run_alembic("upgrade", "head")
        after_upgrade_tables = _table_names()
        assert fks_tables <= after_upgrade_tables, "re-upgrading must restore every table downgrade removed"
        assert fks_columns <= _document_columns(), "re-upgrading must restore every documents column downgrade removed"
        # The round trip must be a true no-op on the rest of the schema, not just on the
        # Founder Knowledge Studio pieces this migration owns.
        assert before == after_upgrade_tables
    except Exception:
        # Best-effort recovery so a failure here doesn't leave every subsequent test in the
        # session failing for an unrelated, confusing reason (missing tables).
        _run_alembic("upgrade", "head")
        raise
