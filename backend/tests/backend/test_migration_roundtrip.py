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
from sqlalchemy import text as sa_text

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
    """Whole-schema fingerprint: every table's column set plus every native enum type's
    label set, keyed by table/type name. Anything a migration touches (new table, new
    column, dropped column, an ADD VALUE on an existing enum like migration 0011's) shows up
    here without this test needing to know which migration or which table in advance. Enum
    labels are included alongside columns because a migration can be purely additive at the
    enum level (no new/changed columns at all) — Postgres's ALTER TYPE ... ADD VALUE is
    exactly that shape, and a snapshot of column names alone would never notice it ran.
    Excludes `alembic_version` itself — Alembic's own bookkeeping table, which legitimately
    survives a `downgrade base` (it just ends up empty), not application schema.

    Also fingerprints every function in the `public` schema — added after a real gap this
    test itself caught: migration 0020 (Pass 23) is purely additive at the function level
    (one new SECURITY DEFINER function, no new/changed table or enum at all), and a snapshot
    of columns/enum-labels alone is completely blind to that -- `downgrade -1` genuinely
    removed the function, but the "before" and "after downgrade" snapshots compared
    byte-for-byte equal, silently defeating the very "downgrade -1 must actually change the
    schema" assertion below. Trigger functions and SECURITY DEFINER functions are both
    ordinary rows in pg_proc, so this one query covers both without needing to know which
    migration added which kind.

    Pass 24, deepened further after a founder review pointed out that name+signature alone
    would happily call a function "restored" even if `downgrade`/`upgrade` (or a stray manual
    `ALTER FUNCTION`) silently changed its SECURITY DEFINER/INVOKER status, return type,
    `search_path`, or language — none of which affect the name or argument list Postgres uses
    to identify an overload. Each function's fingerprint now also includes its return type,
    `prosecdef` (SECURITY DEFINER vs INVOKER), `proconfig` (covers `search_path`), language,
    and an md5 of `pg_get_functiondef()` (the function's full, canonical CREATE-statement
    text, body included) — so "the schema was restored exactly" actually means the SECURITY
    properties came back too, not just that a same-named function reappeared."""
    migration_engine.dispose()  # drop pooled connections so the inspector sees the current schema, not a stale cached one
    inspector = inspect(migration_engine)
    snapshot = {
        table: frozenset(col["name"] for col in inspector.get_columns(table))
        for table in inspector.get_table_names()
        if table != "alembic_version"
    }
    for enum in inspector.get_enums():
        snapshot[f"enum:{enum['name']}"] = frozenset(enum["labels"])
    with migration_engine.connect() as conn:
        function_rows = conn.execute(
            sa_text(
                "SELECT p.oid::regprocedure::text, p.prorettype::regtype::text, p.prosecdef, "
                "       p.proconfig, l.lanname, md5(pg_get_functiondef(p.oid)) "
                "FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "JOIN pg_language l ON l.oid = p.prolang "
                "WHERE n.nspname = 'public' "
                # Excludes functions owned by an installed EXTENSION (pg_depend deptype='e') --
                # pgvector's own functions (array_to_vector, avg(vector), etc.) install into
                # `public` by default and are NOT touched by any migration's upgrade/downgrade
                # (the extension itself is created once, outside the migration chain, and
                # downgrading to base correctly leaves it in place) — without this filter,
                # test_full_migration_chain_downgrades_to_base_and_back_to_head would see
                # those extension functions as "leftover application schema" after a full
                # downgrade to base, which they are not.
                "AND NOT EXISTS ("
                "  SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e'"
                ")"
            )
        ).all()
    function_fingerprints = frozenset(
        (signature, return_type, prosecdef, tuple(proconfig or []), language, definition_hash)
        for signature, return_type, prosecdef, proconfig, language, definition_hash in function_rows
    )
    if function_fingerprints:
        # Only added when non-empty -- `downgrade base` must produce a literally empty {}
        # snapshot (see test_full_migration_chain_downgrades_to_base_and_back_to_head's own
        # `after_downgrade == {}` assertion), matching how the table/enum snapshotting above
        # already contributes zero keys once nothing is left, rather than an explicit
        # "functions": frozenset() entry that would make an otherwise-empty schema compare
        # unequal to a real {}.
        snapshot["functions"] = function_fingerprints
    return snapshot


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
