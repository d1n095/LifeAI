#!/usr/bin/env python3
"""Exact-SHA staging verification for an imported LifeAI delivery.

The build runs in a detached temporary worktree and reuses the repository's existing
pytest security suites. It never applies migrations to a non-test database, never starts a
service and never deploys anything. Environmental prerequisites are reported as BLOCKED,
not silently converted into a code PASS or FAIL.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.integration.common import (  # noqa: E402
    PipelineError,
    git_output,
    guard_redis,
    guard_test_database,
    redact_url,
    resolve_commit,
    run,
    status_exit,
    worktree_add,
    worktree_remove,
    write_json,
)

MIGRATION_DIR = Path("backend/alembic/versions")
ENV_ERROR = re.compile(
    r"(could not connect|connection refused|operationalerror|"
    r"no module named ['\"]?pytest|no module named ['\"]?psycopg2|"
    r"command not found|no such file or directory: ['\"]?npm|"
    r"cannot find module ['\"]?next|missing dependency|database .* does not exist)",
    re.IGNORECASE,
)


def _redact_output(text: str) -> str:
    text = re.sub(r"(postgresql|redis)://[^\s\"']+", r"\1://***", text)
    return text[-3000:]


def _migration_item(path: Path) -> dict[str, Any]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        return {"path": str(path), "error": str(exc)}
    values: dict[str, Any] = {}
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
            try:
                values[target.id] = ast.literal_eval(value)  # type: ignore[arg-type]
            except (ValueError, TypeError):
                values[target.id] = None
    if not isinstance(values.get("revision"), str):
        return {"path": str(path), "error": "missing string revision"}
    down = values.get("down_revision")
    if down is None:
        downs: list[str] = []
    elif isinstance(down, str):
        downs = [down]
    elif isinstance(down, (tuple, list)) and all(isinstance(item, str) for item in down):
        downs = list(down)
    else:
        return {"path": str(path), "error": "invalid down_revision"}
    return {"path": str(path), "revision": values["revision"], "down_revision": downs}


def check_migrations(worktree: Path | str) -> dict[str, Any]:
    root = Path(worktree) / MIGRATION_DIR
    if not root.is_dir():
        return {"status": "FAIL", "error": f"missing migration directory: {root}"}
    parsed: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.py")):
        item = _migration_item(path)
        if "error" in item:
            errors.append(item)
        else:
            parsed.append(item)
    revisions: dict[str, list[str]] = {}
    for item in parsed:
        revisions.setdefault(item["revision"], []).append(item["path"])
    duplicates = {revision: paths for revision, paths in revisions.items() if len(paths) > 1}
    down_values = {down for item in parsed for down in item["down_revision"]}
    heads = sorted(set(revisions) - down_values)
    missing = sorted(
        down for item in parsed for down in item["down_revision"] if down not in revisions
    )
    # Cycle detection over the revision graph.
    graph = {item["revision"]: item["down_revision"] for item in parsed}
    visiting: set[str] = set()
    visited: set[str] = set()
    cycles: list[str] = []

    def visit(node: str) -> None:
        if node in visiting:
            cycles.append(node)
            return
        if node in visited or node not in graph:
            return
        visiting.add(node)
        for parent in graph[node]:
            visit(parent)
        visiting.remove(node)
        visited.add(node)

    for revision in graph:
        visit(revision)
    failed = bool(errors or duplicates or missing or cycles or len(heads) != 1)
    return {
        "status": "FAIL" if failed else "PASS",
        "files": len(parsed),
        "heads": heads,
        "duplicate_revisions": duplicates,
        "missing_parents": missing,
        "cycles": sorted(set(cycles)),
        "errors": errors,
    }


def check_lockfiles(worktree: Path | str) -> dict[str, Any]:
    root = Path(worktree)
    checks: dict[str, Any] = {}
    frontend = root / "frontend"
    package_json = frontend / "package.json"
    package_lock = frontend / "package-lock.json"
    try:
        package = json.loads(package_json.read_text(encoding="utf-8"))
        lock = json.loads(package_lock.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "FAIL", "frontend": f"cannot read package files: {exc}"}
    lock_version = lock.get("lockfileVersion")
    root_package = (lock.get("packages") or {}).get("")
    if not isinstance(lock_version, int) or lock_version < 2 or not isinstance(root_package, dict):
        return {"status": "FAIL", "frontend": "package-lock.json must be lockfileVersion >= 2"}
    mismatches: list[str] = []
    for section in ("dependencies", "devDependencies"):
        declared = package.get(section) or {}
        locked = root_package.get(section) or {}
        for name in declared:
            if name not in locked:
                mismatches.append(f"{section}:{name}")
    checks["frontend"] = {
        "status": "PASS" if not mismatches else "FAIL",
        "lockfile_version": lock_version,
        "missing_locked_dependencies": mismatches,
    }

    requirements = root / "backend" / "requirements.txt"
    requirements_dev = root / "backend" / "requirements-dev.txt"
    backend_issues: list[str] = []
    names: dict[str, str] = {}
    for path in (requirements, requirements_dev):
        if not path.is_file():
            backend_issues.append(f"missing {path.name}")
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-r "):
                continue
            match = re.match(r"^([A-Za-z0-9_.-]+)", line)
            if not match:
                backend_issues.append(f"{path.name}:{line_number}: unparsable requirement")
                continue
            name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
            if name in names and names[name] != line:
                backend_issues.append(f"{path.name}:{line_number}: conflicting duplicate {name}")
            names[name] = line
    checks["backend"] = {
        "status": "PASS" if not backend_issues else "FAIL",
        "requirements": len(names),
        "issues": backend_issues,
    }
    return {
        "status": "PASS" if all(item["status"] == "PASS" for item in checks.values()) else "FAIL",
        **checks,
    }


def _test_env(
    worktree: Path,
    *,
    database_url: str | None,
    app_database_url: str | None,
    redis_url: str | None,
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": ".",
            "PYTHONUNBUFFERED": "1",
            "SECRET_KEY": env.get("SECRET_KEY", "v8-staging-test-secret"),
            "FOUNDER_EMAIL": env.get("FOUNDER_EMAIL", "founder@lifeos.local"),
            "FOUNDER_PASSWORD": env.get("FOUNDER_PASSWORD", "TestFounderPassword123!"),
            "FRONTEND_ORIGINS": "http://127.0.0.1:3020",
            "PUBLIC_APP_URL": "http://127.0.0.1:3020",
            "ENABLE_SCHEDULED_CLEANUP": "false",
            "OPENAI_API_KEY": env.get("OPENAI_API_KEY", "fake-key-for-staging-tests"),
            "STORAGE_ROOT": str(Path(tempfile.mkdtemp(prefix="lifeai-v8-staging-storage-"))),
        }
    )
    if database_url:
        env["DATABASE_URL"] = database_url
        env["LIFEAI_TEST_DATABASE_NAME"] = urlparse(database_url).path.lstrip("/")
    if app_database_url:
        env["APP_DATABASE_URL"] = app_database_url
    if redis_url:
        env["REDIS_URL"] = redis_url
    return env


def _pytest_result(
    worktree: Path,
    command: list[str],
    *,
    env: dict[str, str],
    junit: Path | None,
    timeout: int,
) -> dict[str, Any]:
    started = time.monotonic()
    full = command + ([f"--junitxml={junit}"] if junit else [])
    result = run(full, cwd=worktree / "backend", env=env, timeout=timeout)
    duration = round(time.monotonic() - started, 3)
    output = f"{result.stdout}\n{result.stderr}"
    status = "PASS" if result.returncode == 0 else "FAIL"
    if result.returncode != 0 and ENV_ERROR.search(output):
        status = "BLOCKED"
    junit_info: dict[str, Any] | None = None
    if junit and junit.is_file():
        try:
            import xml.etree.ElementTree as ET

            root = ET.parse(junit).getroot()
            suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
            junit_info = {
                "tests": sum(int(item.attrib.get("tests", 0)) for item in suites),
                "failures": sum(int(item.attrib.get("failures", 0)) for item in suites),
                "errors": sum(int(item.attrib.get("errors", 0)) for item in suites),
                "skipped": sum(int(item.attrib.get("skipped", 0)) for item in suites),
            }
        except (OSError, ValueError, ET.ParseError):
            junit_info = None
    return {
        "status": status,
        "command": " ".join(full),
        "returncode": result.returncode,
        "duration_seconds": duration,
        "junit": str(junit) if junit else None,
        "junit_totals": junit_info,
        "output_tail": _redact_output(output),
    }


def check_security(
    worktree: Path,
    *,
    database_url: str | None,
    app_database_url: str | None,
    redis_url: str | None,
    junit_out: Path | None,
    timeout: int,
) -> dict[str, Any]:
    env = _test_env(
        worktree,
        database_url=database_url,
        app_database_url=app_database_url,
        redis_url=redis_url,
    )
    commands = [
        (
            "r15_successor",
            [sys.executable, "-m", "pytest", "tests/security_r15_successor", "-q", "--confcutdir=tests/security_r15_successor"],
        ),
        (
            "r15_egress",
            [sys.executable, "-m", "pytest", "tests/security_r15_egress", "-q", "--confcutdir=tests/security_r15_egress"],
        ),
    ]
    results: dict[str, Any] = {}
    for name, command in commands:
        results[name] = _pytest_result(
            worktree, command, env=env, junit=None, timeout=timeout
        )
    db_command = [
        sys.executable,
        "-m",
        "pytest",
        "tests/backend/providers/test_r15_integration_security.py",
        "tests/backend/test_rls_policy_registry.py",
        "-q",
    ]
    if not database_url:
        results["r15_db_rls"] = {
            "status": "BLOCKED",
            "command": " ".join(db_command),
            "output_tail": "DATABASE_URL is not set; refusing to claim the DB/RLS suite ran",
        }
    else:
        results["r15_db_rls"] = _pytest_result(
            worktree,
            db_command,
            env=env,
            junit=junit_out,
            timeout=max(timeout, 600),
        )
    return {
        "status": "PASS"
        if all(item["status"] == "PASS" for item in results.values())
        else ("FAIL" if any(item["status"] == "FAIL" for item in results.values()) else "BLOCKED"),
        "suites": results,
    }


def check_authorization_egress(worktree: Path, security: dict[str, Any]) -> dict[str, Any]:
    required = [
        "backend/app/egress_policy/__init__.py",
        "backend/app/egress_policy/service.py",
        "backend/app/routers/auth.py",
        "backend/app/rls.py",
    ]
    missing = [path for path in required if not (Path(worktree) / path).is_file()]
    if missing:
        return {"status": "FAIL", "missing": missing}
    if security["status"] != "PASS":
        return {"status": security["status"], "detail": "security suites did not pass"}
    return {"status": "PASS", "files": required}


def check_backend(worktree: Path, *, timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    result = run(
        [sys.executable, "-m", "compileall", "-q", "app"],
        cwd=Path(worktree) / "backend",
        timeout=timeout,
    )
    return {
        "status": "PASS" if result.returncode == 0 else "FAIL",
        "returncode": result.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "output_tail": _redact_output(f"{result.stdout}\n{result.stderr}"),
    }


def check_frontend(worktree: Path, *, run_build: bool, timeout: int) -> dict[str, Any]:
    root = Path(worktree) / "frontend"
    if not (root / "package.json").is_file() or not (root / "package-lock.json").is_file():
        return {"status": "FAIL", "detail": "frontend package/lockfile missing"}
    if not (root / "node_modules").is_dir():
        return {
            "status": "BLOCKED",
            "detail": "frontend/node_modules is absent; run npm ci before claiming frontend compatibility",
        }
    if not run_build:
        return {"status": "BLOCKED", "detail": "frontend build explicitly skipped"}
    env = os.environ.copy()
    env["NEXT_TELEMETRY_DISABLED"] = "1"
    started = time.monotonic()
    result = run(["npm", "run", "build"], cwd=root, env=env, timeout=timeout)
    output = f"{result.stdout}\n{result.stderr}"
    status = "PASS" if result.returncode == 0 else "FAIL"
    if result.returncode != 0 and ENV_ERROR.search(output):
        status = "BLOCKED"
    return {
        "status": status,
        "returncode": result.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "output_tail": _redact_output(output),
    }


def verdict(checks: dict[str, dict[str, Any]]) -> str:
    values = [item.get("status", "FAIL") for item in checks.values()]
    if "FAIL" in values:
        return "FAIL"
    if "BLOCKED" in values:
        return "BLOCKED"
    return "PASS"


def build_staging(
    *,
    repo: Path | str,
    sha: str,
    out: Path | str,
    database_url: str | None = None,
    app_database_url: str | None = None,
    redis_url: str | None = None,
    junit_out: Path | str | None = None,
    run_frontend_build: bool = True,
    keep_worktree: bool = False,
    timeout: int = 600,
) -> dict[str, Any]:
    repo_path = Path(repo).resolve()
    commit = resolve_commit(repo_path, sha)
    if database_url:
        guard_test_database(database_url, "DATABASE_URL")
    if app_database_url:
        guard_test_database(app_database_url, "APP_DATABASE_URL")
    if redis_url:
        guard_redis(redis_url, "REDIS_URL")

    temp_root = Path(tempfile.mkdtemp(prefix="lifeai-v8-staging-"))
    worktree = temp_root / "repo"
    checks: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] | None = None
    try:
        worktree_add(repo_path, worktree, commit)
        if git_output(worktree, "rev-parse", "HEAD") != commit:
            raise PipelineError("staging worktree HEAD is not the requested SHA", "FAIL")
        checks["checkout"] = {"status": "PASS", "sha": commit, "worktree": str(worktree)}
        checks["lockfiles"] = check_lockfiles(worktree)
        checks["migrations"] = check_migrations(worktree)
        checks["backend"] = check_backend(worktree, timeout=timeout)
        resolved_junit = Path(junit_out).resolve() if junit_out else None
        if resolved_junit is not None:
            resolved_junit.parent.mkdir(parents=True, exist_ok=True)
        checks["security"] = check_security(
            worktree,
            database_url=database_url,
            app_database_url=app_database_url,
            redis_url=redis_url,
            junit_out=resolved_junit,
            timeout=timeout,
        )
        checks["authorization_egress"] = check_authorization_egress(worktree, checks["security"])
        checks["frontend"] = check_frontend(
            worktree, run_build=run_frontend_build, timeout=max(timeout, 600)
        )
        status = verdict(checks)
        result = {
            "status": status,
            "commit": commit,
            "base_worktree": str(worktree),
            "checks": checks,
            "database_url": redact_url(database_url),
            "app_database_url": redact_url(app_database_url),
            "redis_url": redact_url(redis_url),
            "migrations_applied_to_production": False,
            "note": "Security tests may create/drop their own disposable test database; this command never migrates or writes production data.",
        }
    finally:
        if not keep_worktree:
            try:
                worktree_remove(repo_path, worktree)
            finally:
                shutil.rmtree(temp_root, ignore_errors=True)
    if result is None:
        raise PipelineError("staging build produced no result", "FAIL")
    write_json(out, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--sha", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--app-database-url", default=os.environ.get("APP_DATABASE_URL"))
    parser.add_argument("--redis-url", default=os.environ.get("REDIS_URL"))
    parser.add_argument("--junit-out")
    parser.add_argument("--skip-frontend-build", action="store_true")
    parser.add_argument("--keep-worktree", action="store_true")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args(argv)
    try:
        result = build_staging(
            repo=args.repo,
            sha=args.sha,
            out=args.out,
            database_url=args.database_url,
            app_database_url=args.app_database_url,
            redis_url=args.redis_url,
            junit_out=args.junit_out,
            run_frontend_build=not args.skip_frontend_build,
            keep_worktree=args.keep_worktree,
            timeout=args.timeout,
        )
    except PipelineError as exc:
        result = {"status": exc.status, "error": str(exc), "detail": exc.detail}
        write_json(args.out, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return status_exit(result["status"])


if __name__ == "__main__":
    raise SystemExit(main())
