#!/usr/bin/env python3
"""Measure real Founder Alpha resource and latency behavior.

Runs the real E2E backend (real API + durable worker, only the outbound provider/email faked)
against an existing isolated database. No production access, no provider calls.

Measurements are intentionally reported with their limits:
  * startup: wall time until /api/health first returns 200;
  * API: health, login, document list, project status and one chat reply;
  * CPU/RAM: sampled from /proc for the backend process tree;
  * DB: size, connection count, queue counts and optional pg_stat_statements;
  * concurrency: parallel health requests;
  * disk: free space and storage-root size;
  * slow-query statistics: BLOCKED (not guessed) if pg_stat_statements is absent.

Example:
    python scripts/acceptance/resource_probe.py \
      --database-url postgresql://postgres@127.0.0.1:55432/lifeos_e2e \
      --app-database-url postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_e2e \
      --redis-url redis://127.0.0.1:56379/15 \
      --out ../docs/evidence/founder-alpha/resource-probe.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg2
from sqlalchemy.engine import make_url

BACKEND_ROOT = Path(__file__).resolve().parents[2]
CLOCK_TICKS = os.sysconf("SC_CLK_TCK")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _redacted(url: str) -> str:
    try:
        parsed = make_url(url)
    except Exception:
        return "<unparseable-url>"
    return f"{parsed.drivername}://{parsed.username or ''}@{parsed.host or ''}:{parsed.port or ''}/{parsed.database or ''}"


def _pct(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((percentile / 100) * (len(ordered) - 1))))
    return ordered[index]


def _proc_table() -> dict[int, tuple[int, int, int]]:
    """pid -> (ppid, rss_kb, cpu_ticks)."""
    table: dict[int, tuple[int, int, int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            rest = stat.rsplit(")", 1)[1].split()
            ppid = int(rest[1])
            utime = int(rest[11])
            stime = int(rest[12])
            status = (entry / "status").read_text()
            rss = 0
            for line in status.splitlines():
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1])
                    break
            table[int(entry.name)] = (ppid, rss, utime + stime)
        except (FileNotFoundError, ProcessLookupError, IndexError, ValueError):
            continue
    return table


def _descendants(root_pid: int) -> set[int]:
    table = _proc_table()
    result = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _rss, _cpu) in table.items():
            if ppid in result and pid not in result:
                result.add(pid)
                changed = True
    return result


def _sample_process(root_pid: int) -> dict:
    table = _proc_table()
    pids = _descendants(root_pid)
    return {
        "pids": sorted(pids),
        "rss_mb": round(sum(table.get(pid, (0, 0, 0))[1] for pid in pids) / 1024, 1),
        "cpu_seconds": round(sum(table.get(pid, (0, 0, 0))[2] for pid in pids) / CLOCK_TICKS, 3),
    }


def _db_metrics(database_url: str) -> dict:
    try:
        conn = psycopg2.connect(database_url, connect_timeout=5)
    except Exception as exc:
        return {"status": "BLOCKED", "reason": f"database unreachable: {type(exc).__name__}"}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), pg_database_size(current_database())")
            database, size_bytes = cur.fetchone()
            cur.execute("SELECT count(*) FROM pg_stat_activity")
            connections = int(cur.fetchone()[0])
            result = {
                "status": "PASS",
                "database": database,
                "size_bytes": int(size_bytes),
                "connections": connections,
                "tables": {},
                "queues": {},
            }
            for table in ("users", "documents", "document_chunks", "conversations", "messages", "memory_source_units"):
                try:
                    cur.execute(f"SELECT count(*) FROM {table}")
                    result["tables"][table] = int(cur.fetchone()[0])
                except psycopg2.Error:
                    result["tables"][table] = None
            for table, column in (
                ("knowledge_import_jobs", "status"),
                ("mainai_jobs", "status"),
            ):
                try:
                    cur.execute(f"SELECT {column}, count(*) FROM {table} GROUP BY {column}")
                    result["queues"][table] = {str(row[0]): int(row[1]) for row in cur.fetchall()}
                except psycopg2.Error:
                    result["queues"][table] = None
            cur.execute("SELECT extname FROM pg_extension WHERE extname = 'pg_stat_statements'")
            has_stats = cur.fetchone() is not None
            result["slow_query_stats"] = {"status": "BLOCKED", "reason": "pg_stat_statements not installed"}
            if has_stats:
                try:
                    cur.execute(
                        "SELECT query, calls, total_exec_time, mean_exec_time "
                        "FROM pg_stat_statements ORDER BY mean_exec_time DESC LIMIT 5"
                    )
                    result["slow_query_stats"] = {
                        "status": "PASS",
                        "top": [
                            {
                                "query": row[0][:240],
                                "calls": int(row[1]),
                                "total_exec_time_ms": round(float(row[2]), 2),
                                "mean_exec_time_ms": round(float(row[3]), 3),
                            }
                            for row in cur.fetchall()
                        ],
                    }
                except psycopg2.Error as exc:
                    result["slow_query_stats"] = {"status": "BLOCKED", "reason": f"pg_stat_statements unreadable: {type(exc).__name__}"}
            return result
    finally:
        conn.close()


def _disk_metrics(storage_root: Path) -> dict:
    usage = shutil.disk_usage(storage_root)
    total = 0
    if storage_root.exists():
        total = sum(path.stat().st_size for path in storage_root.rglob("*") if path.is_file())
    return {
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "storage_root": str(storage_root),
        "storage_root_bytes": total,
    }


def _start_backend(args: argparse.Namespace, port: int, temp: Path) -> tuple[subprocess.Popen, Path]:
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": args.database_url,
            "APP_DATABASE_URL": args.app_database_url,
            "REDIS_URL": args.redis_url,
            "SECRET_KEY": args.secret_key,
            "FOUNDER_EMAIL": args.founder_email,
            "FOUNDER_PASSWORD": args.founder_password,
            "E2E_FOUNDER_PASSWORD": args.founder_password,
            "E2E_DATABASE_URL": args.database_url,
            "E2E_EMAIL_LOG_PATH": str(temp / "emails.jsonl"),
            "E2E_BACKEND_PORT": str(port),
            "STORAGE_ROOT": str(temp / "storage"),
            "COOKIE_SECURE": "false",
            "ENABLE_SCHEDULED_CLEANUP": "false",
            # The E2E harness replaces the provider transport, but the registry still
            # requires a configured provider. A value that looks like a template
            # placeholder is treated as unconfigured by app.providers.base, so use a
            # non-placeholder test key to exercise the real chat path.
            "OPENAI_API_KEY": "sk-resource-probe-local-only",
            "RATE_LIMIT_LOGIN_PER_MINUTE": "1000",
            "RATE_LIMIT_REGISTER_PER_MINUTE": "1000",
            "RATE_LIMIT_FORGOT_PASSWORD_PER_MINUTE": "1000",
            "RATE_LIMIT_RESET_PASSWORD_PER_MINUTE": "1000",
            "RATE_LIMIT_VERIFY_EMAIL_PER_MINUTE": "1000",
            "PYTHONPATH": ".",
        }
    )
    log_path = temp / "backend.log"
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "scripts/ci/run_e2e_backend.py"],
        cwd=BACKEND_ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    log.close()
    return proc, log_path


def _wait_health(client: httpx.Client, proc: subprocess.Popen, timeout: float = 60.0) -> float:
    started = time.perf_counter()
    deadline = started + timeout
    while time.perf_counter() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"backend exited during startup with code {proc.returncode}")
        try:
            response = client.get("/api/health")
            if response.status_code == 200:
                return time.perf_counter() - started
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError("backend did not become healthy before timeout")


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--app-database-url", required=True)
    parser.add_argument("--redis-url", default="redis://127.0.0.1:56379/15")
    parser.add_argument("--secret-key", default="resource-probe-local-secret")
    parser.add_argument("--founder-email", default="founder@lifeos.local")
    parser.add_argument("--founder-password", default="TestFounderPassword123!")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    if "127.0.0.1" not in args.database_url and "localhost" not in args.database_url:
        raise SystemExit("refusing to run resource probe against a non-local database")

    temp = Path(tempfile.mkdtemp(prefix="lifeai-resource-probe-"))
    (temp / "storage").mkdir(parents=True, exist_ok=True)
    port = _free_port()
    proc, log_path = _start_backend(args, port, temp)
    result: dict = {
        "generated_at": _now(),
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=BACKEND_ROOT.parent, text=True).strip(),
        "environment": {
            "database_url": _redacted(args.database_url),
            "redis_url": _redacted(args.redis_url),
            "port": port,
            "python": sys.version.split()[0],
        },
        "status": "PASS",
        "blockers": [],
    }
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60.0) as client:
            startup = _wait_health(client, proc)
            result["startup_latency_s"] = round(startup, 3)
            cpu_started = time.perf_counter()
            before = _sample_process(proc.pid)
            health_latencies = []
            for _ in range(40):
                started = time.perf_counter()
                response = client.get("/api/health")
                health_latencies.append((time.perf_counter() - started) * 1000)
                assert response.status_code == 200, response.text[:200]
            result["health_latency_ms"] = {
                "p50": round(statistics.median(health_latencies), 2),
                "p95": round(_pct(health_latencies, 95), 2),
                "max": round(max(health_latencies), 2),
            }

            login_started = time.perf_counter()
            login = client.post(
                "/api/auth/login",
                json={"email": args.founder_email, "password": args.founder_password},
            )
            result["login_latency_ms"] = round((time.perf_counter() - login_started) * 1000, 2)
            result["login_status"] = login.status_code
            if login.status_code != 200:
                result["status"] = "FAIL"
                result["blockers"].append(f"login returned {login.status_code}")
            else:
                csrf = login.json().get("csrf_token", "")
                for path in ("/api/documents", "/api/conversations", "/api/mainai/project-status"):
                    started = time.perf_counter()
                    response = client.get(path)
                    result.setdefault("api_latency_ms", {})[path] = round((time.perf_counter() - started) * 1000, 2)
                    result.setdefault("api_status", {})[path] = response.status_code
                    if response.status_code != 200:
                        result["status"] = "FAIL"
                        result["blockers"].append(f"{path} returned {response.status_code}")

                chat_started = time.perf_counter()
                chat = client.post(
                    "/api/chat",
                    json={"message": "Resursprobe: svara kort."},
                    headers={"X-CSRF-Token": csrf},
                )
                result["chat_latency_ms"] = round((time.perf_counter() - chat_started) * 1000, 2)
                result["chat_status"] = chat.status_code
                result["chat_reply_chars"] = len((chat.json().get("reply") or "")) if chat.status_code == 200 else 0
                if chat.status_code != 200:
                    result["status"] = "FAIL"
                    result["blockers"].append(f"chat returned {chat.status_code}")

            # Concurrency: parallel reads against the real API.
            def _one(_index: int) -> tuple[int, float]:
                started = time.perf_counter()
                response = client.get("/api/health")
                return response.status_code, (time.perf_counter() - started) * 1000

            with ThreadPoolExecutor(max_workers=10) as pool:
                parallel = list(pool.map(_one, range(40)))
            parallel_latencies = [latency for _status, latency in parallel]
            result["concurrency"] = {
                "requests": len(parallel),
                "workers": 10,
                "statuses": sorted({status for status, _latency in parallel}),
                "p50_ms": round(statistics.median(parallel_latencies), 2),
                "p95_ms": round(_pct(parallel_latencies, 95), 2),
            }
            if any(status != 200 for status, _latency in parallel):
                result["status"] = "FAIL"
                result["blockers"].append("parallel health requests returned non-200")

            # Repeated reads: RSS delta is a real observation, not a leak verdict.
            rss_before = _sample_process(proc.pid)["rss_mb"]
            for _ in range(100):
                response = client.get("/api/documents")
                assert response.status_code == 200
            rss_after = _sample_process(proc.pid)["rss_mb"]
            result["memory"] = {
                "rss_before_mb": rss_before,
                "rss_after_mb": rss_after,
                "delta_mb": round(rss_after - rss_before, 1),
                "repeated_reads": 100,
                "note": "RSS delta is an observation, not proof of a leak; GC/allocator behavior varies.",
            }

            after = _sample_process(proc.pid)
            elapsed = max(time.perf_counter() - cpu_started, 0.001)
            cpu_delta = after["cpu_seconds"] - before["cpu_seconds"]
            result["cpu"] = {
                "window_s": round(elapsed, 3),
                "process_tree_cpu_seconds": round(cpu_delta, 3),
                "process_tree_cpu_percent": round((cpu_delta / elapsed) * 100, 1),
            }
            result["memory"]["process_tree_rss_mb"] = after["rss_mb"]
    except Exception as exc:
        result["status"] = "BLOCKED"
        result["blockers"].append(f"{type(exc).__name__}: {exc}")
    finally:
        _stop(proc)

    result["database"] = _db_metrics(args.database_url)
    result["disk"] = _disk_metrics(temp / "storage")
    result["log_path"] = str(log_path)
    if result["database"].get("status") == "BLOCKED":
        result["blockers"].append("database metrics blocked")
    if result.get("status") == "PASS" and result.get("blockers"):
        result["status"] = "BLOCKED"

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
    print(text)
    return 0 if result["status"] == "PASS" else (3 if result["status"] == "BLOCKED" else 2)


if __name__ == "__main__":
    raise SystemExit(main())
