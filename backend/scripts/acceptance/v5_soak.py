#!/usr/bin/env python3
"""F23/V5 soak harness: real backend + worker against isolated local services.

This is deliberately not a smoke test with a PASS label. A run shorter than four hours is
reported as ``BLOCKED`` for F23 even if every request succeeds, because the directive requires
sustained evidence and a short run cannot provide it.

What it exercises through the real HTTP API and the real durable worker:
  * health and owner-scoped incident health;
  * JSON chat and SSE streaming chat;
  * document import (durable storage + worker ingestion + pgvector retrieval);
  * MainAI job creation/cancellation (skipped honestly if the capability is unavailable);
  * concurrent health reads, repeated requests, process restarts and Redis/PostgreSQL metrics.

Only the outbound AI provider and outbound email are faked, by the existing
``scripts/ci/run_e2e_backend.py`` harness. This script refuses non-local databases.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg2
import redis
from psycopg2 import sql
from sqlalchemy.engine import make_url

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_SECONDS = 4 * 60 * 60
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
            rss = 0
            for line in (entry / "status").read_text().splitlines():
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
            # Same reason as resource_probe.py: the transport is faked by the E2E
            # harness, but the provider registry must see a configured key rather than
            # a template placeholder that app.providers.base deliberately rejects.
            "OPENAI_API_KEY": "sk-soak-local-only",
            "RATE_LIMIT_CHAT_PER_MINUTE": "100000",
            "RATE_LIMIT_DEFAULT_PER_MINUTE": "100000",
            "RATE_LIMIT_LIBRARY_IMPORT_PER_MINUTE": "100000",
            "RATE_LIMIT_LOGIN_PER_MINUTE": "100000",
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


def _wait_health(client: httpx.Client, proc: subprocess.Popen, timeout: float = 90.0) -> float:
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
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _migrate(args: argparse.Namespace) -> None:
    env = {
        **os.environ,
        "DATABASE_URL": args.database_url,
        "APP_DATABASE_URL": args.app_database_url,
        "PYTHONPATH": ".",
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade failed: {(result.stdout + result.stderr)[-1200:]}")


def _grant_runtime_role(args: argparse.Namespace) -> None:
    """Mirror backend/db-init and the pytest fixture for this disposable local database."""

    role = make_url(args.app_database_url).username
    database = make_url(args.database_url).database
    if not role or not database:
        raise RuntimeError("APP_DATABASE_URL must include a role and database name")
    conn = psycopg2.connect(args.database_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), sql.Identifier(role)))
            cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
            cur.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}").format(sql.Identifier(role)))
            cur.execute(sql.SQL("GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO {}").format(sql.Identifier(role)))
            cur.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}").format(sql.Identifier(role)))
            cur.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL PRIVILEGES ON SEQUENCES TO {}").format(sql.Identifier(role)))
    finally:
        conn.close()


def _login(client: httpx.Client, args: argparse.Namespace) -> str:
    response = client.post(
        "/api/auth/login",
        json={"email": args.founder_email, "password": args.founder_password},
    )
    if response.status_code != 200:
        raise RuntimeError(f"login failed: {response.status_code} {response.text[:300]}")
    return response.json()["csrf_token"]


def _authed(client: httpx.Client, session: dict, method: str, path: str, **kwargs) -> httpx.Response:
    """Send an authenticated request, renewing the session once when the token expired.

    Access tokens live ``settings.access_token_expire_minutes`` (15 minutes by default), so a
    multi-hour soak must re-authenticate. Without this the loop keeps running on 401 responses
    and silently produces no evidence for the rest of the run.
    """

    headers = dict(kwargs.pop("headers", {}))
    headers["X-CSRF-Token"] = session["csrf"]
    response = client.request(method, path, headers=headers, **kwargs)
    if response.status_code == 401:
        session["refreshes"] += 1
        session["csrf"] = _login(client, session["args"])
        headers["X-CSRF-Token"] = session["csrf"]
        response = client.request(method, path, headers=headers, **kwargs)
    return response


def _parse_sse(text: str) -> dict:
    events = 0
    deltas = 0
    done = False
    error = None
    for block in text.split("\n\n"):
        event = None
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    data = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    data = None
        if event is not None:
            events += 1
        if event == "delta":
            deltas += 1
        elif event == "done":
            done = True
        elif event == "error":
            error = (data or {}).get("message", "stream error")
    return {"events": events, "deltas": deltas, "done": done, "error": error}


def _db_metrics(database_url: str) -> dict:
    try:
        conn = psycopg2.connect(database_url, connect_timeout=5)
    except Exception as exc:
        return {"status": "BLOCKED", "reason": f"{type(exc).__name__}: {exc}"}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), pg_database_size(current_database())")
            database, size_bytes = cur.fetchone()
            cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
            connections = int(cur.fetchone()[0])
            result = {
                "status": "PASS",
                "database": database,
                "size_bytes": int(size_bytes),
                "connections": connections,
                "tables": {},
                "jobs": {},
            }
            for table in ("users", "documents", "document_chunks", "conversations", "messages", "runtime_incidents", "knowledge_import_jobs", "mainai_jobs"):
                try:
                    cur.execute(f"SELECT count(*) FROM {table}")
                    result["tables"][table] = int(cur.fetchone()[0])
                except psycopg2.Error:
                    conn.rollback()
                    result["tables"][table] = None
            for table, column in (("knowledge_import_jobs", "status"), ("mainai_jobs", "status"), ("runtime_incidents", "status")):
                try:
                    cur.execute(f"SELECT {column}, count(*) FROM {table} GROUP BY {column}")
                    result["jobs"][table] = {str(row[0]): int(row[1]) for row in cur.fetchall()}
                except psycopg2.Error:
                    conn.rollback()
                    result["jobs"][table] = None
            return result
    finally:
        conn.close()


def _redis_metrics(redis_url: str) -> dict:
    try:
        client = redis.from_url(redis_url, socket_connect_timeout=5)
        info = client.info("memory")
        result = {
            "status": "PASS",
            "dbsize": int(client.dbsize()),
            "used_memory_human": info.get("used_memory_human"),
            "used_memory_bytes": int(info.get("used_memory", 0)),
            "connected_clients": int(client.info("clients").get("connected_clients", 0)),
        }
        client.close()
        return result
    except Exception as exc:
        return {"status": "BLOCKED", "reason": f"{type(exc).__name__}: {exc}"}


def _incident_health(client: httpx.Client, session: dict) -> dict:
    response = _authed(client, session, "GET", "/api/incidents/health")
    if response.status_code != 200:
        return {"state": "unavailable", "http_status": response.status_code}
    return response.json()


def _record(latencies: dict[str, list[float]], name: str, started: float) -> None:
    latencies.setdefault(name, []).append((time.perf_counter() - started) * 1000)


def _record_failure(failures: list[dict], name: str, status: int | None, detail: str) -> None:
    if len(failures) >= 100:
        return
    failures.append({"operation": name, "status": status, "detail": detail[:300], "at": _now()})


def _run_job(client: httpx.Client, session: dict, cycle: int) -> dict:
    response = _authed(
        client,
        session,
        "POST",
        "/api/mainai/jobs",
        json={
            "job_type": "message_sequence_backfill",
            "input_refs": [],
            "idempotency_key": f"v5-soak-job-{uuid.uuid4()}",
        },
    )
    if response.status_code == 409:
        return {"status": "SKIPPED", "http_status": 409, "detail": response.text[:300]}
    if response.status_code != 201:
        return {"status": "FAIL", "http_status": response.status_code, "detail": response.text[:300]}
    job_id = response.json()["id"]
    cancelled = _authed(client, session, "POST", f"/api/mainai/jobs/{job_id}/cancel")
    return {
        "status": "PASS" if cancelled.status_code in (200, 409) else "FAIL",
        "http_status": cancelled.status_code,
        "job_id": job_id,
    }


def _import_document(client: httpx.Client, session: dict, cycle: int) -> dict:
    content = f"V5 soak document {cycle} {uuid.uuid4()}. Semantiskt innehall for retrieval.".encode()
    response = _authed(
        client,
        session,
        "POST",
        "/api/library/import",
        files={"file": (f"v5-soak-{cycle}.txt", content, "text/plain")},
    )
    if response.status_code != 200:
        return {"status": "FAIL", "http_status": response.status_code, "detail": response.text[:300]}
    job_id = response.json()["id"]
    deadline = time.monotonic() + 30
    last = None
    while time.monotonic() < deadline:
        poll = _authed(client, session, "GET", f"/api/library/jobs/{job_id}")
        if poll.status_code != 200:
            return {"status": "FAIL", "http_status": poll.status_code, "detail": poll.text[:300]}
        last = poll.json()
        if last.get("status") not in {"pending", "running"}:
            break
        time.sleep(0.25)
    return {
        "status": "PASS" if last and last.get("status") in {"completed", "partial"} else "FAIL",
        "job_status": last.get("status") if last else None,
        "job_id": job_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--app-database-url", required=True)
    parser.add_argument("--redis-url", default="redis://127.0.0.1:56379/15")
    parser.add_argument("--duration-seconds", type=int, default=REQUIRED_SECONDS)
    parser.add_argument("--sample-every-seconds", type=int, default=30)
    parser.add_argument("--restart-every-seconds", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--secret-key", default="v5-soak-local-secret")
    parser.add_argument("--founder-email", default="founder@lifeos.local")
    parser.add_argument("--founder-password", default="TestFounderPassword123!")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    parsed = make_url(args.database_url)
    if parsed.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("refusing to run the soak harness against a non-local database")
    if args.duration_seconds <= 0:
        raise SystemExit("duration must be positive")

    temp = Path(tempfile.mkdtemp(prefix="lifeai-v5-soak-"))
    (temp / "storage").mkdir(parents=True, exist_ok=True)
    port = _free_port()
    result: dict = {
        "generated_at": _now(),
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=BACKEND_ROOT.parent, text=True).strip(),
        "required_seconds": REQUIRED_SECONDS,
        "duration_seconds": args.duration_seconds,
        "environment": {
            "database_url": _redacted(args.database_url),
            "redis_url": _redacted(args.redis_url),
            "port": port,
            "python": sys.version.split()[0],
            "provider": "faked by scripts/ci/run_e2e_backend.py",
        },
        "status": "BLOCKED",
        "f23_status": "BLOCKED",
        "blockers": [],
        "failures": [],
        "counts": {},
        "latencies_ms": {},
        "samples": [],
    }
    proc: subprocess.Popen | None = None
    log_path: Path | None = None
    try:
        _migrate(args)
        _grant_runtime_role(args)
        proc, log_path = _start_backend(args, port, temp)
        result["log_path"] = str(log_path)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=120.0) as client:
            result["startup_latency_s"] = round(_wait_health(client, proc), 3)
            session = {"csrf": _login(client, args), "args": args, "refreshes": 0}
            result["login_status"] = 200
            last_sample = 0.0
            last_restart = time.monotonic()
            started = time.monotonic()
            deadline = started + args.duration_seconds
            cycle = 0
            restarts = 0
            jobs_skipped = 0
            while time.monotonic() < deadline:
                cycle += 1
                for name, path in (("health", "/api/health"), ("incident_health", "/api/incidents/health")):
                    t0 = time.perf_counter()
                    response = _authed(client, session, "GET", path)
                    _record(result["latencies_ms"], name, t0)
                    result["counts"][name] = result["counts"].get(name, 0) + 1
                    if response.status_code != 200:
                        _record_failure(result["failures"], name, response.status_code, response.text)

                t0 = time.perf_counter()
                chat = _authed(
                    client,
                    session,
                    "POST",
                    "/api/chat",
                    json={"message": f"V5 soak cycle {cycle}: svara kort.", "client_message_id": f"v5-soak-chat-{uuid.uuid4()}"},
                )
                _record(result["latencies_ms"], "chat", t0)
                result["counts"]["chat"] = result["counts"].get("chat", 0) + 1
                if chat.status_code != 200:
                    _record_failure(result["failures"], "chat", chat.status_code, chat.text)

                t0 = time.perf_counter()
                stream = _authed(
                    client,
                    session,
                    "POST",
                    "/api/chat/stream",
                    json={"message": f"V5 soak stream {cycle}: svara kort.", "client_message_id": f"v5-soak-stream-{uuid.uuid4()}"},
                )
                _record(result["latencies_ms"], "chat_stream", t0)
                result["counts"]["chat_stream"] = result["counts"].get("chat_stream", 0) + 1
                parsed_stream = _parse_sse(stream.text)
                if stream.status_code != 200 or not parsed_stream["done"] or parsed_stream["error"]:
                    _record_failure(
                        result["failures"],
                        "chat_stream",
                        stream.status_code,
                        parsed_stream["error"] or stream.text[:300],
                    )

                if cycle % 5 == 0:
                    imported = _import_document(client, session, cycle)
                    result["counts"]["document_import"] = result["counts"].get("document_import", 0) + 1
                    if imported["status"] != "PASS":
                        _record_failure(result["failures"], "document_import", imported.get("http_status"), json.dumps(imported))
                    else:
                        # Retrieval after ingestion is the real pgvector-backed chat path.
                        t0 = time.perf_counter()
                        grounded = _authed(
                            client,
                            session,
                            "POST",
                            "/api/chat",
                            json={"message": "Vilket innehall finns i det importerade V5 soak-dokumentet?", "client_message_id": f"v5-soak-ground-{uuid.uuid4()}"},
                        )
                        _record(result["latencies_ms"], "retrieval_chat", t0)
                        result["counts"]["retrieval_chat"] = result["counts"].get("retrieval_chat", 0) + 1
                        if grounded.status_code != 200:
                            _record_failure(result["failures"], "retrieval_chat", grounded.status_code, grounded.text)

                if cycle % 3 == 0:
                    job = _run_job(client, session, cycle)
                    result["counts"]["mainai_job"] = result["counts"].get("mainai_job", 0) + 1
                    if job["status"] == "SKIPPED":
                        jobs_skipped += 1
                    elif job["status"] != "PASS":
                        _record_failure(result["failures"], "mainai_job", job.get("http_status"), json.dumps(job))

                if cycle % 20 == 0:
                    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
                        statuses = list(pool.map(lambda _: client.get("/api/health").status_code, range(args.concurrency * 4)))
                    result["counts"]["concurrent_health"] = result["counts"].get("concurrent_health", 0) + len(statuses)
                    if any(status != 200 for status in statuses):
                        _record_failure(result["failures"], "concurrent_health", None, str(statuses))

                now = time.monotonic()
                if args.sample_every_seconds > 0 and now - last_sample >= args.sample_every_seconds:
                    last_sample = now
                    if len(result["samples"]) < 600:
                        result["samples"].append(
                            {
                                "at": _now(),
                                "elapsed_s": round(now - started, 1),
                                "process": _sample_process(proc.pid),
                                "database": _db_metrics(args.database_url),
                                "redis": _redis_metrics(args.redis_url),
                                "incidents": _incident_health(client, session),
                            }
                        )

                if args.restart_every_seconds > 0 and now - last_restart >= args.restart_every_seconds:
                    _stop(proc)
                    proc, log_path = _start_backend(args, port, temp)
                    _wait_health(client, proc)
                    session["csrf"] = _login(client, session["args"])
                    restarts += 1
                    last_restart = time.monotonic()
                    result["counts"]["restarts"] = restarts

                time.sleep(0.5)

            result["counts"]["cycles"] = cycle
            result["counts"]["jobs_skipped_capability"] = jobs_skipped
            result["counts"]["restarts"] = restarts
            result["counts"]["session_refreshes"] = session["refreshes"]
            result["incident_health_last"] = _incident_health(client, session)
    except Exception as exc:
        result["blockers"].append(f"{type(exc).__name__}: {exc}")
    finally:
        if proc is not None:
            _stop(proc)

    for name, values in result["latencies_ms"].items():
        if values:
            result["latencies_ms"][name] = {
                "count": len(values),
                "p50": round(statistics.median(values), 2),
                "p95": round(_pct(values, 95), 2),
                "max": round(max(values), 2),
            }
    result["database"] = _db_metrics(args.database_url)
    result["redis"] = _redis_metrics(args.redis_url)
    result["finished_at"] = _now()

    if result["duration_seconds"] < REQUIRED_SECONDS:
        result["blockers"].append(
            f"duration_seconds={result['duration_seconds']} < required {REQUIRED_SECONDS}; "
            "F23 cannot be claimed from this run"
        )
    if result["failures"]:
        result["status"] = "FAIL"
    elif result["blockers"]:
        result["status"] = "BLOCKED"
    elif not result["samples"]:
        result["status"] = "BLOCKED"
        result["blockers"].append("no resource samples were collected")
    else:
        result["status"] = "PASS"
    result["f23_status"] = "PASS" if result["status"] == "PASS" else ("FAIL" if result["status"] == "FAIL" else "BLOCKED")
    result["limitations"] = [
        "Provider and email transports are faked by the existing E2E harness; real Ollama/pgvector acceptance is separate.",
        "This harness is local and isolated; it never targets production.",
        "A run shorter than four hours is intentionally reported BLOCKED for F23.",
    ]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("status", "f23_status", "duration_seconds", "counts", "blockers")}, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else (3 if result["status"] == "BLOCKED" else 2)


if __name__ == "__main__":
    raise SystemExit(main())
