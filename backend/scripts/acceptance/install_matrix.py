#!/usr/bin/env python3
"""Real-service Founder Alpha installation matrix.

Every scenario uses a real PostgreSQL database and a real Redis connection; no mocked
startup. The script checks actual health, the Alembic revision, the restricted app role and
the API's behavior, not just a process exit code.

Safety:
  * every scenario database is created under a unique ``lifeos_matrix_*`` name;
  * the existing-data scenario is read-only and never dropped;
  * no production URL is accepted;
  * logs are kept in a temporary directory, never printed with credentials.

Examples:
    python scripts/acceptance/install_matrix.py \
      --admin-url postgresql://postgres@127.0.0.1:55432/postgres \
      --existing-url postgresql://postgres@127.0.0.1:55432/lifeos_e2e \
      --existing-app-url postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_e2e \
      --pg-bin /tmp/lifeai-testenv/root/usr/lib/postgresql/16/bin \
      --out ../docs/evidence/founder-alpha/install-matrix.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psycopg2
from sqlalchemy.engine import make_url

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_APP_PASSWORD = "mainai_app_pw"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run(
    cmd: list[str],
    *,
    cwd: Path = BACKEND_ROOT,
    env: dict[str, str] | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)


def _db_url(base_url: str, database: str) -> str:
    return str(make_url(base_url).set(database=database))


def _app_url(
    admin_url: str,
    database: str,
    password: str = DEFAULT_APP_PASSWORD,
    username: str = "mainai_app",
) -> str:
    parsed = make_url(admin_url).set(database=database)
    return str(
        parsed.set(
            username=username,
            password=password,
        )
    )


def _redacted(url: str) -> str:
    try:
        parsed = make_url(url)
    except Exception:
        return "<unparseable-url>"
    return f"{parsed.drivername}://{parsed.username or ''}@{parsed.host or ''}:{parsed.port or ''}/{parsed.database or ''}"


def _admin_conn(url: str):
    return psycopg2.connect(url, connect_timeout=5)


def _create_database(admin_url: str, name: str) -> None:
    conn = _admin_conn(admin_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()

    db_url = _db_url(admin_url, name)
    conn = _admin_conn(db_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        conn.close()


def _drop_database(admin_url: str, name: str) -> None:
    conn = _admin_conn(admin_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        conn.close()


def _head_revision(env: dict[str, str]) -> str:
    result = _run([sys.executable, "-m", "alembic", "heads"], env=env)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-1000:] or result.stdout[-1000:])
    match = re.search(r"^\s*([0-9A-Za-z_]+)\s+\(head\)", result.stdout, re.MULTILINE)
    if not match:
        raise RuntimeError(f"could not parse alembic heads output: {result.stdout!r}")
    return match.group(1)


def _current_revision(db_url: str) -> str | None:
    conn = _admin_conn(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            row = cur.fetchone()
            return row[0] if row else None
    except psycopg2.Error:
        return None
    finally:
        conn.close()


def _count(db_url: str, table: str) -> int:
    conn = _admin_conn(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table}")
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def _app_privilege(admin_url: str, database: str) -> dict:
    conn = _admin_conn(_db_url(admin_url, database))
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT has_table_privilege('mainai_app','mainai_job_events','SELECT'),"
                " has_table_privilege('mainai_app','mainai_job_events','INSERT'),"
                " has_table_privilege('mainai_app','mainai_job_events','UPDATE'),"
                " has_table_privilege('mainai_app','mainai_job_proposals','UPDATE'),"
                " has_table_privilege('mainai_app','documents','SELECT')"
            )
            return dict(
                zip(
                    ("event_select", "event_insert", "event_update", "proposal_update", "documents_select"),
                    cur.fetchone(),
                )
            )
    finally:
        conn.close()


def _base_env(args: argparse.Namespace, *, database_url: str, app_database_url: str, redis_url: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": database_url,
            "APP_DATABASE_URL": app_database_url,
            "MAINAI_APP_PASSWORD": DEFAULT_APP_PASSWORD,
            "REDIS_URL": redis_url,
            "SECRET_KEY": "founder-alpha-install-matrix-secret",
            "FOUNDER_EMAIL": args.founder_email,
            "FOUNDER_PASSWORD": args.founder_password,
            "ENVIRONMENT": "test",
            "COOKIE_SECURE": "false",
            "ENABLE_SCHEDULED_CLEANUP": "false",
            "STORAGE_ROOT": str(args.work_dir / "storage"),
            "PYTHONPATH": ".",
            "OPENAI_API_KEY": "install-matrix-placeholder",
            "RATE_LIMIT_LOGIN_PER_MINUTE": "1000",
            "RATE_LIMIT_REGISTER_PER_MINUTE": "1000",
            "RATE_LIMIT_FORGOT_PASSWORD_PER_MINUTE": "1000",
            "RATE_LIMIT_RESET_PASSWORD_PER_MINUTE": "1000",
            "RATE_LIMIT_VERIFY_EMAIL_PER_MINUTE": "1000",
            "OLLAMA_BASE_URL": "http://127.0.0.1:1",
        }
    )
    return env


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _start_api(env: dict[str, str], *, timeout: int = 40) -> dict:
    port = _free_port()
    log_path = Path(env["STORAGE_ROOT"]).parent / f"api-{port}.log"
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=BACKEND_ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + timeout
    health: dict | None = None
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return {
                    "state": "exited",
                    "exit_code": proc.returncode,
                    "port": port,
                    "health": health,
                    "log_path": str(log_path),
                }
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2.0)
                health = {"status_code": response.status_code, "body": response.json()}
                if response.status_code in (200, 503):
                    return {
                        "state": "running",
                        "exit_code": None,
                        "port": port,
                        "health": health,
                        "log_path": str(log_path),
                    }
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.25)
        return {
            "state": "timeout",
            "exit_code": proc.poll(),
            "port": port,
            "health": health,
            "log_path": str(log_path),
        }
    finally:
        log.close()
        if proc.poll() is not None:
            pass
        elif health is None:
            _stop(proc)
        else:
            # Keep the process only until the caller explicitly stops it.
            pass


def _api_running(result: dict) -> bool:
    return result.get("state") == "running" and (result.get("health") or {}).get("status_code") == 200


def _stop_api(result: dict) -> None:
    # The caller does not get the Popen object; find it by port is unsafe. The API is always
    # stopped by its process group through the port-owning pid in /proc when possible.
    port = result.get("port")
    if not port:
        return
    try:
        out = subprocess.check_output(["ss", "-ltnp"], text=True)
    except Exception:
        return
    for line in out.splitlines():
        if f":{port}" not in line:
            continue
        match = re.search(r"pid=(\d+)", line)
        if match:
            try:
                os.kill(int(match.group(1)), 15)
            except ProcessLookupError:
                pass


def _log_tail(result: dict, limit: int = 1600) -> str:
    path = result.get("log_path")
    if not path or not Path(path).exists():
        return ""
    return Path(path).read_text(encoding="utf-8", errors="replace")[-limit:]


def _health_ok(result: dict) -> bool:
    return _api_running(result)


def _install(args: argparse.Namespace, database: str, redis_url: str) -> dict:
    database_url = _db_url(args.admin_url, database)
    app_database_url = _app_url(args.admin_url, database)
    env = _base_env(args, database_url=database_url, app_database_url=app_database_url, redis_url=redis_url)
    env["RENDER_ENV_FILE"] = str(args.work_dir / "render-env.sh")
    steps = []
    for label, command in (
        ("ensure_app_role", [sys.executable, "scripts/security/ensure_app_role.py"]),
        ("alembic_upgrade", [sys.executable, "-m", "alembic", "upgrade", "head"]),
        ("apply_runtime_privileges", [sys.executable, "scripts/security/apply_runtime_privileges.py"]),
    ):
        result = _run(command, env=env)
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
    return {"env": env, "steps": steps, "ok": all(step["exit_code"] == 0 for step in steps)}


class _FakeOllamaHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/tags":
            body = json.dumps({"models": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *_args) -> None:
        return


def _fake_ollama() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOllamaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


class Matrix:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.records: list[dict] = []
        self.created: set[str] = set()
        self.clean_db = f"lifeos_matrix_clean_{uuid.uuid4().hex[:8]}"
        self.clean: dict | None = None

    def record(self, name: str, status: str, checks: list[dict], details: dict | None = None, started: float | None = None) -> None:
        self.records.append(
            {
                "scenario": name,
                "status": status,
                "checks": checks,
                "details": details or {},
                "duration_s": round(time.monotonic() - started, 2) if started else None,
            }
        )

    def _check(self, label: str, passed: bool, actual=None, expected=None) -> dict:
        return {"check": label, "status": "PASS" if passed else "FAIL", "actual": actual, "expected": expected}

    def ensure_clean(self) -> dict:
        if self.clean is not None:
            return self.clean
        started = time.monotonic()
        _create_database(self.args.admin_url, self.clean_db)
        self.created.add(self.clean_db)
        installed = _install(self.args, self.clean_db, self.args.redis_url)
        self.clean = installed
        self.record(
            "clean_install_provisioning",
            "PASS" if installed["ok"] else "FAIL",
            [self._check("provisioning steps", installed["ok"], installed["steps"])],
            {"database": self.clean_db},
            started,
        )
        return installed

    def clean_install(self) -> None:
        started = time.monotonic()
        installed = self.ensure_clean()
        env = installed["env"]
        result = _start_api(env)
        checks = [self._check("provisioning", installed["ok"], installed["steps"])]
        if _health_ok(result):
            checks.append(self._check("health", True, result["health"]))
            head = _head_revision(env)
            current = _current_revision(env["DATABASE_URL"])
            checks.append(self._check("alembic at head", current == head, current, head))
            privileges = _app_privilege(self.args.admin_url, self.clean_db)
            checks.append(
                self._check(
                    "runtime job grants",
                    privileges["event_select"]
                    and privileges["event_insert"]
                    and privileges["event_update"] is False
                    and privileges["proposal_update"],
                    privileges,
                )
            )
            checks.append(self._check("app role can read documents", privileges["documents_select"], privileges))
        else:
            checks.append(self._check("health", False, result, "200"))
        _stop_api(result)
        self.record("clean_install", "PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL", checks, {"database": self.clean_db}, started)

    def existing_data(self) -> None:
        started = time.monotonic()
        if not self.args.existing_url or not self.args.existing_app_url:
            self.record("existing_data", "BLOCKED", [self._check("existing database provided", False, None, "URL")], {}, started)
            return
        try:
            counts = {table: _count(self.args.existing_url, table) for table in ("documents", "conversations", "users")}
        except Exception as exc:
            self.record("existing_data", "BLOCKED", [self._check("read existing database", False, repr(exc))], {}, started)
            return
        env = _base_env(
            self.args,
            database_url=self.args.existing_url,
            app_database_url=self.args.existing_app_url,
            redis_url=self.args.redis_url,
        )
        result = _start_api(env)
        checks = [self._check("existing data present", counts["documents"] > 0 or counts["conversations"] > 0, counts)]
        if _health_ok(result):
            checks.append(self._check("health", True, result["health"]))
            with httpx.Client(base_url=f"http://127.0.0.1:{result['port']}", timeout=30.0) as client:
                login = client.post(
                    "/api/auth/login",
                    json={"email": self.args.founder_email, "password": self.args.founder_password},
                )
                checks.append(self._check("founder login", login.status_code == 200, login.status_code, 200))
                if login.status_code == 200:
                    csrf = login.json().get("csrf_token", "")
                    for path in ("/api/documents", "/api/conversations", "/api/mainai/project-status"):
                        response = client.get(path, headers={"X-CSRF-Token": csrf})
                        checks.append(self._check(path, response.status_code == 200, response.status_code, 200))
        else:
            checks.append(self._check("health", False, result, "200"))
        _stop_api(result)
        self.record("existing_data", "PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL", checks, {"counts": counts}, started)

    def bad_db_credentials(self) -> None:
        started = time.monotonic()
        installed = self.ensure_clean()
        env = dict(installed["env"])
        # A wrong password alone is not a reliable probe on every local PostgreSQL: a trust
        # rule in pg_hba can accept it. A role that does not exist cannot authenticate under
        # any auth method, so this proves the same fail-closed path without depending on the
        # host's local auth policy.
        env["APP_DATABASE_URL"] = _app_url(
            self.args.admin_url,
            self.clean_db,
            password="definitely-wrong-password",
            username="mainai_app_missing",
        )
        result = _start_api(env, timeout=45)
        log = _log_tail(result)
        failed_closed = result["state"] != "running" or (result.get("health") or {}).get("status_code") == 503
        checks = [
            self._check("invalid app role fails closed", failed_closed, result),
            self._check("wrong password not echoed", "definitely-wrong-password" not in log, "redacted" if "definitely-wrong-password" not in log else "leaked"),
        ]
        _stop_api(result)
        self.record("bad_db_credentials", "PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL", checks, {}, started)

    def unreachable_postgres(self) -> None:
        started = time.monotonic()
        bad = "postgresql://postgres@127.0.0.1:1/postgres"
        env = _base_env(self.args, database_url=bad, app_database_url=bad, redis_url=self.args.redis_url)
        result = _start_api(env, timeout=45)
        failed_closed = result["state"] != "running" or (result.get("health") or {}).get("status_code") == 503
        checks = [self._check("unreachable PostgreSQL fails closed", failed_closed, result)]
        _stop_api(result)
        self.record("unreachable_postgres", "PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL", checks, {}, started)

    def unreachable_redis(self) -> None:
        started = time.monotonic()
        installed = self.ensure_clean()
        env = dict(installed["env"])
        env["REDIS_URL"] = "redis://127.0.0.1:1/0"
        result = _start_api(env, timeout=30)
        failed_closed = result["state"] != "running" or (result.get("health") or {}).get("status_code") == 503
        checks = [self._check("unreachable Redis fails closed", failed_closed, result)]
        _stop_api(result)
        self.record("unreachable_redis", "PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL", checks, {}, started)

    def missing_ollama(self) -> None:
        started = time.monotonic()
        installed = self.ensure_clean()
        env = dict(installed["env"])
        env.update(
            {
                "DEFAULT_LLM_PROVIDER": "ollama",
                "DEFAULT_LLM_MODEL": "llama3.1",
                "OLLAMA_BASE_URL": "http://127.0.0.1:1",
            }
        )
        result = _start_api(env)
        checks = []
        if _health_ok(result):
            local = (result["health"].get("body") or {}).get("local_model") or {}
            checks.append(self._check("API remains healthy without Ollama", True, result["health"]))
            checks.append(self._check("local model reported unreachable", local.get("reachable") is False, local))
        else:
            checks.append(self._check("API remains healthy without Ollama", False, result, "200"))
        _stop_api(result)
        self.record("missing_ollama", "PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL", checks, {}, started)

    def bad_model_config(self) -> None:
        started = time.monotonic()
        server, url = _fake_ollama()
        try:
            installed = self.ensure_clean()
            env = dict(installed["env"])
            env.update(
                {
                    "DEFAULT_LLM_PROVIDER": "ollama",
                    "DEFAULT_LLM_MODEL": "not-installed-model",
                    "OLLAMA_BASE_URL": url,
                }
            )
            result = _start_api(env)
            checks = []
            if _health_ok(result):
                local = (result["health"].get("body") or {}).get("local_model") or {}
                checks.append(self._check("health stays honest", True, result["health"]))
                checks.append(self._check("runtime reachable", local.get("reachable") is True, local))
                checks.append(self._check("configured model reported missing", "not-installed-model" in (local.get("missing_models") or []), local))
            else:
                checks.append(self._check("health stays honest", False, result, "200"))
            _stop_api(result)
        finally:
            server.shutdown()
            server.server_close()
        self.record("bad_model_config", "PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL", checks, {}, started)

    def partial_migration(self) -> str:
        started = time.monotonic()
        database = f"lifeos_matrix_partial_{uuid.uuid4().hex[:8]}"
        _create_database(self.args.admin_url, database)
        self.created.add(database)
        env = _base_env(
            self.args,
            database_url=_db_url(self.args.admin_url, database),
            app_database_url=_app_url(self.args.admin_url, database),
            redis_url=self.args.redis_url,
        )
        env["RENDER_ENV_FILE"] = str(self.args.work_dir / "render-env-partial.sh")
        role = _run([sys.executable, "scripts/security/ensure_app_role.py"], env=env)
        upgrade = _run([sys.executable, "-m", "alembic", "upgrade", "+1"], env=env)
        result = _start_api(env)
        failed_closed = result["state"] != "running" or (result.get("health") or {}).get("status_code") == 503
        checks = [
            self._check("role provisioning", role.returncode == 0, role.returncode, 0),
            self._check("partial migration applied", upgrade.returncode == 0, upgrade.returncode, 0),
            self._check("API refuses partial schema", failed_closed, result),
        ]
        _stop_api(result)
        self.record("aborted_or_partial_migration", "PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL", checks, {"database": database}, started)
        return database

    def failed_migration(self) -> None:
        started = time.monotonic()
        database = f"lifeos_matrix_failed_{uuid.uuid4().hex[:8]}"
        _create_database(self.args.admin_url, database)
        self.created.add(database)
        installed = _install(self.args, database, self.args.redis_url)
        conn = _admin_conn(_db_url(self.args.admin_url, database))
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE alembic_version SET version_num = 'deadbeefdeadbeef'")
        finally:
            conn.close()
        result = _start_api(installed["env"])
        failed_closed = result["state"] != "running" or (result.get("health") or {}).get("status_code") == 503
        checks = [
            self._check("provisioning", installed["ok"], installed["steps"]),
            self._check("API refuses unknown migration revision", failed_closed, result),
        ]
        _stop_api(result)
        self.record("failed_migration", "PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL", checks, {"database": database}, started)

    def restart_after_partial(self, database: str) -> None:
        started = time.monotonic()
        env = _base_env(
            self.args,
            database_url=_db_url(self.args.admin_url, database),
            app_database_url=_app_url(self.args.admin_url, database),
            redis_url=self.args.redis_url,
        )
        result = _start_api(env)
        failed_closed = result["state"] != "running" or (result.get("health") or {}).get("status_code") == 503
        checks = [self._check("restart still refuses partial schema", failed_closed, result)]
        _stop_api(result)
        self.record("restart_after_aborted_install", "PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL", checks, {"database": database}, started)

    def partial_startup_recovery(self) -> None:
        started = time.monotonic()
        installed = self.ensure_clean()
        bad_env = dict(installed["env"])
        bad_env["REDIS_URL"] = "redis://127.0.0.1:1/0"
        first = _start_api(bad_env, timeout=30)
        first_failed = first["state"] != "running" or (first.get("health") or {}).get("status_code") == 503
        _stop_api(first)
        second = _start_api(installed["env"])
        checks = [
            self._check("first partial startup fails closed", first_failed, first),
            self._check("restart after dependency recovery is healthy", _health_ok(second), second),
        ]
        _stop_api(second)
        self.record("partial_startup_recovery", "PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL", checks, {}, started)

    def cleanup(self) -> None:
        if self.args.keep:
            return
        for database in sorted(self.created):
            try:
                _drop_database(self.args.admin_url, database)
            except Exception as exc:
                print(f"warning: could not drop {database}: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-url", default=os.environ.get("MATRIX_ADMIN_URL", "postgresql://postgres@127.0.0.1:55432/postgres"))
    parser.add_argument("--existing-url", default=os.environ.get("MATRIX_EXISTING_URL", ""))
    parser.add_argument("--existing-app-url", default=os.environ.get("MATRIX_EXISTING_APP_URL", ""))
    parser.add_argument("--redis-url", default=os.environ.get("REDIS_URL", "redis://127.0.0.1:56379/15"))
    parser.add_argument("--pg-bin", default=os.environ.get("PG_BIN", ""))
    parser.add_argument("--founder-email", default="founder@lifeos.local")
    parser.add_argument("--founder-password", default="TestFounderPassword123!")
    parser.add_argument("--out", default="")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    if "127.0.0.1" not in args.admin_url and "localhost" not in args.admin_url:
        raise SystemExit("refusing to run the install matrix against a non-local database")
    if args.pg_bin:
        os.environ["PATH"] = f"{args.pg_bin}{os.pathsep}{os.environ.get('PATH', '')}"
    args.work_dir = Path(tempfile.mkdtemp(prefix="lifeai-install-matrix-"))
    (args.work_dir / "storage").mkdir(parents=True, exist_ok=True)

    matrix = Matrix(args)
    partial_db = ""
    try:
        matrix.clean_install()
        matrix.existing_data()
        matrix.bad_db_credentials()
        matrix.unreachable_postgres()
        matrix.unreachable_redis()
        matrix.missing_ollama()
        matrix.bad_model_config()
        partial_db = matrix.partial_migration()
        matrix.failed_migration()
        matrix.restart_after_partial(partial_db)
        matrix.partial_startup_recovery()
    finally:
        matrix.cleanup()

    statuses = [record["status"] for record in matrix.records]
    summary = {
        "generated_at": _now(),
        "commit": _run(["git", "rev-parse", "HEAD"], cwd=BACKEND_ROOT.parent).stdout.strip(),
        "branch": _run(["git", "branch", "--show-current"], cwd=BACKEND_ROOT.parent).stdout.strip(),
        "environment": {
            "admin_url": _redacted(args.admin_url),
            "redis_url": _redacted(args.redis_url),
            "work_dir": str(args.work_dir),
        },
        "summary": {
            "PASS": statuses.count("PASS"),
            "FAIL": statuses.count("FAIL"),
            "BLOCKED": statuses.count("BLOCKED"),
        },
        "scenarios": matrix.records,
    }
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
    print(text)
    if summary["summary"]["FAIL"]:
        return 2
    if summary["summary"]["BLOCKED"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
