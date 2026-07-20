"""DEL 9 (STEG 9) of the Founder Knowledge Studio work order: migration round-trip tests —
upgrade -> downgrade -> upgrade — run against the SAME session-scoped test database every
other test in the suite uses (see conftest.py's `_test_database`, which already ran
`alembic upgrade head` once at session start).

Deliberately migration-agnostic (compares whole-schema snapshots rather than hardcoding
specific table/column names): a first version of this test hardcoded migration 0006's tables
and broke the moment migration 0007 (STEG 10, claim-level trust) became head — `downgrade -1`
from a new head only ever undoes the LATEST migration, whatever that happens to be, so a test
tied to one specific migration's contents goes stale every time a new migration is added.
Comparing snapshots checks the same property (downgrade is genuinely reversible, not just
error-free) without that coupling.

Runs via subprocess (same pattern as conftest.py's `_test_database` fixture) rather than
calling alembic's Python API directly, so it exercises the exact command path
`docs/OPERATIONS.md` documents for a real rollback."""

import os
import subprocess
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from app.db import migration_engine

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run_alembic(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "alembic", *args], cwd=BACKEND_ROOT, check=True, env={**os.environ})


def _revision_count() -> int:
    config = Config(os.path.join(BACKEND_ROOT, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(BACKEND_ROOT, "alembic"))
    script = ScriptDirectory.from_config(config)
    return len(list(script.walk_revisions()))


def _schema_snapshot() -> dict:
    """Whole-schema fingerprint: every table's column set, keyed by table name. Anything a
    migration touches (new table, new column, dropped column) shows up here without this
    test needing to know which migration or which table in advance. Excludes
    `alembic_version` itself — Alembic's own bookkeeping table, which legitimately survives
    a `downgrade base` (it just ends up empty), not application schema."""
    migration_engine.dispose()  # drop pooled connections so the inspector sees the current schema, not a stale cached one
    inspector = inspect(migration_engine)
    return {
        table: frozenset(col["name"] for col in inspector.get_columns(table))
        for table in inspector.get_table_names()
        if table != "alembic_version"
    }


def test_latest_migration_upgrade_downgrade_upgrade_round_trip():
    """Leaves the database at `head` when it finishes, whether it passes or fails — every
    other test in the suite depends on that schema being present."""
    before = _schema_snapshot()

    try:
        _run_alembic("downgrade", "-1")
        after_downgrade = _schema_snapshot()
        assert after_downgrade != before, "downgrade -1 must actually change the schema, not silently no-op"

        _run_alembic("upgrade", "head")
        after_upgrade = _schema_snapshot()
        assert after_upgrade == before, "upgrading back to head must restore the exact schema downgrade removed"
    except Exception:
        # Best-effort recovery so a failure here doesn't leave every subsequent test in the
        # session failing for an unrelated, confusing reason (missing tables).
        _run_alembic("upgrade", "head")
        raise


def test_full_migration_chain_downgrades_to_base_and_back_to_head():
    """A stronger version of the same property across EVERY migration at once, not just the
    latest one — downgrading a fresh database all the way to base and re-upgrading to head
    must reproduce the exact same schema `_test_database` (conftest.py) already built by
    running every migration forward once. Confirms the whole chain is reversible, not just
    whatever migration happens to be newest."""
    if _revision_count() < 2:
        return  # nothing meaningful to chain-test with only one migration

    before = _schema_snapshot()
    try:
        _run_alembic("downgrade", "base")
        after_downgrade = _schema_snapshot()
        assert after_downgrade != before
        assert after_downgrade == {}, "downgrading every migration must leave no application tables behind"

        _run_alembic("upgrade", "head")
        after_upgrade = _schema_snapshot()
        assert after_upgrade == before
    except Exception:
        _run_alembic("upgrade", "head")
        raise
