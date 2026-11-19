#!/usr/bin/env python3
"""One idempotent command that puts a database into the state the API can actually boot from.

Why this exists
---------------
The container path already had the right boot order (`backend/docker-entrypoint.sh`):

    ensure_app_role  ->  alembic upgrade head  ->  apply_runtime_privileges  ->  app

Every non-container installation path re-implemented parts of that sequence by hand. The
reported Founder Alpha startup defect came from exactly this: a freshly migrated database
passed the migration check, then `uvicorn app.main:app` refused to start because `mainai_app`
had no INSERT/SELECT on `mainai_job_events`. The missing piece was the role grant that
`ensure_app_role.py` performs *before* the migration, which is what makes tables created by
the migration readable/writable by the runtime role at all.

This script is that sequence, in that order, with a verification step that fails closed. It is
safe to run repeatedly: every step is idempotent, and `--verify-only` touches nothing.

What each step is for
---------------------
1. ensure_app_role.py
   Creates/updates `mainai_app`, grants the narrowed DML floor on every existing table, and —
   critically — sets `ALTER DEFAULT PRIVILEGES` so tables created by the *following* migration
   are granted to the runtime role too. Must run BEFORE `alembic upgrade head`.

2. alembic upgrade head
   Schema only. Runs as the admin/owner role (`DATABASE_URL`), not the runtime role.

3. apply_runtime_privileges.py
   Re-applies and verifies the narrowed S1A privilege policy on the objects the migration just
   created. Must run AFTER the migration, and its failure is fatal: the app must never serve
   traffic against an unverified privilege state.

4. Verify the boot contract
   The same checks `app/main.py`'s startup performs (MainAI job + execution privilege policies),
   plus a direct probe as the runtime role: SELECT on every public table, INSERT on the core
   write tables, and no TRUNCATE anywhere. This is the check that would have caught the reported
   defect before a human tried to start the server.

Usage
-----
    DATABASE_URL=... APP_DATABASE_URL=... python backend/scripts/setup/bootstrap_database.py
    DATABASE_URL=... APP_DATABASE_URL=... python backend/scripts/setup/bootstrap_database.py --verify-only

Exit codes: 0 ready, 1 verification/step failure, 2 unsafe configuration (missing credentials).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent
POLICY_DIR = BACKEND_ROOT / "scripts" / "security"
OPS_DIR = BACKEND_ROOT / "scripts" / "ops"
APP_ROLE = "mainai_app"

# Tables the API writes to on a normal founder session. SELECT is required on every table;
# INSERT only on the ones a request actually creates rows in. Deliberately a named list rather
# than "INSERT on everything": the audit log and the migration bookkeeping are tables where a
# blanket INSERT check would be wrong.
CORE_WRITE_TABLES = (
    "users",
    "conversations",
    "messages",
    "documents",
    "document_chunks",
    "projects",
    "mainai_jobs",
    "mainai_goals",
    "mainai_tasks",
)

# `audit_logs` is append-only by trigger (migration 0079): INSERT/SELECT yes, UPDATE only for
# anonymisation, DELETE never. Probed separately so a policy change is visible here.
AUDIT_TABLE = "audit_logs"

# Tables that are write-once *by design*, so "can this role write?" is the wrong question for
# them. `mainai_job_events` is append-only: app/rls.py's lockdown and migration 0027's integrity
# policy require `mainai_app` to hold SELECT and INSERT and NOTHING ELSE.
#
# This was a real install defect, found by running the bootstrap twice: the first run passed
# `verify_grants` because it ran before `verify_boot` re-asserted the append-only lockdown, and
# every subsequent `--verify-only` on the same, correctly installed database failed with
# "runtime role lacks DML on core write tables: {'mainai_job_events': ['UPDATE', 'DELETE']}".
# A readiness check that fails on a healthy install is as damaging as one that passes on a broken
# one, so the expectation now matches the policy the API enforces at startup.
APPEND_ONLY_TABLES: dict[str, tuple[str, ...]] = {
    "mainai_job_events": ("SELECT", "INSERT"),
}

# Tables the runtime role deliberately cannot touch directly. These are not gaps: each is a
# reviewed design decision, and the verification below asserts the exemption still holds rather
# than silently skipping the table.
DIRECT_ACCESS_EXEMPT: dict[str, str] = {
    "alembic_version": (
        "migration bookkeeping; the runtime role has no privileges on it (SFA-1), so a SQL "
        "injection reachable as mainai_app cannot rewrite the schema version"
    ),
    "storage_deletion_tasks": (
        "reached only through SECURITY DEFINER functions; migration 0022 deliberately gives "
        "mainai_app ZERO direct privileges, so a compromised request path cannot queue or "
        "alter physical-deletion work"
    ),
}


class BootstrapError(RuntimeError):
    """A step failed. Carries the step id so the report is actionable."""

    def __init__(self, step: str, message: str):
        super().__init__(f"{step}: {message}")
        self.step = step


@dataclass
class Step:
    id: str
    description: str
    fn: Callable[[], dict]
    result: dict = field(default_factory=dict)
    seconds: float = 0.0


def _run(cmd: list[str], *, step: str, env: dict | None = None) -> str:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(BACKEND_ROOT),
            capture_output=True,
            text=True,
            env={**os.environ, **(env or {})},
            timeout=900,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BootstrapError(step, f"{type(exc).__name__}: {exc}") from exc
    elapsed = time.monotonic() - started
    output = (completed.stdout or "").strip()
    if completed.returncode != 0:
        tail = "\n".join(((completed.stderr or completed.stdout or "").strip().splitlines())[-12:])
        raise BootstrapError(step, f"exit {completed.returncode} after {elapsed:.1f}s:\n{tail}")
    return output


def _require_env() -> tuple[str, str]:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    app_url = os.environ.get("APP_DATABASE_URL", "").strip()
    if not database_url:
        raise BootstrapError("config", "DATABASE_URL is not set (admin/owner connection used for migrations)")
    # APP_DATABASE_URL can be derived from MAINAI_APP_PASSWORD in the container path; the
    # installer path is expected to have it explicit. Never guess a password.
    if not app_url and not os.environ.get("MAINAI_APP_PASSWORD"):
        raise BootstrapError(
            "config",
            "neither APP_DATABASE_URL nor MAINAI_APP_PASSWORD is set — the runtime role's "
            "credential is required to verify the boot contract",
        )
    return database_url, app_url


def _derive_app_url(database_url: str, app_password: str) -> str:
    """Same derivation as ensure_app_role.py --derive-only (imported, not re-implemented)."""
    sys.path.insert(0, str(POLICY_DIR))
    from ensure_app_role import _derive_app_database_url  # noqa: WPS433 (deliberate local import)

    return _derive_app_database_url(database_url, app_password)


def _app_database_url(database_url: str) -> str:
    explicit = os.environ.get("APP_DATABASE_URL", "").strip()
    if explicit:
        return explicit
    password = os.environ.get("MAINAI_APP_PASSWORD")
    if not password:
        raise BootstrapError("config", "APP_DATABASE_URL is not set")
    return _derive_app_url(database_url, password)


# ---------------------------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------------------------


def step_ensure_app_role() -> dict:
    """Create/update the runtime role and its default privileges (BEFORE the migration)."""
    if not os.environ.get("APP_DATABASE_URL", "").strip() and not os.environ.get("MAINAI_APP_PASSWORD"):
        raise BootstrapError("ensure_app_role", "MAINAI_APP_PASSWORD is required to provision the runtime role")
    # ensure_app_role writes APP_DATABASE_URL to $RENDER_ENV_FILE for the calling shell; this
    # process already computes it itself, so it only needs a scratch file it will not read.
    scratch = BACKEND_ROOT / ".bootstrap_app_env.tmp"
    output = _run(
        [sys.executable, str(POLICY_DIR / "ensure_app_role.py")],
        step="ensure_app_role",
        env={"RENDER_ENV_FILE": str(scratch)},
    )
    scratch.unlink(missing_ok=True)
    return {"output": output.splitlines()[-3:]}


def step_migrate() -> dict:
    output = _run([sys.executable, "-m", "alembic", "upgrade", "head"], step="alembic upgrade head")
    return {"output": output.splitlines()[-3:]}


def step_apply_privileges() -> dict:
    output = _run(
        [sys.executable, str(POLICY_DIR / "apply_runtime_privileges.py")],
        step="apply_runtime_privileges",
    )
    return {"output": output.splitlines()[-3:]}


def _connect(url: str):
    import psycopg2  # imported lazily so --help works without the backend venv

    return psycopg2.connect(url, connect_timeout=10)


def _table_names(cur) -> list[str]:
    cur.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    )
    return [row[0] for row in cur.fetchall()]


def _missing_privileges(cur, table: str, privileges: tuple[str, ...]) -> list[str]:
    missing = []
    for privilege in privileges:
        cur.execute(
            "SELECT has_table_privilege(current_user, %s, %s)",
            (f"public.{table}", privilege),
        )
        if not cur.fetchone()[0]:
            missing.append(privilege)
    return missing


def verify_runtime_grants(app_url: str) -> dict:
    """Read-only probe as the actual runtime role. Fails closed on the reported defect."""
    conn = _connect(app_url)
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            cur.execute("SELECT current_user")
            (role,) = cur.fetchone()
            if role != APP_ROLE:
                raise BootstrapError(
                    "verify_grants",
                    f"APP_DATABASE_URL authenticates as {role!r}, expected {APP_ROLE!r}; "
                    "row-level security would not be enforced for a superuser",
                )
            tables = _table_names(cur)
            if not tables:
                raise BootstrapError("verify_grants", "database has no tables — the migration did not run")

            select_missing = {}
            for table in tables:
                if table in DIRECT_ACCESS_EXEMPT:
                    continue
                missing = _missing_privileges(cur, table, ("SELECT",))
                if missing:
                    select_missing[table] = missing
            if select_missing:
                raise BootstrapError("verify_grants", f"runtime role cannot SELECT: {select_missing}")

            insert_missing = {}
            for table in CORE_WRITE_TABLES:
                if table not in tables:
                    continue
                missing = _missing_privileges(cur, table, ("INSERT", "UPDATE", "DELETE"))
                if missing:
                    insert_missing[table] = missing
            if insert_missing:
                raise BootstrapError(
                    "verify_grants",
                    f"runtime role lacks DML on core write tables: {insert_missing} — this is the "
                    "exact class of failure that made a freshly migrated database unable to boot",
                )

            for table, allowed in APPEND_ONLY_TABLES.items():
                if table not in tables:
                    continue
                missing = _missing_privileges(cur, table, allowed)
                if missing:
                    raise BootstrapError("verify_grants", f"runtime role lacks {missing} on append-only {table}")
                forbidden = sorted(_granted_privileges(cur, table) - set(allowed))
                if forbidden:
                    raise BootstrapError(
                        "verify_grants",
                        f"append-only {table} must grant only {list(allowed)}, but the runtime role "
                        f"also holds {forbidden} — app/rls.py revokes these on every boot; an install "
                        "that leaves them granted has broken the append-only guarantee",
                    )

            if AUDIT_TABLE in tables:
                if _missing_privileges(cur, AUDIT_TABLE, ("INSERT", "SELECT")):
                    raise BootstrapError("verify_grants", f"{AUDIT_TABLE} must accept appends and reads")
                # Erasure-safe, never destructive: the trigger allows UPDATE only for
                # anonymisation and refuses DELETE. The grant must still exist for the former.
                if "DELETE" in _granted_privileges(cur, AUDIT_TABLE):
                    raise BootstrapError("verify_grants", f"{AUDIT_TABLE} must never grant DELETE")

            # The exemptions are only legitimate while the runtime role truly has nothing on
            # them. A future migration that granted access "to make a query work" would be a
            # silent boundary change; this check makes it fail the install instead.
            for table, reason in DIRECT_ACCESS_EXEMPT.items():
                if table in tables and _granted_privileges(cur, table):
                    raise BootstrapError(
                        "verify_grants",
                        f"{table} must have no direct runtime privileges ({reason}), but grants are "
                        f"{sorted(_granted_privileges(cur, table))}",
                    )

            truncate_granted = [
                table
                for table in tables
                if table not in DIRECT_ACCESS_EXEMPT and _has_privilege(cur, table, "TRUNCATE")
            ]
            if truncate_granted:
                raise BootstrapError(
                    "verify_grants",
                    f"runtime role can TRUNCATE (RLS does not apply to it): {truncate_granted}",
                )

            return {
                "role": role,
                "tables_checked": len(tables),
                "select_ok": True,
                "core_write_tables_ok": [t for t in CORE_WRITE_TABLES if t in tables],
                "audit_append_only": AUDIT_TABLE in tables,
                "truncate_denied": True,
            }
    finally:
        conn.close()


def _has_privilege(cur, table: str, privilege: str) -> bool:
    cur.execute("SELECT has_table_privilege(current_user, %s, %s)", (f"public.{table}", privilege))
    return bool(cur.fetchone()[0])


def _granted_privileges(cur, table: str) -> set[str]:
    """Privileges the *runtime role itself* holds on this table.

    Scoped to `current_user`: `information_schema.role_table_grants` also lists rows for other
    enabled roles, and counting the owner among them made an "is this table exempt?" check
    depend on which roles the session happened to be allowed to assume.
    """
    cur.execute(
        "SELECT privilege_type FROM information_schema.role_table_grants "
        "WHERE table_schema = 'public' AND table_name = %s AND grantee = current_user",
        (table,),
    )
    return {row[0].upper() for row in cur.fetchall()}


def verify_s1a_policy() -> dict:
    output = _run(
        [sys.executable, str(POLICY_DIR / "apply_runtime_privileges.py"), "--verify-only"],
        step="verify_s1a_policy",
    )
    return {"output": output.splitlines()[-3:]}


def verify_schema_head() -> dict:
    """The database's alembic revision must be the tree's single head."""
    sys.path.insert(0, str(OPS_DIR))
    from db_recovery import alembic_revision, migration_graph  # noqa: WPS433

    graph = migration_graph()
    if len(graph["heads"]) != 1:
        raise BootstrapError("verify_schema", f"migration tree has {len(graph['heads'])} heads: {graph['heads']}")
    head = graph["heads"][0]
    conn = _connect(os.environ["DATABASE_URL"])
    try:
        revision = alembic_revision(conn)
    finally:
        conn.close()
    if revision != head:
        raise BootstrapError("verify_schema", f"database is at {revision!r}, tree head is {head!r}")
    return {"revision": revision, "heads": graph["heads"]}


def verify_boot_contract() -> dict:
    """The same privilege policies `app/main.py` asserts at startup, run pre-boot.

    `app/main.py` calls these with their default `require_complete=True`; using the same mode
    here means a green bootstrap is a claim about the real boot path, not a weaker shadow of it.
    """
    sys.path.insert(0, str(BACKEND_ROOT))
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)
    return {"mainai_job_runtime_privileges": "verified", "mainai_execution_privileges": "verified"}


# ---------------------------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------------------------


def bootstrap(*, verify_only: bool = False) -> dict:
    database_url, _ = _require_env()
    app_url = _app_database_url(database_url)

    steps: list[Step] = []
    if not verify_only:
        steps = [
            Step("ensure_app_role", "Provision the runtime role and default table grants", step_ensure_app_role),
            Step("migrate", "Apply migrations as the owner role", step_migrate),
            Step("apply_privileges", "Apply and verify the narrowed S1A privilege policy", step_apply_privileges),
        ]
    steps += [
        Step("verify_schema", "Database revision is the tree's single head", verify_schema_head),
        # The boot contract is applied BEFORE the grants are verified, not after.
        #
        # `ensure_app_role` grants a broad DML floor so tables created by the following migration
        # are usable at all; `app/main.py` then narrows that floor on every start (the append-only
        # job-event log keeps SELECT/INSERT only). Verifying grants first and enforcing the boot
        # contract last made the two halves of one install disagree: the install run passed, and
        # every later `--verify-only` on that same database failed because the narrow policy had
        # by then been applied. A freshly installed database now ends in the state the API
        # enforces, and the report order says so.
        Step("verify_boot", "app/main.py's startup privilege contract is satisfiable", verify_boot_contract),
        Step("verify_grants", "Runtime role can read everything and write where it must", lambda: verify_runtime_grants(app_url)),
        Step("verify_s1a", "S1A privilege policy verifies read-only", verify_s1a_policy),
    ]

    started = time.monotonic()
    for step in steps:
        began = time.monotonic()
        step.result = step.fn()
        step.seconds = round(time.monotonic() - began, 2)

    return {
        "tool": "mainai-db-bootstrap",
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": "verify-only" if verify_only else "bootstrap",
        "seconds": round(time.monotonic() - started, 2),
        "steps": [
            {"id": s.id, "description": s.description, "seconds": s.seconds, "result": s.result}
            for s in steps
        ],
        "ready": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verify-only", action="store_true", help="mutate nothing; only prove the DB can boot")
    parser.add_argument("--out", help="write the JSON report here")
    args = parser.parse_args(argv)

    try:
        report = bootstrap(verify_only=args.verify_only)
    except BootstrapError as exc:
        report = {
            "tool": "mainai-db-bootstrap",
            "version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "mode": "verify-only" if args.verify_only else "bootstrap",
            "ready": False,
            "failed_step": exc.step,
            "error": str(exc),
        }
        print(f"FAILED [{exc.step}]: {exc}", file=sys.stderr)
        if args.out:
            Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return 2 if exc.step == "config" else 1

    for step in report["steps"]:
        print(f"[ok {step['seconds']:5.1f}s] {step['id']}: {step['description']}")
    print("database is ready for `uvicorn app.main:app`.")
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
