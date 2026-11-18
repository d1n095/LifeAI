#!/usr/bin/env python3
"""Founder Alpha acceptance: run the real startup path and report what actually passed.

This is not a test suite. It answers one question an operator can act on: *if I start MainAI
privately right now, which required capability is verified, which is only configured, and which is
not verified at all?* Each check declares its own evidence kind:

  real         — executed against live infrastructure (PostgreSQL, Redis, the repository itself)
  mocked       — executed, but with the external dependency replaced by a fake (typically the model)
  config_only  — asserted from configuration, not exercised
  not_verified — not executed here at all; the report says so instead of implying success

`ready_for_private_start` is deliberately conservative: it requires every check marked `mandatory`
to be `passed`, and it never flips `first_cycle_go`. Authorization is the founder's, and this script
reports it as such rather than deciding it.

Run:  python backend/scripts/acceptance/founder_alpha_acceptance.py --out acceptance-report.json
      (add --skip-suites for the structural checks only)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"

PASSED, FAILED, SKIPPED = "passed", "failed", "skipped"


class Check:
    def __init__(self, check_id: str, title: str, evidence_kind: str, mandatory: bool, runner):
        self.id = check_id
        self.title = title
        self.evidence_kind = evidence_kind
        self.mandatory = mandatory
        self.runner = runner

    def run(self) -> dict:
        result = {
            "id": self.id,
            "title": self.title,
            "evidence_kind": self.evidence_kind,
            "mandatory": self.mandatory,
        }
        try:
            status, detail = self.runner()
        except Exception as exc:  # a check that crashes is a failed check, never a skipped one
            status, detail = FAILED, f"{type(exc).__name__}: {exc}"
        result["status"] = status
        result["detail"] = detail
        return result


def _settings():
    sys.path.insert(0, str(BACKEND_ROOT))
    from app.config import get_settings

    return get_settings()


# ---------------------------------------------------------------------------------------------
# Structural / infrastructure checks
# ---------------------------------------------------------------------------------------------


def check_config() -> tuple[str, dict]:
    settings = _settings()
    problems = []
    if settings.secret_key in {"", "change-me-in-production", "test-secret-key-for-pytest"}:
        problems.append("SECRET_KEY is a placeholder")
    if not settings.founder_email or "@" not in settings.founder_email:
        problems.append("FOUNDER_EMAIL is not an address")
    if settings.github_write_enabled:
        problems.append("GITHUB_WRITE_ENABLED is on: MainAI could push real branches")
    detail = {
        "environment": settings.environment,
        "github_write_enabled": settings.github_write_enabled,
        "proposal_isolation": settings.mainai_proposal_worktree_isolation,
        "problems": problems,
    }
    return (FAILED if problems else PASSED), detail


def check_postgres() -> tuple[str, dict]:
    import psycopg2

    settings = _settings()
    conn = psycopg2.connect(settings.database_url, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version(), current_database()")
            version, database = cur.fetchone()
    finally:
        conn.close()
    return PASSED, {"server": version.split(" ")[0], "database": database}


def check_migrations() -> tuple[str, dict]:
    import psycopg2

    settings = _settings()
    conn = psycopg2.connect(settings.database_url, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            row = cur.fetchone()
    finally:
        conn.close()
    applied = row[0] if row else None

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"], cwd=str(BACKEND_ROOT), capture_output=True, text=True
    )
    heads = [line.split()[0] for line in result.stdout.splitlines() if line.strip()]
    ok = applied in heads
    return (PASSED if ok else FAILED), {"applied": applied, "heads": heads}


def check_redis() -> tuple[str, dict]:
    import redis

    settings = _settings()
    client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=5)
    pong = client.ping()
    return (PASSED if pong else FAILED), {"ping": bool(pong)}


def _run_script(script: str, *args: str) -> tuple[str, dict]:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / script), "--repo-root", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
    )
    tail = (result.stdout + result.stderr).strip().splitlines()[-6:]
    return (PASSED if result.returncode == 0 else FAILED), {
        "exit_code": result.returncode,
        "output": tail,
    }


def check_policy_lock() -> tuple[str, dict]:
    return _run_script("backend/scripts/security/verify_policy_lock.py")


def check_capabilities() -> tuple[str, dict]:
    return _run_script("backend/scripts/security/verify_agent_capabilities.py")


def check_registry() -> tuple[str, dict]:
    return _run_script("backend/scripts/requirements/verify_registry.py")


def check_dead_seams() -> tuple[str, dict]:
    """Enforced rule only: a settings field nothing reads documents a behavior that does not exist."""
    return _run_script("backend/scripts/requirements/dead_seams.py", "--enforce-unread-settings")


def check_runtime_privileges() -> tuple[str, dict]:
    """The exact verification `app/main.py` performs at startup, run here so that "ready" means
    "this database can actually boot the API" — not just "migrations match the tree".

    Found the hard way: a freshly migrated database passed every other check while
    `uvicorn app.main:app` refused to start, because `mainai_app` had no INSERT/SELECT on the
    mainai_job_* tables. `scripts/security/apply_runtime_privileges.py` is the install step that
    grants them, and nothing in the startup documentation named it.
    """
    import sys as _sys

    backend_root = REPO_ROOT / "backend"
    if str(backend_root) not in _sys.path:
        _sys.path.insert(0, str(backend_root))
    try:
        from app.db import migration_engine
        from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

        # require_complete=True — the same mode app/main.py boots with. A non-fatal mode was tried
        # first and was wrong: it logs drift and returns, so the check reported "passed" on a
        # database where the API could not start at all. A readiness check that cannot fail is
        # decoration.
        apply_mainai_job_runtime_privileges(migration_engine)
        apply_mainai_execution_privileges(migration_engine)
    except Exception as exc:  # noqa: BLE001 - any failure here is a boot blocker worth reporting
        return FAILED, {"error": f"{type(exc).__name__}: {exc}"}
    return PASSED, {"note": "app/main.py's startup privilege policy is satisfiable on this database"}


def check_local_model() -> tuple[str, dict]:
    """Runs the real-inference harness when a local runtime is configured.

    Previously this reported "skipped" even with a live model, so the report could say
    `ready_for_private_start: True` while nothing had ever asked a model a question. The harness is
    now invoked for real, and its three exit codes map to three different truths: 0 the model
    answered, 2 the model is present but broken (a real failure), 3 the runtime or model is absent
    (blocked — reported as not-verified with the exact `ollama pull` instruction).
    """
    configured = bool(os.environ.get("OLLAMA_BASE_URL") or os.environ.get("LOCAL_LLM_BASE_URL"))
    if not configured:
        return SKIPPED, {
            "reason": "no local model endpoint configured in this environment; hardware/model "
            "deployment is NOT verified by this report. Set OLLAMA_BASE_URL to have it run for real."
        }
    try:
        result = subprocess.run(
            [sys.executable, str(BACKEND_ROOT / "scripts" / "acceptance" / "local_model_acceptance.py")],
            capture_output=True,
            text=True,
            timeout=600,
            env={**os.environ},
        )
    except subprocess.TimeoutExpired:
        return FAILED, {"error": "local model acceptance exceeded 600s"}
    tail = (result.stdout or result.stderr).strip().splitlines()[-8:]
    if result.returncode == 0:
        return PASSED, {"exit_code": 0, "output": tail}
    if result.returncode == 3:
        return SKIPPED, {"exit_code": 3, "reason": "runtime or model absent", "output": tail}
    return FAILED, {"exit_code": result.returncode, "output": tail}


def _suite(path: str):
    def runner() -> tuple[str, dict]:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", path, "-q", "-p", "no:cacheprovider"],
            cwd=str(BACKEND_ROOT),
            capture_output=True,
            text=True,
        )
        tail = result.stdout.strip().splitlines()[-3:]
        return (PASSED if result.returncode == 0 else FAILED), {"exit_code": result.returncode, "output": tail}

    return runner


def build_checks(include_suites: bool) -> list[Check]:
    checks = [
        Check("config", "Configuration and dependency validation", "config_only", True, check_config),
        Check("postgres", "PostgreSQL availability", "real", True, check_postgres),
        Check("migrations", "Database schema matches the code's migration head", "real", True, check_migrations),
        Check("redis", "Redis availability", "real", True, check_redis),
        Check("security.policy_lock", "Security controls unmodified and fully covered", "real", True, check_policy_lock),
        Check("security.capabilities", "Agent capabilities match the runtime contract", "real", True, check_capabilities),
        Check("security.runtime_privileges", "Database grants satisfy the API's startup policy", "real", True, check_runtime_privileges),
        Check("requirements.registry", "Requirements registry verifies against the tree", "real", True, check_registry),
        Check("requirements.dead_seams", "No configuration flag that nothing reads", "real", True, check_dead_seams),
        Check("model.local", "Local model answered a real request", "real", False, check_local_model),
    ]
    if include_suites:
        checks.extend(
            [
                Check(
                    "auth.sessions",
                    "Founder authentication and privileged-action enforcement",
                    "real",
                    True,
                    _suite("tests/security/test_session_auth.py"),
                ),
                Check(
                    "auth.rls",
                    "Row-level security isolation between owners",
                    "real",
                    True,
                    _suite("tests/security/test_rls_isolation.py"),
                ),
                Check("knowledge.ingestion", "Knowledge ingestion and retrieval", "real", True, _suite("tests/backend/rag")),
                Check(
                    "memory.persistence",
                    "Persistent memory across a new session",
                    "real",
                    True,
                    _suite("tests/backend/test_founder_alpha_runtime_flow.py"),
                ),
                Check(
                    "agent.execution",
                    "Controlled agent execution and failure recovery",
                    "mocked",
                    True,
                    _suite("tests/backend/test_runtime_failure_recovery.py"),
                ),
                Check(
                    "backup.restore",
                    "Backup and restore with real PostgreSQL",
                    "real",
                    True,
                    _suite("tests/backend/test_database_recovery.py"),
                ),
                Check(
                    "security.controls",
                    "Guardian security gates behave and can fail",
                    "real",
                    True,
                    _suite("tests/security/test_security_controls.py"),
                ),
            ]
        )
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="acceptance-report.json")
    parser.add_argument("--skip-suites", action="store_true", help="structural checks only (fast)")
    args = parser.parse_args(argv)

    checks = [check.run() for check in build_checks(include_suites=not args.skip_suites)]

    mandatory = [c for c in checks if c["mandatory"]]
    failed = [c for c in mandatory if c["status"] != PASSED]
    report = {
        "version": 1,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "git_commit": _git_commit(),
        "checks": checks,
        "summary": {
            "total": len(checks),
            "passed": sum(1 for c in checks if c["status"] == PASSED),
            "failed": sum(1 for c in checks if c["status"] == FAILED),
            "skipped": sum(1 for c in checks if c["status"] == SKIPPED),
            "mandatory_failed": [c["id"] for c in failed],
            "real_evidence": sum(1 for c in checks if c["evidence_kind"] == "real"),
        },
        "ready_for_private_start": not failed,
        "first_cycle_go": False,
        "authorization": "founder_authorization_required",
        "note": "Individual modules passing their tests is not readiness. Read the per-check evidence_kind: "
        "config_only and not_verified checks are not proof of working behavior.",
    }
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    for check in checks:
        print(f"[{check['status']:7}] {check['id']:28} ({check['evidence_kind']}) {check['title']}")
    print()
    print(f"ready_for_private_start: {report['ready_for_private_start']}")
    if failed:
        print(f"blocking: {', '.join(c['id'] for c in failed)}")
    print(f"report: {args.out}")
    return 0 if report["ready_for_private_start"] else 1


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


if __name__ == "__main__":
    raise SystemExit(main())
