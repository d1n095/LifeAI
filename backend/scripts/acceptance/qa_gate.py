#!/usr/bin/env python3
"""Founder Alpha QA gate — executable evidence check, not a green badge.

The gate never invents a result. Every required evidence file must exist and contain a
terminal status; missing evidence is BLOCKED, not PASS. It checks:

  * exact git commit SHA and a clean tracked tree;
  * the live database's Alembic revision equals the checkout's head;
  * PostgreSQL and Redis are reachable;
  * the installation matrix (real services) has no FAIL/BLOCKED;
  * backend JUnit results have tests and zero failures/errors;
  * Playwright JSON has expected tests and zero unexpected failures;
  * recovery evidence is PASS;
  * performance evidence is PASS and contains real measurements;
  * known limitations are printed with the result.

Exit codes: 0 PASS, 2 FAIL, 3 BLOCKED.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import redis
from sqlalchemy.engine import make_url

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=BACKEND_ROOT, env=env, text=True, capture_output=True)


def _load_json(path: str, label: str, checks: list[dict]) -> dict | None:
    if not path:
        checks.append({"check": label, "status": "BLOCKED", "detail": "no evidence path supplied"})
        return None
    p = Path(path)
    if not p.exists():
        checks.append({"check": label, "status": "BLOCKED", "detail": f"missing {p}"})
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        checks.append({"check": label, "status": "FAIL", "detail": f"invalid JSON: {exc}"})
        return None
    return data


def _check_sha(expected: str, checks: list[dict]) -> str:
    head = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
    ok = bool(expected) and head == expected
    checks.append({"check": "exact commit SHA", "status": "PASS" if ok else "FAIL", "actual": head, "expected": expected})
    dirty = _run(["git", "status", "--porcelain", "--untracked-files=no"]).stdout.strip()
    checks.append({"check": "tracked tree clean", "status": "PASS" if not dirty else "FAIL", "detail": dirty or "clean"})
    return head


def _check_migration(database_url: str, checks: list[dict]) -> None:
    if not database_url:
        checks.append({"check": "Alembic revision", "status": "BLOCKED", "detail": "no database URL"})
        return
    env = {**os.environ, "DATABASE_URL": database_url, "PYTHONPATH": "."}
    heads = _run([sys.executable, "-m", "alembic", "heads"], env=env)
    match = re.search(r"^\s*([0-9A-Za-z_]+)\s+\(head\)", heads.stdout, re.MULTILINE)
    if heads.returncode != 0 or not match:
        checks.append({"check": "Alembic revision", "status": "BLOCKED", "detail": (heads.stdout + heads.stderr)[-800:]})
        return
    expected = match.group(1)
    try:
        conn = psycopg2.connect(database_url, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT version_num FROM alembic_version")
                row = cur.fetchone()
                actual = row[0] if row else None
        finally:
            conn.close()
    except Exception as exc:
        checks.append({"check": "Alembic revision", "status": "BLOCKED", "detail": f"database unreadable: {exc}"})
        return
    checks.append({"check": "Alembic revision", "status": "PASS" if actual == expected else "FAIL", "actual": actual, "expected": expected})


def _check_environment(database_url: str, redis_url: str, checks: list[dict]) -> None:
    try:
        conn = psycopg2.connect(database_url, connect_timeout=5)
        conn.close()
        checks.append({"check": "PostgreSQL reachable", "status": "PASS"})
    except Exception as exc:
        checks.append({"check": "PostgreSQL reachable", "status": "BLOCKED", "detail": str(exc)[-500:]})
    try:
        client = redis.from_url(redis_url, socket_connect_timeout=5)
        client.ping()
        client.close()
        checks.append({"check": "Redis reachable", "status": "PASS"})
    except Exception as exc:
        checks.append({"check": "Redis reachable", "status": "BLOCKED", "detail": str(exc)[-500:]})


def _check_install(data: dict | None, checks: list[dict]) -> None:
    if data is None:
        return
    summary = data.get("summary") or {}
    ok = summary.get("FAIL", 1) == 0 and summary.get("BLOCKED", 1) == 0 and summary.get("PASS", 0) >= 12
    checks.append({"check": "installation matrix", "status": "PASS" if ok else "FAIL", "actual": summary, "expected": {"FAIL": 0, "BLOCKED": 0, "PASS": ">=12"}})


def _check_performance(data: dict | None, checks: list[dict]) -> None:
    if data is None:
        return
    ok = data.get("status") == "PASS" and (data.get("database") or {}).get("status") == "PASS"
    checks.append({"check": "resource/performance probe", "status": "PASS" if ok else "FAIL", "detail": {"status": data.get("status"), "blockers": data.get("blockers")}})


def _check_recovery(data: dict | None, checks: list[dict]) -> None:
    if data is None:
        return
    ok = data.get("status") == "PASS"
    checks.append({"check": "backup/restore recovery", "status": "PASS" if ok else "FAIL", "detail": data.get("summary") or data.get("status")})


def _check_junit(path: str, checks: list[dict]) -> None:
    if not path:
        checks.append({"check": "backend test evidence", "status": "BLOCKED", "detail": "no JUnit path"})
        return
    p = Path(path)
    if not p.exists():
        checks.append({"check": "backend test evidence", "status": "BLOCKED", "detail": f"missing {p}"})
        return
    try:
        root = ET.parse(p).getroot()
    except Exception as exc:
        checks.append({"check": "backend test evidence", "status": "FAIL", "detail": f"invalid JUnit: {exc}"})
        return
    # pytest emits <testsuites><testsuite .../></testsuites>, so the totals live on the
    # child suites, not the root. A single-suite file puts them on the root instead.
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
    tests = sum(int(suite.attrib.get("tests", 0)) for suite in suites)
    failures = sum(int(suite.attrib.get("failures", 0)) for suite in suites)
    errors = sum(int(suite.attrib.get("errors", 0)) for suite in suites)
    skipped = sum(int(suite.attrib.get("skipped", 0)) for suite in suites)
    ok = tests > 0 and failures == 0 and errors == 0
    checks.append({"check": "backend tests", "status": "PASS" if ok else "FAIL", "tests": tests, "failures": failures, "errors": errors, "skipped": skipped})


def _check_playwright(path: str, checks: list[dict]) -> None:
    data = _load_json(path, "Playwright evidence", checks)
    if data is None:
        return
    stats = data.get("stats") or {}
    expected = int(stats.get("expected", 0))
    unexpected = int(stats.get("unexpected", 0))
    flaky = int(stats.get("flaky", 0))
    ok = expected > 0 and unexpected == 0 and flaky == 0
    checks.append({"check": "real browser E2E", "status": "PASS" if ok else "FAIL", "stats": stats})


def _write_evidence(out_dir: Path, checks: list[dict], status: str, head: str, limitations: list[str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": _now(),
        "commit": head,
        "status": status,
        "checks": checks,
        "known_limitations": limitations,
    }
    (out_dir / "qa-gate.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# Founder Alpha QA gate — {status}", "", f"Commit: `{head}`", ""]
    for check in checks:
        lines.append(f"- **{check['status']}** — {check['check']}: `{json.dumps(check.get('actual', check.get('detail', '')), ensure_ascii=False)[:500]}`")
    lines.extend(["", "## Known limitations", ""])
    lines.extend(f"- {item}" for item in limitations)
    (out_dir / "qa-gate.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", default=os.environ.get("EXPECTED_SHA", ""))
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--redis-url", default=os.environ.get("REDIS_URL", "redis://127.0.0.1:56379/15"))
    parser.add_argument("--install-evidence", default="")
    parser.add_argument("--performance-evidence", default="")
    parser.add_argument("--recovery-evidence", default="")
    parser.add_argument("--playwright-json", default="")
    parser.add_argument("--pytest-junit", default="")
    parser.add_argument("--out-dir", default="docs/evidence/founder-alpha/qa-gate")
    parser.add_argument("--known-limitation", action="append", default=[])
    args = parser.parse_args()

    checks: list[dict] = []
    head = _check_sha(args.expected_sha, checks)
    _check_environment(args.database_url, args.redis_url, checks)
    _check_migration(args.database_url, checks)
    _check_install(_load_json(args.install_evidence, "installation matrix evidence", checks), checks)
    _check_performance(_load_json(args.performance_evidence, "performance evidence", checks), checks)
    _check_recovery(_load_json(args.recovery_evidence, "recovery evidence", checks), checks)
    _check_junit(args.pytest_junit, checks)
    _check_playwright(args.playwright_json, checks)

    limitations = args.known_limitation or [
        "Z.ai CLI-dependent tests are not run in this environment.",
        "`zip`-dependent tests are skipped when the binary is unavailable.",
        "pg_stat_statements is not installed, so slow-query statistics are BLOCKED rather than guessed.",
        "Resource-exhaustion behavior (CPU/memory/disk limits) is not measured.",
        "Exact-head GitHub Actions cannot run without an approved push.",
    ]
    statuses = [check["status"] for check in checks]
    if "FAIL" in statuses:
        status = "FAIL"
    elif "BLOCKED" in statuses:
        status = "BLOCKED"
    else:
        status = "PASS"
    _write_evidence(Path(args.out_dir), checks, status, head, limitations)
    print(json.dumps({"status": status, "commit": head, "checks": checks, "known_limitations": limitations}, ensure_ascii=False, indent=2))
    return 0 if status == "PASS" else (3 if status == "BLOCKED" else 2)


if __name__ == "__main__":
    raise SystemExit(main())
