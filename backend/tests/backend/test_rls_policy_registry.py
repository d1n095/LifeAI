"""Drift-prevention for app/rls.py's self-heal loop (founder review, round 2 — MEDIUM 4).

apply_rls() re-applies RLS_STATEMENTS (ENABLE/FORCE) AND (re)creates any POLICY_DEFINITIONS
entry missing from pg_policies on every boot. If a table is ENABLE-listed but has no matching
POLICY_DEFINITIONS entry, a lost/dropped policy is never repaired — the table stays FORCE-RLS
with no policy, which is a silent default-deny (not a security hole, but a real availability
gap that this test exists to catch before it ships again for a new table).
"""

import ast
import re
from pathlib import Path

from sqlalchemy import text

from app.db import migration_engine
from app.rls import POLICY_DEFINITIONS, RLS_STATEMENTS

_ENABLE_RE = re.compile(r"^ALTER TABLE (\w+) ENABLE ROW LEVEL SECURITY$")


def test_policy_definitions_cover_every_rls_enabled_table():
    enabled_tables = {
        match.group(1) for stmt in RLS_STATEMENTS if (match := _ENABLE_RE.match(stmt.strip())) is not None
    }
    assert enabled_tables, "sanity check: RLS_STATEMENTS should not be empty"

    policy_tables = {policy["table"] for policy in POLICY_DEFINITIONS}

    missing = enabled_tables - policy_tables
    assert not missing, (
        f"tables have RLS ENABLEd in RLS_STATEMENTS but no matching POLICY_DEFINITIONS entry: "
        f"{sorted(missing)} — apply_rls()'s self-heal loop can never repair a lost policy for "
        f"these tables (see app/rls.py's module docstring)"
    )


def test_policy_definitions_has_no_orphan_entries():
    """The reverse gap: a POLICY_DEFINITIONS entry for a table RLS_STATEMENTS never enables
    would try to create a policy on a table without RLS enabled at all — not harmful, but a
    sign the two lists have drifted apart and one of them is wrong."""
    enabled_tables = {
        match.group(1) for stmt in RLS_STATEMENTS if (match := _ENABLE_RE.match(stmt.strip())) is not None
    }
    policy_tables = {policy["table"] for policy in POLICY_DEFINITIONS}

    orphans = policy_tables - enabled_tables
    assert not orphans, f"POLICY_DEFINITIONS references tables never ENABLEd in RLS_STATEMENTS: {sorted(orphans)}"


def test_live_policies_match_policy_definitions_exactly():
    """The OTHER drift direction, and the one migration 0031 makes materially dangerous: every
    policy is written twice — once by the migration that creates it on a fresh database, and
    once in POLICY_DEFINITIONS, which app/rls.py's boot loop uses to REPAIR it if it goes
    missing. That loop keys on the policy NAME only: if a policy with the right name already
    exists it is left alone, whatever its expression says. So two different rules under one
    name would never be noticed — a database built by migration would enforce one thing, and
    the same database after a repair would enforce another.

    That was survivable while every expression was the same one-line `owner_id = <uid>`
    comparison. `messages_isolation` (migration 0031) is a subquery over `conversations`, and
    it is the one policy where a silently divergent repair could widen access rather than
    merely differ.

    Compares against Postgres's own normalized form rather than the raw strings: the live
    policy is read from pg_policies, then POLICY_DEFINITIONS' expression is fed through
    CREATE POLICY on the same table and read back the same way, so formatting, whitespace and
    parenthesization can never make an identical rule look different (or vice versa). The
    probe policy is created and dropped inside a transaction that is always rolled back, so
    this test never leaves a policy behind even if it fails partway through."""
    mismatches: list[str] = []
    with migration_engine.connect() as conn:
        # Opened before the first statement so SQLAlchemy's autobegin doesn't claim the
        # connection first — everything below, including the probe policies, is rolled back.
        transaction = conn.begin()
        try:
            live = {
                (row.tablename, row.policyname): row
                for row in conn.execute(
                    text("SELECT tablename, policyname, qual, with_check FROM pg_policies WHERE schemaname = 'public'")
                ).all()
            }

            for policy in POLICY_DEFINITIONS:
                key = (policy["table"], policy["name"])
                actual = live.get(key)
                if actual is None:
                    mismatches.append(
                        f"{policy['table']}.{policy['name']}: declared in POLICY_DEFINITIONS but not present in "
                        "pg_policies after a real migration run"
                    )
                    continue

                conn.execute(
                    text(
                        f"CREATE POLICY __drift_probe ON {policy['table']} "
                        f"USING ({policy['expr']}) WITH CHECK ({policy['expr']})"
                    )
                )
                probe = conn.execute(
                    text(
                        "SELECT qual, with_check FROM pg_policies WHERE schemaname = 'public' "
                        "AND tablename = :t AND policyname = '__drift_probe'"
                    ),
                    {"t": policy["table"]},
                ).one()
                conn.execute(text(f"DROP POLICY __drift_probe ON {policy['table']}"))

                if probe.qual != actual.qual:
                    mismatches.append(
                        f"{policy['table']}.{policy['name']} USING differs:\n"
                        f"    migration/live : {actual.qual}\n"
                        f"    POLICY_DEFINITIONS: {probe.qual}"
                    )
                if probe.with_check != actual.with_check:
                    mismatches.append(
                        f"{policy['table']}.{policy['name']} WITH CHECK differs:\n"
                        f"    migration/live : {actual.with_check}\n"
                        f"    POLICY_DEFINITIONS: {probe.with_check}"
                    )
        finally:
            transaction.rollback()

    assert not mismatches, (
        "app/rls.py's POLICY_DEFINITIONS has drifted from the policies the migrations actually "
        "create — a repaired policy would enforce a different rule than a freshly migrated "
        "database:\n" + "\n".join(mismatches)
    )


def test_worker_module_never_imports_app_main():
    """Founder-reported production incident (VPS hotfix): `app/main.py`'s FastAPI startup
    handler calls `apply_rls()`/`apply_mainai_job_runtime_privileges()` — real, mutating
    REVOKE/GRANT statements against the same catalog rows `apply_privilege_policy()` manages.
    Those only ever fire when something actually runs the FastAPI app's lifespan (uvicorn, or
    a test client) — NOT merely by importing `app.main`. `app/worker.py` (the durable-worker
    container's entrypoint, `python -m app.worker`) must never import `app.main` at all, or it
    would trigger that same mutating path a second time, racing the backend container's own
    call to it. Checked via a static AST parse (not a runtime import, which would only prove
    "importing app.worker doesn't ALSO import app.main today" for whatever happens to already
    be imported/cached) so a future `import app.main` added anywhere in app/worker.py's own
    module — even one that looks harmless — is caught here directly, not by accident."""
    worker_path = Path(__file__).resolve().parent.parent.parent / "app" / "worker.py"
    tree = ast.parse(worker_path.read_text())

    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    forbidden = {m for m in imported_modules if m == "app.main" or m.startswith("app.main.")}
    assert not forbidden, (
        f"app/worker.py imports {sorted(forbidden)} — this would trigger app.main's FastAPI "
        "startup handler's own apply_rls()/apply_mainai_job_runtime_privileges() calls if "
        "anything ever actually starts that app object from within the worker process, "
        "reintroducing the exact privilege-mutation race this test guards against."
    )
