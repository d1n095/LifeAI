#!/usr/bin/env python3
"""V6 fail-closed release gate.

A missing or unverifiable requirement is BLOCKED, never PASS. The gate does not run the
acceptance suites itself; it verifies their machine-readable evidence against the live
services and the exact Git SHA, then writes one terminal result.

Exit codes: 0 PASS, 2 FAIL, 3 BLOCKED.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import redis

ROOT = Path(__file__).resolve().parents[3]
REQUIRED_STEPS = [
    "authentication",
    "unauthorized_routes",
    "streaming_chat",
    "streaming_delivery",
    "conversation_history",
    "document_ingestion",
    "pgvector_retrieval",
    "supervised_task",
    "approval_authorization",
    "approved_execution",
    "active_operation_cancel",
    "evidence_and_audit",
]
REQUIRED_FILES = [
    "founder-journey.json",
    "reliability.json",
    "playwright-founder-v6.json",
    "backend-junit.xml",
    "testlab-status.json",
    "provider-egress.json",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _http(url: str, timeout: float = 10.0) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return None


def _junit(path: Path) -> dict:
    if not path.exists():
        return {"status": "BLOCKED", "detail": "missing"}
    try:
        root = ET.parse(path).getroot()
    except Exception as exc:
        return {"status": "FAIL", "detail": f"invalid JUnit: {exc}"}
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
    tests = sum(int(s.attrib.get("tests", 0)) for s in suites)
    failures = sum(int(s.attrib.get("failures", 0)) for s in suites)
    errors = sum(int(s.attrib.get("errors", 0)) for s in suites)
    skipped = sum(int(s.attrib.get("skipped", 0)) for s in suites)
    names = " ".join(
        (case.attrib.get("classname", "") + " " + case.attrib.get("name", "")).lower()
        for case in root.iter("testcase")
    )
    return {
        "status": "PASS" if tests > 0 and failures == 0 and errors == 0 else "FAIL",
        "tests": tests,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "has_auth_rls": ("rls" in names or "security" in names or "auth" in names),
    }


def _git(expected: str) -> dict:
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, text=True, capture_output=True
    ).stdout.strip()
    return {
        "status": "PASS" if expected and head == expected and not dirty else "FAIL",
        "head": head,
        "expected": expected,
        "tracked_tree_clean": not dirty,
        "detail": dirty or "clean",
    }


def _live(args: argparse.Namespace) -> dict:
    checks: dict[str, dict] = {}
    checks["backend_health"] = {
        "status": "PASS" if _http(f"{args.base_url.rstrip('/')}/api/health") == 200 else "BLOCKED",
    }
    checks["frontend_login"] = {
        "status": "PASS" if _http(f"{args.frontend_url.rstrip('/')}/login") in (200, 307, 308) else "BLOCKED",
    }
    try:
        conn = psycopg2.connect(args.database_url, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT version_num FROM alembic_version")
                row = cur.fetchone()
            checks["postgres"] = {"status": "PASS", "revision": row[0] if row else None}
        finally:
            conn.close()
    except Exception as exc:
        checks["postgres"] = {"status": "BLOCKED", "detail": str(exc)[-300:]}
    try:
        client = redis.from_url(args.redis_url, socket_connect_timeout=5)
        checks["redis"] = {"status": "PASS" if client.ping() else "FAIL"}
        client.close()
    except Exception as exc:
        checks["redis"] = {"status": "BLOCKED", "detail": str(exc)[-300:]}
    checks["ollama"] = {
        "status": "PASS" if _http(f"{args.ollama_url.rstrip('/')}/api/tags") == 200 else "BLOCKED",
    }
    return checks


def _migration(args: argparse.Namespace, live: dict) -> dict:
    env = {**__import__("os").environ, "DATABASE_URL": args.database_url, "PYTHONPATH": "."}
    heads = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=ROOT / "backend",
        env=env,
        text=True,
        capture_output=True,
    )
    matches = re.findall(r"^\s*([0-9A-Za-z_]+)\s+\(head\)", heads.stdout, re.MULTILINE)
    current = (live.get("postgres") or {}).get("revision")
    ok = heads.returncode == 0 and len(matches) == 1 and current == matches[0]
    return {
        "status": "PASS" if ok else "FAIL",
        "heads_returncode": heads.returncode,
        "heads": matches,
        "current": current,
        "detail": (heads.stdout + heads.stderr)[-500:],
    }


def evaluate(args: argparse.Namespace) -> dict:
    evidence = Path(args.evidence_dir)
    checks: dict[str, dict] = {}
    checks["git"] = _git(args.expected_sha)
    missing = [name for name in REQUIRED_FILES if not (evidence / name).exists()]
    checks["required_evidence"] = {
        "status": "BLOCKED" if missing else "PASS",
        "missing": missing,
    }
    journey = _load(evidence / "founder-journey.json")
    reliability = _load(evidence / "reliability.json")
    playwright = _load(evidence / "playwright-founder-v6.json")
    status = _load(evidence / "testlab-status.json")
    egress = _load(evidence / "provider-egress.json")

    if journey is None:
        checks["founder_journey"] = {"status": "BLOCKED", "detail": "missing or invalid"}
    else:
        steps = {s.get("step"): s.get("status") for s in journey.get("steps") or []}
        missing_steps = [name for name in REQUIRED_STEPS if name not in steps]
        bad = [name for name in REQUIRED_STEPS if steps.get(name) not in ("PASS", "SKIP")]
        provider_ok = journey.get("provider_mode") == "local"
        ok = (
            journey.get("status") == "PASS"
            and not missing_steps
            and not bad
            and provider_ok
            and journey.get("commit") == args.expected_sha
        )
        checks["founder_journey"] = {
            "status": "PASS" if ok else "FAIL",
            "missing_steps": missing_steps,
            "bad_steps": bad,
            "provider_mode": journey.get("provider_mode"),
            "commit": journey.get("commit"),
        }

    if reliability is None:
        checks["reliability_f23"] = {"status": "BLOCKED", "detail": "missing or invalid"}
    else:
        faults = {f.get("fault"): f.get("status") for f in reliability.get("faults") or []}
        required_faults = {
            "backend_restart",
            "worker_restart",
            "redis_interruption",
            "database_reconnection",
            "ollama_timeout",
            "browser_disconnection",
            "duplicate_job_delivery",
            "expired_authorization",
        }
        missing_faults = sorted(required_faults - set(faults))
        bad_faults = sorted(name for name in required_faults if faults.get(name) != "PASS")
        ok = (
            reliability.get("status") == "PASS"
            and reliability.get("f23_status") == "PASS"
            and int(reliability.get("duration_seconds") or 0) >= 14400
            and reliability.get("commit") == args.expected_sha
            and reliability.get("provider_mode") == "local"
            and not missing_faults
            and not bad_faults
            and (reliability.get("error_rate") or 1) == 0
        )
        checks["reliability_f23"] = {
            "status": "PASS" if ok else ("BLOCKED" if int(reliability.get("duration_seconds") or 0) < 14400 else "FAIL"),
            "duration_seconds": reliability.get("duration_seconds"),
            "f23_status": reliability.get("f23_status"),
            "missing_faults": missing_faults,
            "bad_faults": bad_faults,
            "provider_mode": reliability.get("provider_mode"),
        }

    if playwright is None:
        checks["playwright"] = {"status": "BLOCKED", "detail": "missing or invalid"}
    else:
        stats = playwright.get("stats") or {}
        ok = (
            int(stats.get("expected", 0)) > 0
            and int(stats.get("unexpected", 0)) == 0
            and int(stats.get("flaky", 0)) == 0
        )
        checks["playwright"] = {"status": "PASS" if ok else "FAIL", "stats": stats}

    checks["backend_tests"] = _junit(evidence / "backend-junit.xml")
    if checks["backend_tests"].get("status") == "PASS" and not checks["backend_tests"].get("has_auth_rls"):
        checks["backend_tests"]["status"] = "BLOCKED"
        checks["backend_tests"]["detail"] = "JUnit has no auth/RLS/security test names"

    live = _live(args)
    checks["live_services"] = {
        "status": "FAIL"
        if any(v.get("status") == "FAIL" for v in live.values())
        else ("BLOCKED" if any(v.get("status") == "BLOCKED" for v in live.values()) else "PASS"),
        "services": live,
    }
    checks["migration"] = _migration(args, live)
    checks["provider_egress"] = {
        "status": (egress or {}).get("status", "BLOCKED"),
        "detail": (egress or {}).get("detail", "missing provider-egress evidence"),
    }
    checks["testlab_status"] = {
        "status": "PASS"
        if status
        and all(status.get("services", {}).values())
        and (status.get("migration") or {}).get("single_head")
        else "BLOCKED",
        "services": (status or {}).get("services"),
    }
    checks["duplicate_side_effects"] = {
        "status": "PASS"
        if reliability
        and any(
            f.get("fault") == "duplicate_job_delivery" and f.get("status") == "PASS"
            for f in reliability.get("faults") or []
        )
        else "BLOCKED",
    }
    checks["f24_full_gate"] = {
        "status": "PASS"
        if all(check.get("status") == "PASS" for check in checks.values())
        else "BLOCKED",
    }
    statuses = [check.get("status") for check in checks.values()]
    if "FAIL" in statuses:
        terminal = "FAIL"
    elif "BLOCKED" in statuses:
        terminal = "BLOCKED"
    else:
        terminal = "PASS"
    return {
        "generated_at": _now(),
        "commit": args.expected_sha,
        "status": terminal,
        "f24_status": "PASS" if terminal == "PASS" else ("FAIL" if terminal == "FAIL" else "BLOCKED"),
        "first_cycle_go": False,
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", default="")
    parser.add_argument("--evidence-dir", default="delivery/v6-testlab/evidence")
    parser.add_argument("--database-url", default="postgresql://lifeai:lifeai_testlab@127.0.0.1:55433/lifeai_testlab")
    parser.add_argument("--redis-url", default="redis://127.0.0.1:56380/0")
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--frontend-url", default="http://127.0.0.1:13000")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11435")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()
    if not args.expected_sha:
        args.expected_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    payload = evaluate(args)
    out_dir = Path(args.out_dir) if args.out_dir else Path(args.evidence_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "v6-release-gate.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    lines = [f"# V6 release gate — {payload['status']}", "", f"Commit: `{payload['commit']}`", ""]
    for name, check in payload["checks"].items():
        lines.append(f"- **{check.get('status')}** — {name}: `{json.dumps(check, ensure_ascii=False)[:700]}`")
    (out_dir / "v6-release-gate.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "f24_status": payload["f24_status"], "checks": payload["checks"]}, indent=2))
    return 0 if payload["status"] == "PASS" else (3 if payload["status"] == "BLOCKED" else 2)


if __name__ == "__main__":
    raise SystemExit(main())
