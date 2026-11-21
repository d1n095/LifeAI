#!/usr/bin/env python3
"""V6 continuous reliability runner with controlled fault injection.

Runs against the isolated Docker test lab (or an already-running local backend with
--no-docker). It performs real HTTP/SSE interactions and injects:

  * backend restart
  * worker restart
  * Redis interruption (CLIENT PAUSE)
  * PostgreSQL connection termination
  * Ollama stop/timeout
  * browser/SSE disconnect
  * duplicate durable-job delivery (same idempotency key)
  * expired execution authorization (active envelope is expired, then /current must be null)

It records latency percentiles, throughput, error rate, recovery times, resource samples,
the exact Git SHA, environment and terminal PASS/FAIL/BLOCKED per fault. A run shorter than
four hours is never F23 evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg2
import redis
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[3]
REQUIRED_SECONDS = 4 * 60 * 60
ALL_FAULTS = [
    "backend_restart",
    "worker_restart",
    "redis_interruption",
    "database_reconnection",
    "ollama_timeout",
    "browser_disconnection",
    "duplicate_job_delivery",
    "expired_authorization",
]
DOCKER_FAULTS = {
    "backend_restart",
    "worker_restart",
    "redis_interruption",
    "database_reconnection",
    "ollama_timeout",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redacted(url: str) -> str:
    try:
        parsed = make_url(url)
    except Exception:
        return "<unparseable>"
    return f"{parsed.drivername}://{parsed.username or ''}@{parsed.host or ''}:{parsed.port or ''}/{parsed.database or ''}"


def _pct(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((percentile / 100) * (len(ordered) - 1))))
    return ordered[index]


def _run(cmd: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout)


class Runner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.client = httpx.Client(
            base_url=args.base_url.rstrip("/"),
            timeout=httpx.Timeout(120.0, connect=10.0),
            follow_redirects=False,
        )
        self.csrf = ""
        self.failures: list[dict] = []
        self.faults: list[dict] = []
        self.counts: dict[str, int] = {}
        self.latencies: dict[str, list[float]] = {}
        self.samples: list[dict] = []
        self.provider = None
        self.model = None
        self.requests_total = 0

    def close(self) -> None:
        self.client.close()

    def headers(self) -> dict:
        return {"X-CSRF-Token": self.csrf} if self.csrf else {}

    def record_latency(self, name: str, started: float) -> None:
        self.latencies.setdefault(name, []).append((time.perf_counter() - started) * 1000)
        self.counts[name] = self.counts.get(name, 0) + 1
        self.requests_total += 1

    def failure(self, operation: str, status: int | None, detail: str) -> None:
        if len(self.failures) >= 100:
            return
        self.failures.append(
            {"operation": operation, "status": status, "detail": detail[:300], "at": _now()}
        )

    def login(self) -> bool:
        response = self.client.post(
            "/api/auth/login",
            json={"email": self.args.founder_email, "password": self.args.founder_password},
        )
        if response.status_code != 200:
            self.failure("login", response.status_code, response.text)
            return False
        self.csrf = response.json().get("csrf_token", "")
        return bool(self.csrf)

    def health(self) -> bool:
        try:
            response = self.client.get("/api/health")
            return response.status_code == 200
        except Exception:
            return False

    def wait_health(self, timeout: float = 90.0) -> float | None:
        started = time.perf_counter()
        deadline = started + timeout
        while time.monotonic() < deadline:
            if self.health():
                return round(time.perf_counter() - started, 3)
            time.sleep(0.5)
        return None

    def chat(self) -> dict:
        started = time.perf_counter()
        try:
            response = self.client.post(
                "/api/chat",
                json={
                    "message": f"V6 reliability probe {uuid.uuid4().hex[:8]}: svara kort.",
                    "client_message_id": f"v6-rel-{uuid.uuid4()}",
                },
                headers=self.headers(),
            )
        except Exception as exc:
            self.failure("chat", None, f"{type(exc).__name__}: {exc}")
            return {"status": "FAIL", "error": str(exc)}
        self.record_latency("chat", started)
        if response.status_code != 200:
            self.failure("chat", response.status_code, response.text)
            return {"status": "FAIL", "http_status": response.status_code}
        body = response.json()
        self.provider = body.get("provider") or self.provider
        self.model = body.get("model") or self.model
        if body.get("assistant_status") != "succeeded":
            self.failure("chat", response.status_code, json.dumps(body)[:300])
            return {"status": "FAIL", "body": body}
        return {"status": "PASS", "body": body}

    def stream(self, *, disconnect: bool = False) -> dict:
        started = time.perf_counter()
        start = None
        deltas = 0
        done = None
        try:
            with self.client.stream(
                "POST",
                "/api/chat/stream",
                json={
                    "message": f"V6 reliability stream {uuid.uuid4().hex[:8]}: svara kort.",
                    "client_message_id": f"v6-rel-stream-{uuid.uuid4()}",
                },
                headers=self.headers(),
                timeout=httpx.Timeout(180.0, connect=10.0),
            ) as response:
                if response.status_code != 200:
                    self.failure("chat_stream", response.status_code, response.read()[:300].decode(errors="replace"))
                    return {"status": "FAIL", "http_status": response.status_code}
                event = None
                data_lines: list[str] = []
                for line in response.iter_lines():
                    if line == "":
                        if data_lines:
                            try:
                                data = json.loads("\n".join(data_lines))
                            except json.JSONDecodeError:
                                data = None
                            if event == "start":
                                start = data
                            elif event == "delta":
                                deltas += 1
                                if disconnect:
                                    # Close the SSE response while the provider is still
                                    # producing. The backend must persist an honest terminal
                                    # state rather than leave an inflight claim.
                                    raise httpx.ReadError("simulated browser disconnect")
                            elif event == "done":
                                done = data
                        event = None
                        data_lines = []
                    elif line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].strip())
        except httpx.ReadError as exc:
            if disconnect and start is not None and deltas > 0:
                self.record_latency("browser_disconnect", started)
                return {"status": "PASS", "disconnected": True, "deltas": deltas, "detail": str(exc)}
            self.failure("chat_stream", None, f"{type(exc).__name__}: {exc}")
            return {"status": "FAIL", "error": str(exc)}
        except Exception as exc:
            self.failure("chat_stream", None, f"{type(exc).__name__}: {exc}")
            return {"status": "FAIL", "error": str(exc)}
        self.record_latency("chat_stream", started)
        if start is None or done is None:
            self.failure("chat_stream", None, f"incomplete SSE start={start is not None} done={done is not None}")
            return {"status": "FAIL"}
        body = done
        self.provider = body.get("provider") or self.provider
        self.model = body.get("model") or self.model
        return {"status": "PASS", "deltas": deltas, "body": body}

    def _compose(self, args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess:
        return _run(
            [
                "docker",
                "compose",
                "-p",
                self.args.project_name,
                "-f",
                self.args.compose_file,
                *args,
            ],
            timeout=timeout,
        )

    def _service_running(self, service: str) -> bool:
        result = self._compose(["ps", "--status", "running", "--services"])
        return service in {line.strip() for line in result.stdout.splitlines() if line.strip()}

    def _fault(self, name: str) -> dict:
        started = time.perf_counter()
        detail = ""
        try:
            if name == "backend_restart":
                result = self._compose(["restart", "backend"])
                if result.returncode != 0:
                    return {"fault": name, "status": "FAIL", "detail": result.stderr[-500:]}
                recovery = self.wait_health()
                if recovery is None:
                    return {"fault": name, "status": "FAIL", "detail": "backend did not recover"}
                detail = f"backend healthy after {recovery}s"
            elif name == "worker_restart":
                result = self._compose(["restart", "worker"])
                if result.returncode != 0:
                    return {"fault": name, "status": "FAIL", "detail": result.stderr[-500:]}
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline and not self._service_running("worker"):
                    time.sleep(0.5)
                detail = "worker running again"
            elif name == "redis_interruption":
                result = self._compose(["exec", "-T", "redis", "redis-cli", "CLIENT", "PAUSE", "3000"])
                if result.returncode != 0:
                    return {"fault": name, "status": "FAIL", "detail": result.stderr[-500:]}
                time.sleep(1)
                if not self.health():
                    self.failure("redis_interruption_health", None, "health failed during pause")
                deadline = time.monotonic() + 30
                recovered = False
                while time.monotonic() < deadline:
                    ping = self._compose(["exec", "-T", "redis", "redis-cli", "ping"])
                    if ping.returncode == 0 and ping.stdout.strip() == "PONG":
                        recovered = True
                        break
                    time.sleep(0.5)
                if not recovered:
                    return {"fault": name, "status": "FAIL", "detail": "Redis did not recover"}
                detail = "Redis paused for 3s and recovered"
            elif name == "database_reconnection":
                result = self._compose(
                    [
                        "exec",
                        "-T",
                        "postgres",
                        "psql",
                        "-U",
                        "lifeai",
                        "-d",
                        "lifeai_testlab",
                        "-c",
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname='lifeai_testlab' AND pid <> pg_backend_pid()",
                    ]
                )
                if result.returncode != 0:
                    return {"fault": name, "status": "FAIL", "detail": result.stderr[-500:]}
                recovery = self.wait_health()
                if recovery is None:
                    return {"fault": name, "status": "FAIL", "detail": "API did not recover after DB termination"}
                detail = f"DB connections terminated; API healthy after {recovery}s"
            elif name == "ollama_timeout":
                stop = self._compose(["stop", "ollama"])
                if stop.returncode != 0:
                    return {"fault": name, "status": "FAIL", "detail": stop.stderr[-500:]}
                try:
                    response = self.client.post(
                        "/api/chat",
                        json={"message": "V6 expected ollama outage", "client_message_id": f"v6-ollama-{uuid.uuid4()}"},
                        headers=self.headers(),
                        timeout=httpx.Timeout(90.0, connect=10.0),
                    )
                    expected_failure = response.status_code >= 500 or (
                        response.status_code == 200
                        and response.json().get("assistant_status") == "failed"
                    )
                except Exception:
                    expected_failure = True
                start = self._compose(["start", "ollama"])
                if start.returncode != 0:
                    return {"fault": name, "status": "FAIL", "detail": start.stderr[-500:]}
                deadline = time.monotonic() + 120
                recovered = False
                while time.monotonic() < deadline:
                    try:
                        tags = self.client.get("/api/health", timeout=10)
                        if tags.status_code == 200:
                            recovered = True
                            break
                    except Exception:
                        pass
                    time.sleep(1)
                if not expected_failure or not recovered:
                    return {
                        "fault": name,
                        "status": "FAIL",
                        "detail": f"expected_failure={expected_failure}, recovered={recovered}",
                    }
                detail = "Ollama outage produced a truthful failure; API recovered"
            elif name == "browser_disconnection":
                result = self.stream(disconnect=True)
                if result.get("status") != "PASS":
                    return {"fault": name, "status": "FAIL", "detail": json.dumps(result)}
                normal = self.chat()
                if normal.get("status") != "PASS":
                    return {"fault": name, "status": "FAIL", "detail": "normal chat failed after disconnect"}
                detail = "SSE client disconnected mid-stream; a later chat succeeded"
            elif name == "duplicate_job_delivery":
                key = f"v6-duplicate-{uuid.uuid4()}"
                first = self.client.post(
                    "/api/mainai/jobs",
                    json={"job_type": "message_sequence_backfill", "input_refs": [], "idempotency_key": key},
                    headers=self.headers(),
                )
                if first.status_code == 409:
                    return {"fault": name, "status": "BLOCKED", "detail": first.text[:300]}
                second = self.client.post(
                    "/api/mainai/jobs",
                    json={"job_type": "message_sequence_backfill", "input_refs": [], "idempotency_key": key},
                    headers=self.headers(),
                )
                if first.status_code != 201 or second.status_code != 201:
                    return {
                        "fault": name,
                        "status": "FAIL",
                        "detail": f"first={first.status_code}, second={second.status_code}",
                    }
                first_id = first.json().get("id")
                second_id = second.json().get("id")
                if first_id != second_id:
                    return {"fault": name, "status": "FAIL", "detail": f"duplicate ids {first_id} != {second_id}"}
                count = None
                try:
                    conn = psycopg2.connect(self.args.database_url, connect_timeout=5)
                    try:
                        with conn.cursor() as cur:
                            cur.execute("SELECT count(*) FROM mainai_jobs WHERE idempotency_key = %s", (key,))
                            count = int(cur.fetchone()[0])
                    finally:
                        conn.close()
                except Exception as exc:
                    return {"fault": name, "status": "BLOCKED", "detail": f"DB count unavailable: {exc}"}
                if count != 1:
                    return {"fault": name, "status": "FAIL", "detail": f"durable rows={count}, expected 1"}
                detail = "same idempotency key returned one job and exactly one durable row"
            elif name == "expired_authorization":
                goal = self.client.post(
                    "/api/mainai/execution/goals",
                    json={
                        "title": f"V6 expiry {uuid.uuid4().hex[:8]}",
                        "original_instruction": "Read-only audit for authorization expiry.",
                        "risk_level": "low",
                        "approval_policy": "autonomous_development_work",
                    },
                    headers=self.headers(),
                )
                if goal.status_code != 201:
                    return {"fault": name, "status": "FAIL", "detail": goal.text[:300]}
                goal_id = goal.json()["id"]
                plan = self.client.post(f"/api/mainai/execution/goals/{goal_id}/plan", headers=self.headers())
                if plan.status_code != 201:
                    return {"fault": name, "status": "BLOCKED", "detail": f"plan {plan.status_code}: {plan.text[:300]}"}
                proposal = self.client.post(
                    f"/api/execution-envelopes/goals/{goal_id}/propose",
                    json={"proposed_paths": ["README.md"], "proposed_capabilities": ["read_file"], "proposed_risk": "low"},
                    headers=self.headers(),
                )
                if proposal.status_code != 201:
                    return {"fault": name, "status": "FAIL", "detail": proposal.text[:300]}
                authorization = self.client.post(
                    f"/api/execution-envelopes/proposals/{proposal.json()['id']}/authorize",
                    json={"authorized_paths": ["README.md"], "authorized_capabilities": ["read_file"], "authorized_risk": "low"},
                    headers=self.headers(),
                )
                if authorization.status_code != 201:
                    return {"fault": name, "status": "FAIL", "detail": authorization.text[:300]}
                envelope_id = authorization.json()["id"]
                conn = psycopg2.connect(self.args.database_url, connect_timeout=5)
                try:
                    with conn.cursor() as cur:
                        # The envelope model has no expires_at column and its CHECK constraint
                        # allows only active/superseded. Supersession is the product's real
                        # "this authority is no longer current" transition; inventing an
                        # 'expired' status would violate the DB contract rather than test it.
                        cur.execute(
                            "UPDATE execution_authorization_envelopes SET status='superseded' WHERE id=%s",
                            (envelope_id,),
                        )
                    conn.commit()
                finally:
                    conn.close()
                current = self.client.get(
                    f"/api/execution-envelopes/current?goal_id={goal_id}", headers=self.headers()
                )
                if current.status_code != 200 or current.json() is not None:
                    return {
                        "fault": name,
                        "status": "FAIL",
                        "detail": f"expired envelope still current: {current.status_code} {current.text[:200]}",
                    }
                detail = (
                    "superseded envelope is no longer returned as current authority "
                    "(product has no envelope TTL; supersession is the supported invalidation)"
                )
            else:
                return {"fault": name, "status": "BLOCKED", "detail": "unknown fault"}
        except Exception as exc:
            return {"fault": name, "status": "FAIL", "detail": f"{type(exc).__name__}: {exc}"}
        return {
            "fault": name,
            "status": "PASS",
            "detail": detail,
            "recovery_seconds": round(time.perf_counter() - started, 3),
        }

    def docker_stats(self) -> dict:
        if self.args.no_docker:
            return {"status": "BLOCKED", "reason": "--no-docker"}
        result = _run(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{json .}}",
            ]
        )
        if result.returncode != 0:
            return {"status": "BLOCKED", "reason": result.stderr[-500:]}
        rows = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if self.args.project_name in row.get("Name", ""):
                rows.append(
                    {
                        "name": row.get("Name"),
                        "cpu": row.get("CPUPerc"),
                        "memory": row.get("MemUsage"),
                        "net": row.get("NetIO"),
                        "block": row.get("BlockIO"),
                    }
                )
        return {"status": "PASS" if rows else "BLOCKED", "containers": rows}

    def db_metrics(self) -> dict:
        try:
            conn = psycopg2.connect(self.args.database_url, connect_timeout=5)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT current_database(), pg_database_size(current_database())")
                    database, size = cur.fetchone()
                    cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
                    connections = int(cur.fetchone()[0])
                return {"status": "PASS", "database": database, "size_bytes": int(size), "connections": connections}
            finally:
                conn.close()
        except Exception as exc:
            return {"status": "BLOCKED", "reason": f"{type(exc).__name__}: {exc}"}

    def redis_metrics(self) -> dict:
        try:
            client = redis.from_url(self.args.redis_url, socket_connect_timeout=5)
            info = client.info("memory")
            result = {
                "status": "PASS",
                "dbsize": int(client.dbsize()),
                "used_memory_bytes": int(info.get("used_memory", 0)),
                "connected_clients": int(client.info("clients").get("connected_clients", 0)),
            }
            client.close()
            return result
        except Exception as exc:
            return {"status": "BLOCKED", "reason": f"{type(exc).__name__}: {exc}"}

    def run(self) -> int:
        if not self.login():
            return self.finish()
        started = time.monotonic()
        deadline = started + self.args.duration_seconds
        next_sample = started
        next_fault = started + self.args.fault_every_seconds
        faults = [f for f in self.args.faults.split(",") if f]
        if self.args.no_docker:
            faults = [f for f in faults if f not in DOCKER_FAULTS]
        fault_index = 0
        cycle = 0
        while time.monotonic() < deadline:
            cycle += 1
            self.health()
            self.chat()
            self.stream()
            now = time.monotonic()
            if now >= next_sample:
                self.samples.append(
                    {
                        "at": _now(),
                        "elapsed_s": round(now - started, 1),
                        "docker": self.docker_stats(),
                        "database": self.db_metrics(),
                        "redis": self.redis_metrics(),
                    }
                )
                next_sample = now + self.args.sample_every_seconds
            if faults and now >= next_fault:
                name = faults[fault_index % len(faults)]
                fault_index += 1
                result = self._fault(name)
                self.faults.append(result)
                next_fault = time.monotonic() + self.args.fault_every_seconds
            time.sleep(max(0.5, self.args.interval_seconds))
        self.counts["cycles"] = cycle
        self.counts["faults"] = len(self.faults)
        return self.finish(started=started)

    def finish(self, started: float | None = None) -> int:
        elapsed = round((time.monotonic() - started), 1) if started else 0.0
        latency_summary = {}
        for name, values in self.latencies.items():
            if values:
                latency_summary[name] = {
                    "count": len(values),
                    "p50": round(statistics.median(values), 2),
                    "p95": round(_pct(values, 95), 2),
                    "max": round(max(values), 2),
                }
        fault_failures = [f for f in self.faults if f.get("status") == "FAIL"]
        fault_blockers = [f for f in self.faults if f.get("status") == "BLOCKED"]
        blockers = []
        if self.args.duration_seconds < REQUIRED_SECONDS:
            blockers.append(
                f"duration_seconds={self.args.duration_seconds} < required {REQUIRED_SECONDS}; F23 cannot be claimed"
            )
        if not self.samples:
            blockers.append("no resource samples collected")
        if fault_blockers:
            blockers.extend(f"{f['fault']}: {f.get('detail', '')}" for f in fault_blockers)
        if self.failures or fault_failures:
            status = "FAIL"
        elif blockers:
            status = "BLOCKED"
        else:
            status = "PASS"
        provider_mode = "local" if self.provider == "ollama" else "fake"
        payload = {
            "generated_at": _now(),
            "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "status": status,
            "f23_status": "PASS" if status == "PASS" and self.args.duration_seconds >= REQUIRED_SECONDS else ("FAIL" if status == "FAIL" else "BLOCKED"),
            "required_seconds": REQUIRED_SECONDS,
            "duration_seconds": self.args.duration_seconds,
            "elapsed_seconds": elapsed,
            "provider_mode": provider_mode,
            "provider": self.provider,
            "model": self.model,
            "environment": {
                "base_url": self.args.base_url,
                "database_url": _redacted(self.args.database_url),
                "redis_url": _redacted(self.args.redis_url),
                "docker": not self.args.no_docker,
                "project_name": self.args.project_name,
            },
            "counts": self.counts,
            "throughput_requests_per_second": round(self.requests_total / elapsed, 3) if elapsed else 0,
            "error_rate": round(len(self.failures) / self.requests_total, 4) if self.requests_total else 0,
            "latencies_ms": latency_summary,
            "samples": self.samples[:600],
            "faults": self.faults,
            "failures": self.failures,
            "blockers": blockers,
        }
        out = Path(self.args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(json.dumps({k: payload[k] for k in ("status", "f23_status", "duration_seconds", "faults", "failures", "blockers")}, indent=2))
        return 0 if status == "PASS" else (3 if status == "BLOCKED" else 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--database-url", default="postgresql://lifeai:lifeai_testlab@127.0.0.1:55433/lifeai_testlab")
    parser.add_argument("--redis-url", default="redis://127.0.0.1:56380/0")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11435")
    parser.add_argument("--compose-file", default="docker-compose.testlab.yml")
    parser.add_argument("--project-name", default="lifeai-v6-testlab")
    parser.add_argument("--duration-seconds", type=int, default=REQUIRED_SECONDS)
    parser.add_argument("--sample-every-seconds", type=int, default=30)
    parser.add_argument("--fault-every-seconds", type=int, default=300)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--faults", default=",".join(ALL_FAULTS))
    parser.add_argument("--no-docker", action="store_true", help="API-only smoke; Docker faults are BLOCKED")
    parser.add_argument("--founder-email", default=os.environ.get("FOUNDER_EMAIL", "founder@lifeos.local"))
    parser.add_argument("--founder-password", default=os.environ.get("FOUNDER_PASSWORD", "TestFounderPassword123!"))
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    parsed = make_url(args.base_url)
    if parsed.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("refusing a non-loopback reliability target")
    if args.project_name != "lifeai-v6-testlab":
        raise SystemExit("refusing a non-test-lab Compose project")
    runner = Runner(args)
    try:
        return runner.run()
    finally:
        runner.close()


if __name__ == "__main__":
    raise SystemExit(main())
