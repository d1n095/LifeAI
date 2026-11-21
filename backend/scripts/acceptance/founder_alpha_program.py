#!/usr/bin/env python3
"""Founder Alpha engineering-program runner.

This is an orchestrator over the existing V5-V9 acceptance tools, not a second test
framework. It provisions a disposable PostgreSQL database, applies the real migration and
runtime-grant order, starts the real API + durable worker through the existing E2E harness,
then runs the founder journey and the independent recovery, backup, upgrade, resource, soak,
security and browser checks.

The summary is fail-closed: a missing/broken optional capability is BLOCKED, never PASS. In
particular a short soak cannot satisfy the 24-hour endurance requirement and a browser launch
failure is not a browser PASS.

Safety:
  * every database name is generated under ``lifeos_program_*`` and dropped afterwards;
  * the runner refuses non-loopback database/Redis targets;
  * it never deploys, pushes, merges or activates autonomous execution;
  * logs contain no credentials and URLs are redacted in the summary.

Example:
    cd backend
    python scripts/acceptance/founder_alpha_program.py \
      --admin-url postgresql://postgres@127.0.0.1:55432/postgres \
      --redis-url redis://127.0.0.1:56379/14 \
      --pg-bin /tmp/lifeai-testenv/root/usr/lib/postgresql/16/bin \
      --out-dir ../docs/evidence/founder-alpha/program
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import psycopg2
import redis
from psycopg2 import sql
from sqlalchemy.engine import make_url

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent
REQUIRED_SOAK_SECONDS = 24 * 60 * 60
TEST_FILES = (
    "tests/backend/test_health.py",
    "tests/backend/test_projects_detail.py",
    "tests/backend/test_security_tokens.py",
    "tests/backend/test_rls_policy_registry.py",
    "tests/backend/test_startup_checks.py",
    "tests/backend/test_db_retry.py",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redacted(url: str) -> str:
    try:
        parsed = make_url(url)
    except Exception:
        return "<unparseable-url>"
    return (
        f"{parsed.drivername}://{parsed.username or ''}@"
        f"{parsed.host or ''}:{parsed.port or ''}/{parsed.database or ''}"
    )


def _run(
    cmd: list[str],
    *,
    cwd: Path = BACKEND_ROOT,
    env: dict[str, str] | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(errors="replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(errors="replace")
        return subprocess.CompletedProcess(cmd, 124, stdout=stdout, stderr=stderr)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _db_url(admin_url: str, name: str) -> str:
    return str(make_url(admin_url).set(database=name))


def _app_url(admin_url: str, name: str, password: str) -> str:
    return str(
        make_url(admin_url)
        .set(database=name)
        .set(username="mainai_app", password=password)
    )


def _create_database(admin_url: str, name: str) -> None:
    conn = psycopg2.connect(admin_url, connect_timeout=5)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    finally:
        conn.close()
    db_conn = psycopg2.connect(_db_url(admin_url, name), connect_timeout=5)
    db_conn.autocommit = True
    try:
        with db_conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        db_conn.close()


def _drop_database(admin_url: str, name: str) -> None:
    conn = psycopg2.connect(admin_url, connect_timeout=5)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
    finally:
        conn.close()


def _alembic_head(env: dict[str, str]) -> str:
    result = _run([sys.executable, "-m", "alembic", "heads"], env=env)
    match = re.search(r"^\s*([0-9A-Za-z_]+)\s+\(head\)", result.stdout, re.MULTILINE)
    if result.returncode != 0 or not match:
        raise RuntimeError((result.stdout + result.stderr)[-1200:])
    return match.group(1)


def _alembic_current(env: dict[str, str]) -> str | None:
    result = _run([sys.executable, "-m", "alembic", "current"], env=env)
    if result.returncode != 0:
        return None
    match = re.search(r"^\s*([0-9A-Za-z_]+)(?:\s+\(head\))?\s*$", result.stdout, re.MULTILINE)
    return match.group(1) if match else None


def _junit_totals(path: Path) -> dict[str, int]:
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return {}
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
    return {
        "tests": sum(int(s.attrib.get("tests", 0)) for s in suites),
        "failures": sum(int(s.attrib.get("failures", 0)) for s in suites),
        "errors": sum(int(s.attrib.get("errors", 0)) for s in suites),
        "skipped": sum(int(s.attrib.get("skipped", 0)) for s in suites),
    }


class Program:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.out = Path(args.out_dir).resolve()
        self.run_id = uuid.uuid4().hex[:10]
        self.temp = self.out / f"run-{self.run_id}"
        self.temp.mkdir(parents=True, exist_ok=True)
        self.checks: list[dict[str, Any]] = []
        self.artifacts: dict[str, Any] = {}
        self.created_databases: list[str] = []
        self.backend: subprocess.Popen | None = None
        self.backend_log: Path | None = None
        self.port: int | None = None
        self.db_name = f"lifeos_program_{self.run_id}"
        self.db_url = _db_url(args.admin_url, self.db_name)
        self.app_url = _app_url(args.admin_url, self.db_name, args.app_password)
        self.storage = self.temp / "storage"
        self.storage.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ reporting
    def check(self, name: str, status: str, detail: Any, data: dict | None = None) -> None:
        assert status in {"PASS", "FAIL", "BLOCKED"}
        entry = {
            "check": name,
            "status": status,
            "detail": detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)[:2000],
            "data": data or {},
            "at": _now(),
        }
        self.checks.append(entry)
        print(f"[{status}] {name}: {entry['detail'][:300]}", flush=True)

    def ok(self, name: str, passed: bool, detail: Any, data: dict | None = None) -> None:
        self.check(name, "PASS" if passed else "FAIL", detail, data)

    def blocked(self, name: str, detail: str, data: dict | None = None) -> None:
        self.check(name, "BLOCKED", detail, data)

    # ------------------------------------------------------------------ environment
    def _env(self, *, port: int | None = None, redis_db: str = "14", cookie_secure: bool = False) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "DATABASE_URL": self.db_url,
                "APP_DATABASE_URL": self.app_url,
                "MAINAI_APP_PASSWORD": self.args.app_password,
                "REDIS_URL": self.args.redis_url if redis_db == "14" else f"redis://127.0.0.1:{make_url(self.args.redis_url).port or 6379}/{redis_db}",
                "SECRET_KEY": "founder-alpha-program-local-secret",
                "FOUNDER_EMAIL": self.args.founder_email,
                "FOUNDER_PASSWORD": self.args.founder_password,
                "E2E_FOUNDER_PASSWORD": self.args.founder_password,
                "E2E_DATABASE_URL": self.db_url,
                "E2E_EMAIL_LOG_PATH": str(self.temp / "emails.jsonl"),
                "E2E_BACKEND_PORT": str(port or self.args.api_port or _free_port()),
                "STORAGE_ROOT": str(self.storage),
                "ENVIRONMENT": "test",
                "COOKIE_SECURE": "true" if cookie_secure else "false",
                "ENABLE_SCHEDULED_CLEANUP": "false",
                "FRONTEND_ORIGINS": "http://127.0.0.1:3020",
                "PUBLIC_APP_URL": "http://127.0.0.1:3020",
                "OPENAI_API_KEY": "sk-founder-alpha-program-local",
                "DEFAULT_LLM_PROVIDER": "openai",
                "DEFAULT_LLM_MODEL": "gpt-4o-mini",
                "OLLAMA_BASE_URL": self.args.ollama_url,
                "RATE_LIMIT_LOGIN_PER_MINUTE": "10000",
                "RATE_LIMIT_REGISTER_PER_MINUTE": "10000",
                "RATE_LIMIT_FORGOT_PASSWORD_PER_MINUTE": "10000",
                "RATE_LIMIT_RESET_PASSWORD_PER_MINUTE": "10000",
                "RATE_LIMIT_VERIFY_EMAIL_PER_MINUTE": "10000",
                "RATE_LIMIT_CHAT_PER_MINUTE": "10000",
                "RATE_LIMIT_DEFAULT_PER_MINUTE": "10000",
                "PYTHONPATH": ".",
                "RENDER_ENV_FILE": str(self.temp / "render-env.sh"),
            }
        )
        return env

    def _install(self) -> None:
        env = self._env()
        steps: list[dict] = []
        for label, command in (
            ("ensure_app_role", [sys.executable, "scripts/security/ensure_app_role.py"]),
            ("alembic_upgrade_head", [sys.executable, "-m", "alembic", "upgrade", "head"]),
            ("apply_runtime_privileges", [sys.executable, "scripts/security/apply_runtime_privileges.py"]),
        ):
            result = _run(command, env=env, timeout=300)
            steps.append(
                {
                    "step": label,
                    "exit_code": result.returncode,
                    "stdout_tail": result.stdout[-500:],
                    "stderr_tail": result.stderr[-500:],
                }
            )
            if result.returncode != 0:
                break
        self.ok(
            "fresh installation order",
            all(step["exit_code"] == 0 for step in steps),
            "ensure_app_role -> alembic upgrade head -> apply_runtime_privileges",
            {"steps": steps},
        )
        head = _alembic_head(env)
        current = _alembic_current(env)
        self.ok("database at Alembic head", current == head, f"current={current}, head={head}", {"head": head})
        self.artifacts["migration_head"] = head

    # ------------------------------------------------------------------ backend lifecycle
    def _start_backend(self, *, cookie_secure: bool = False) -> None:
        port = self.args.api_port or _free_port()
        self.port = port
        env = self._env(port=port, cookie_secure=cookie_secure)
        self.backend_log = self.temp / f"backend-{port}.log"
        log = self.backend_log.open("w", encoding="utf-8")
        self.backend = subprocess.Popen(
            [sys.executable, "scripts/ci/run_e2e_backend.py"],
            cwd=BACKEND_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        log.close()
        started = time.perf_counter()
        deadline = started + 120
        last: Exception | None = None
        while time.perf_counter() < deadline:
            if self.backend.poll() is not None:
                raise RuntimeError(
                    f"backend exited during startup ({self.backend.returncode}): "
                    f"{self.backend_log.read_text(errors='replace')[-1200:]}"
                )
            try:
                live = httpx.get(f"http://127.0.0.1:{port}/api/health/live", timeout=2.0)
                if live.status_code == 200:
                    ready = httpx.get(f"http://127.0.0.1:{port}/api/health/ready", timeout=5.0)
                    self.artifacts["startup_seconds"] = round(time.perf_counter() - started, 2)
                    self.ok("liveness endpoint", True, live.text, {"status_code": live.status_code})
                    self.ok("readiness endpoint", ready.status_code == 200, ready.text, {"status_code": ready.status_code})
                    return
            except Exception as exc:  # noqa: BLE001 - startup polling
                last = exc
            time.sleep(0.25)
        raise RuntimeError(f"backend did not become live within 120s: {last}")

    def _stop_backend(self) -> None:
        if self.backend is None:
            return
        if self.backend.poll() is None:
            self.backend.terminate()
            try:
                self.backend.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.backend.kill()
                self.backend.wait(timeout=10)
        self.backend = None

    def _client(self, *, timeout: float = 120.0) -> httpx.Client:
        if self.port is None:
            raise RuntimeError("backend is not running")
        return httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=timeout, follow_redirects=False)

    # ------------------------------------------------------------------ contracts
    def _contracts(self) -> None:
        with httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=30.0) as anonymous:
            protected = {
                "library": "/api/library",
                "mainai_goals": "/api/mainai/execution/goals",
                "agent_tasks": "/api/admin/agents/tasks",
                "incidents": "/api/incidents/health",
            }
            statuses = {name: anonymous.get(path).status_code for name, path in protected.items()}
            self.ok(
                "anonymous protected routes rejected",
                all(code in (401, 403) for code in statuses.values()),
                statuses,
            )
            wrong = anonymous.post(
                "/api/auth/login",
                json={"email": self.args.founder_email, "password": "definitely-wrong-password"},
            )
            self.ok("wrong password rejected", wrong.status_code == 401, {"status_code": wrong.status_code})

        with self._client() as client:
            login = client.post(
                "/api/auth/login",
                json={"email": self.args.founder_email, "password": self.args.founder_password},
            )
            if login.status_code != 200:
                self.ok("founder login", False, f"{login.status_code}: {login.text[:300]}")
                return
            body = login.json()
            csrf = body.get("csrf_token", "")
            self.ok("founder login", bool(csrf), {"user_id": body.get("id"), "email": body.get("email")})
            me = client.get("/api/auth/me")
            self.ok("session /me", me.status_code == 200, {"status_code": me.status_code})
            refresh = client.post("/api/auth/refresh", headers={"X-CSRF-Token": csrf})
            if refresh.status_code == 200:
                csrf = refresh.json().get("csrf_token", csrf)
            self.ok("session refresh", refresh.status_code == 200, {"status_code": refresh.status_code})

            missing_csrf = client.post(
                "/api/projects",
                json={"name": f"Program CSRF {self.run_id}", "description": "must be rejected"},
            )
            self.ok("mutating request without CSRF rejected", missing_csrf.status_code == 403, {"status_code": missing_csrf.status_code})

            project = client.post(
                "/api/projects",
                json={"name": f"Program project {self.run_id}", "description": "isolated acceptance project"},
                headers={"X-CSRF-Token": csrf},
            )
            if project.status_code == 201:
                project_id = project.json()["id"]
                task = client.post(
                    "/api/projects/tasks",
                    json={
                        "project_id": project_id,
                        "title": f"Program task {self.run_id}",
                        "description": "scoped acceptance task",
                        "status": "todo",
                        "priority": "medium",
                    },
                    headers={"X-CSRF-Token": csrf},
                )
                detail = client.get(f"/api/projects/{project_id}")
                detail_body = detail.json() if detail.status_code == 200 else {}
                self.ok(
                    "project create, task and scoped detail",
                    project.status_code in (200, 201)
                    and task.status_code == 200
                    and detail.status_code == 200
                    and any(str(item.get("id")) == str(task.json().get("id")) for item in detail_body.get("tasks") or []),
                    {
                        "project": project.status_code,
                        "task": task.status_code,
                        "detail": detail.status_code,
                        "project_id": project_id,
                    },
                )
            else:
                self.ok("project create, task and scoped detail", False, f"project create {project.status_code}: {project.text[:300]}")

            logout = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
            self.ok("logout", logout.status_code in (200, 204), {"status_code": logout.status_code})
            after = client.get("/api/library")
            self.ok("logout invalidates protected access", after.status_code in (401, 403), {"status_code": after.status_code})

    # ------------------------------------------------------------------ founder journey
    def _journey(self) -> None:
        evidence = self.temp / "founder-journey.json"
        result = _run(
            [
                sys.executable,
                "scripts/acceptance/v6_founder_journey.py",
                "--base-url",
                f"http://127.0.0.1:{self.port}",
                "--database-url",
                self.db_url,
                "--redis-url",
                self.args.redis_url,
                "--allow-fake-provider",
                "--no-require-local-provider",
                "--evidence",
                str(evidence),
                "--ingest-timeout",
                "180",
                "--execution-timeout",
                "180",
                "--cancel-timeout",
                "30",
            ],
            env=self._env(port=self.port or 8011),
            timeout=600,
        )
        data = _read_json(evidence) or {}
        self.artifacts["founder_journey"] = data.get("artifacts") or {}
        self.artifacts["founder_journey_steps"] = [
            {"step": item.get("step"), "status": item.get("status"), "detail": item.get("detail")}
            for item in data.get("steps") or []
        ]
        self.ok(
            "12-step founder journey",
            result.returncode == 0 and data.get("status") == "PASS",
            {
                "exit": result.returncode,
                "status": data.get("status"),
                "failures": data.get("failures"),
                "blockers": data.get("blockers"),
                "provider_mode": data.get("provider_mode"),
            },
            {"evidence": str(evidence.relative_to(self.out)) if evidence.exists() else str(evidence)},
        )

    # ------------------------------------------------------------------ restart recovery
    def _restart(self) -> None:
        artifacts = self.artifacts.get("founder_journey") or {}
        self._stop_backend()
        self._start_backend()
        with self._client() as client:
            login = client.post(
                "/api/auth/login",
                json={"email": self.args.founder_email, "password": self.args.founder_password},
            )
            if login.status_code != 200:
                self.ok("restart preserves state", False, f"login after restart {login.status_code}")
                return
            csrf = login.json().get("csrf_token", "")
            conversation_id = artifacts.get("conversation_id")
            source_id = artifacts.get("source_id")
            task_id = artifacts.get("task_id")
            conversation = client.get(f"/api/conversations/{conversation_id}", headers={"X-CSRF-Token": csrf}) if conversation_id else None
            source = client.get(f"/api/library/{source_id}", headers={"X-CSRF-Token": csrf}) if source_id else None
            task = client.get(f"/api/mainai/execution/tasks/{task_id}", headers={"X-CSRF-Token": csrf}) if task_id else None
            conversation_ok = bool(
                conversation
                and conversation.status_code == 200
                and len(conversation.json().get("messages") or []) >= 2
            )
            source_ok = bool(
                source
                and source.status_code == 200
                and source.json().get("status") == "indexed"
                and int(source.json().get("chunk_count") or 0) > 0
            )
            task_ok = bool(task and task.status_code == 200 and task.json().get("events"))
            self.ok(
                "restart preserves conversation, source and task evidence",
                conversation_ok and source_ok and task_ok,
                {
                    "conversation": conversation.status_code if conversation else None,
                    "source": source.status_code if source else None,
                    "task": task.status_code if task else None,
                    "conversation_ok": conversation_ok,
                    "source_ok": source_ok,
                    "task_ok": task_ok,
                },
            )

    # ------------------------------------------------------------------ recovery / backup
    def _recovery_matrix(self) -> None:
        out = self.temp / "recovery-matrix.json"
        result = _run(
            [
                sys.executable,
                "scripts/acceptance/recovery_matrix.py",
                "--source-url",
                self.db_url,
                "--pg-bin",
                self.args.pg_bin,
                "--out",
                str(out),
            ],
            env=self._env(),
            timeout=300,
        )
        data = _read_json(out) or {}
        self.ok(
            "backup/restore corrupt and partial rejection",
            result.returncode == 0 and data.get("status") == "PASS",
            {"exit": result.returncode, "status": data.get("status"), "checks": data.get("checks")},
        )

    def _backup_restore(self) -> None:
        restore_name = f"lifeos_program_restore_{self.run_id}"
        self.created_databases.append(restore_name)
        restore_url = _db_url(self.args.admin_url, restore_name)
        restore_app = _app_url(self.args.admin_url, restore_name, self.args.app_password)
        out = self.temp / "backup-restore.json"
        port = _free_port()
        result = _run(
            [
                sys.executable,
                "scripts/acceptance/backup_restore_check.py",
                "--source-url",
                self.db_url,
                "--restore-url",
                restore_url,
                "--restore-app-url",
                restore_app,
                "--pg-bin",
                self.args.pg_bin,
                "--redis-url",
                self.args.redis_url,
                "--api-port",
                str(port),
                "--seed-other-user",
                "--cleanup-on-failure",
                "--out",
                str(out),
            ],
            env=self._env(),
            timeout=420,
        )
        data = _read_json(out) or {}
        self.ok(
            "backup restore into disposable database and API verification",
            result.returncode == 0 and data.get("status") == "PASS",
            {
                "exit": result.returncode,
                "status": data.get("status"),
                "summary": data.get("summary"),
                "stderr_tail": result.stderr[-800:],
            },
        )

    def _upgrade(self) -> None:
        name = f"lifeos_program_upgrade_{self.run_id}"
        self.created_databases.append(name)
        _create_database(self.args.admin_url, name)
        env = self._env()
        env["DATABASE_URL"] = _db_url(self.args.admin_url, name)
        env["APP_DATABASE_URL"] = _app_url(self.args.admin_url, name, self.args.app_password)
        steps = []
        for label, command in (
            ("upgrade_head", [sys.executable, "-m", "alembic", "upgrade", "head"]),
            ("downgrade_one", [sys.executable, "-m", "alembic", "downgrade", "-1"]),
            ("upgrade_head_again", [sys.executable, "-m", "alembic", "upgrade", "head"]),
        ):
            result = _run(command, env=env, timeout=300)
            current = _alembic_current(env)
            steps.append({"step": label, "exit_code": result.returncode, "current": current})
        final_head = _alembic_head(env)
        self.ok(
            "migration upgrade / rollback / re-upgrade",
            all(step["exit_code"] == 0 for step in steps) and steps[-1]["current"] == final_head,
            {"steps": steps, "final_head": final_head},
        )

    # ------------------------------------------------------------------ capacity / endurance
    def _resource_probe(self) -> None:
        out = self.temp / "resource-probe.json"
        result = _run(
            [
                sys.executable,
                "scripts/acceptance/resource_probe.py",
                "--database-url",
                self.db_url,
                "--app-database-url",
                self.app_url,
                "--redis-url",
                self.args.redis_url,
                "--out",
                str(out),
            ],
            env=self._env(),
            timeout=300,
        )
        data = _read_json(out) or {}
        status = data.get("status") or ("FAIL" if result.returncode != 0 else "PASS")
        self.check(
            "resource and latency probe",
            status if status in {"PASS", "FAIL", "BLOCKED"} else "FAIL",
            {
                "exit": result.returncode,
                "status": status,
                "latencies": data.get("latencies_ms") or data.get("api_latency_ms"),
                "blockers": data.get("blockers"),
            },
        )

    def _soak(self) -> None:
        out = self.temp / "soak.json"
        duration = max(5, self.args.soak_seconds)
        result = _run(
            [
                sys.executable,
                "scripts/acceptance/v5_soak.py",
                "--database-url",
                self.db_url,
                "--app-database-url",
                self.app_url,
                "--redis-url",
                self.args.redis_url,
                "--duration-seconds",
                str(duration),
                "--sample-every-seconds",
                "5",
                "--out",
                str(out),
            ],
            env=self._env(),
            timeout=max(120, duration + 90),
        )
        data = _read_json(out) or {}
        status = data.get("status") or ("FAIL" if result.returncode not in (0, 3) else "BLOCKED")
        if duration < REQUIRED_SOAK_SECONDS:
            status = "BLOCKED"
        self.check(
            "endurance soak",
            status if status in {"PASS", "FAIL", "BLOCKED"} else "BLOCKED",
            {
                "exit": result.returncode,
                "status": status,
                "duration_seconds": duration,
                "required_seconds": REQUIRED_SOAK_SECONDS,
                "f23_status": "BLOCKED" if duration < REQUIRED_SOAK_SECONDS else data.get("f23_status"),
                "blockers": data.get("blockers"),
            },
        )

    # ------------------------------------------------------------------ regression / security
    def _pytest(self) -> None:
        junit = self.temp / "backend-tests.xml"
        db_name = f"lifeos_program_pytest_{self.run_id}"
        env = self._env(redis_db="13")
        env.update(
            {
                "DATABASE_URL": _db_url(self.args.admin_url, db_name),
                "APP_DATABASE_URL": _app_url(self.args.admin_url, db_name, self.args.app_password),
                "LIFEAI_TEST_DATABASE_NAME": db_name,
                "OPENAI_API_KEY": "fake-key-for-tests",
            }
        )
        result = _run(
            [sys.executable, "-m", "pytest", *TEST_FILES, "-q", "--junitxml", str(junit)],
            env=env,
            timeout=900,
        )
        totals = _junit_totals(junit)
        self.ok(
            "security, health and regression tests",
            result.returncode == 0 and totals.get("tests", 0) > 0 and totals.get("failures") == 0 and totals.get("errors") == 0,
            {"exit": result.returncode, "totals": totals, "tail": (result.stdout + result.stderr)[-1000:]},
        )

    # ------------------------------------------------------------------ browser
    def _browser(self) -> None:
        if self.args.skip_browser:
            self.blocked("browser acceptance", "skipped by --skip-browser")
            return
        frontend = REPO_ROOT / "frontend"
        if not (frontend / "node_modules").exists():
            self.blocked("browser acceptance", "frontend/node_modules is absent")
            return
        # Chromium treats localhost as a secure context and rejects the session cookie when
        # the backend sets COOKIE_SECURE=false. The API checks above use httpx over plain
        # HTTP, so they need the non-secure local setting; the browser leg therefore gets its
        # own short-lived backend on a fresh port with secure cookies enabled.
        self._stop_backend()
        self._start_backend(cookie_secure=True)
        env = self._env(port=self.port or 8011, cookie_secure=True)
        env.update(
            {
                "CI": "1",
                "E2E_INTERNAL_API_URL": f"http://127.0.0.1:{self.port}",
                "E2E_FOUNDER_PASSWORD": self.args.founder_password,
            }
        )
        result = _run(
            [
                "npx",
                "playwright",
                "test",
                "e2e/founder-alpha-resilience.spec.ts",
                "--grep",
                "project detail",
                "--workers=1",
                "--reporter=json",
            ],
            cwd=frontend,
            env=env,
            timeout=240,
        )
        combined = result.stdout + result.stderr
        blocked = "error while loading shared libraries" in combined or "libatk" in combined
        if blocked:
            self.blocked(
                "browser acceptance",
                "Chromium could not start (missing shared library); no browser PASS claimed",
                {"exit": result.returncode, "tail": combined[-1200:]},
            )
        else:
            self.ok(
                "browser acceptance",
                result.returncode == 0,
                {"exit": result.returncode, "tail": combined[-1200:]},
            )

    # ------------------------------------------------------------------ finish
    def _finish(self) -> int:
        self._stop_backend()
        statuses = [entry["status"] for entry in self.checks]
        if "FAIL" in statuses:
            status = "FAIL"
        elif "BLOCKED" in statuses:
            status = "BLOCKED"
        else:
            status = "PASS"
        payload = {
            "generated_at": _now(),
            "commit": self.artifacts.get("commit"),
            "status": status,
            "run_id": self.run_id,
            "first_cycle_go": False,
            "checks": self.checks,
            "artifacts": self.artifacts,
            "environment": {
                "database": _redacted(self.db_url),
                "redis": _redacted(self.args.redis_url),
                "backend_port": self.port,
                "pg_bin": self.args.pg_bin,
            },
            "limitations": [
                "The founder journey and API checks use the existing deterministic E2E provider; real Ollama is not available.",
                "A soak shorter than 24 hours is BLOCKED for endurance, never PASS.",
                "Browser acceptance is BLOCKED when Chromium cannot load its system libraries.",
                "Docker daemon access is unavailable; no Docker/Compose lab was started.",
            ],
        }
        summary = self.out / "program-summary.json"
        summary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            f"# Founder Alpha engineering program — {status}",
            "",
            f"Commit: `{self.artifacts.get('commit')}`",
            f"Run: `{self.run_id}`",
            "",
            "| Check | Status | Detail |",
            "| --- | --- | --- |",
        ]
        for entry in self.checks:
            detail = entry["detail"].replace("|", "\\|").replace("\n", " ")[:300]
            lines.append(f"| {entry['check']} | **{entry['status']}** | {detail} |")
        lines.extend(
            [
                "",
                "## Evidence",
                "",
                "- `program-summary.json`",
                f"- `run-{self.run_id}/founder-journey.json`",
                f"- `run-{self.run_id}/recovery-matrix.json`",
                f"- `run-{self.run_id}/backup-restore.json`",
                f"- `run-{self.run_id}/resource-probe.json`",
                f"- `run-{self.run_id}/soak.json`",
                f"- `run-{self.run_id}/backend-tests.xml`",
                "",
                "## Limitations",
                "",
            ]
        )
        lines.extend(f"- {item}" for item in payload["limitations"])
        (self.out / "program-summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(json.dumps({"status": status, "commit": self.artifacts.get("commit"), "checks": len(self.checks)}, indent=2))
        if not self.args.keep_databases:
            for name in self.created_databases:
                try:
                    _drop_database(self.args.admin_url, name)
                except Exception:
                    pass
            try:
                _drop_database(self.args.admin_url, self.db_name)
            except Exception:
                pass
        return 0 if status == "PASS" else (3 if status == "BLOCKED" else 2)

    def run(self) -> int:
        try:
            head = _run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT).stdout.strip()
            self.artifacts["commit"] = head
            expected = self.args.expected_sha or head
            self.ok("exact commit SHA", head == expected, {"actual": head, "expected": expected})
            dirty = _run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=REPO_ROOT).stdout.strip()
            self.ok("tracked tree clean", not dirty, dirty or "clean")
            self.ok("pg_dump/pg_restore available", all((Path(self.args.pg_bin) / name).exists() for name in ("pg_dump", "pg_restore")), self.args.pg_bin)
            try:
                conn = psycopg2.connect(self.args.admin_url, connect_timeout=5)
                conn.close()
                self.ok("PostgreSQL reachable", True, _redacted(self.args.admin_url))
            except Exception as exc:
                self.ok("PostgreSQL reachable", False, f"{type(exc).__name__}: {exc}")
            try:
                client = redis.from_url(self.args.redis_url, socket_connect_timeout=5)
                client.ping()
                client.close()
                self.ok("Redis reachable", True, _redacted(self.args.redis_url))
            except Exception as exc:
                self.ok("Redis reachable", False, f"{type(exc).__name__}: {exc}")
            node = _run(["node", "--version"], cwd=REPO_ROOT)
            npm = _run(["npm", "--version"], cwd=REPO_ROOT)
            self.ok("Node/npm available", node.returncode == 0 and npm.returncode == 0, {"node": node.stdout.strip(), "npm": npm.stdout.strip()})

            _create_database(self.args.admin_url, self.db_name)
            self.created_databases.append(self.db_name)
            self._install()
            self._start_backend()
            self._contracts()
            self._journey()
            self._restart()
            self._recovery_matrix()
            self._backup_restore()
            self._upgrade()
            self._resource_probe()
            self._soak()
            self._pytest()
            self._browser()
        except Exception as exc:  # noqa: BLE001 - every failure is evidence
            self.check("program runner", "FAIL", f"{type(exc).__name__}: {exc}")
        return self._finish()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-url", default="postgresql://postgres@127.0.0.1:55432/postgres")
    parser.add_argument("--redis-url", default="redis://127.0.0.1:56379/14")
    parser.add_argument("--pg-bin", default=os.environ.get("PG_BIN", "/tmp/lifeai-testenv/root/usr/lib/postgresql/16/bin"))
    parser.add_argument("--app-password", default="mainai_app_pw")
    parser.add_argument("--founder-email", default="founder@lifeos.local")
    parser.add_argument("--founder-password", default="TestFounderPassword123!")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:1")
    parser.add_argument("--api-port", type=int, default=0)
    parser.add_argument("--soak-seconds", type=int, default=15)
    parser.add_argument("--out-dir", default="docs/evidence/founder-alpha/program")
    parser.add_argument("--expected-sha", default="")
    parser.add_argument("--keep-databases", action="store_true")
    parser.add_argument("--skip-browser", action="store_true")
    args = parser.parse_args()

    admin = make_url(args.admin_url)
    redis_url = make_url(args.redis_url)
    if admin.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("refusing a non-loopback PostgreSQL target")
    if redis_url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("refusing a non-loopback Redis target")
    os.environ.setdefault(
        "LD_LIBRARY_PATH",
        "/tmp/lifeai-testenv/root/usr/lib/x86_64-linux-gnu:/tmp/lifeai-testenv/root/usr/lib",
    )
    return Program(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
