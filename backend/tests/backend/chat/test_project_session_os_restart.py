"""Acceptance: the MainAI project journey survives real OS process restarts.

`test_project_session_real_model.py` restarts the backend by closing and reopening the ASGI
lifespan inside one pytest process, and restarts the worker by calling `Worker().run_once()`
twice in the same process. That proves the application-level recovery paths, but not that a
*killed operating-system process* leaves state a brand-new process can pick up.

This module closes that gap. It launches:

  * a real backend: `uvicorn` serving `app.main:app` in a child process, on a real TCP
    socket, against the real Postgres test database, the real Redis instance and the real
    local Ollama model;
  * a real worker: `python -m app.worker`, its production entry point, in a child process.

Both are killed with SIGKILL (and one with SIGTERM), exactly like a crashed container.
Nothing about the conversation, the project or the idempotency bookkeeping lives in the
pytest process: every post-restart assertion is read back from Postgres or from the new
child process.

The one deliberate stub is the embedding provider — no 1536-dimension embedding model is
installed here, so the backend launcher replaces `OpenAIProvider.embed` with deterministic
vectors, exactly as the in-process acceptance test does. The chat model is the real Ollama
`qwen2.5:0.5b`; the recorder in front of it forwards every request unchanged and records
the prompt only so that "the project context reached the model" can be asserted
deterministically. The recorder never fabricates or alters a model response.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import redis
from sqlalchemy import text

from app.models.conversation import Conversation, Message as MessageModel, MessageRole, MessageStatus
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.user import User

BACKEND_ROOT = Path(__file__).resolve().parents[3]
OLLAMA_URL = "http://127.0.0.1:11434"
CHAT_MODEL = "qwen2.5:0.5b"
CLOSED_OLLAMA = "http://127.0.0.1:1"  # nothing listens here; connect is refused immediately
FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
HEARTBEAT_KEY = "worker:heartbeat"

PROJECT_NAME = "MainAI OS-omstart"
TASK_TITLE = "Överlev en riktig processdöd"
FIRST_QUESTION = "Var är vi i projektet? Svara kort på svenska."
SECOND_QUESTION = "Fortsätt där vi slutade. Svara kort."
THIRD_QUESTION = "Fråga medan modellen är nere."


def _installed_models() -> set[str]:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as response:
            payload = json.loads(response.read())
    except Exception:
        return set()
    return {str(model.get("name", "")) for model in payload.get("models", [])}


pytestmark = pytest.mark.skipif(
    CHAT_MODEL not in _installed_models(),
    reason=f"real local model {CHAT_MODEL} not installed at {OLLAMA_URL}",
)


# The launcher is a real uvicorn process. Only the embedding call is stubbed, because this
# environment has no 1536-dimension embedding model — the same substitution the in-process
# real-model acceptance makes. Everything else, including app startup, RLS, bootstrap and
# the chat provider, is the production code path.
_LAUNCHER = '''\
import os

import uvicorn

from app.config import get_settings
from app.providers.openai_provider import OpenAIProvider


async def _embed(self, texts, model, *, timeout=None):
    return [[0.1] * get_settings().embedding_dim for _ in texts]


OpenAIProvider.embed = _embed

from app.main import app  # noqa: E402 - imported after the single stubbed method

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ["OS_RESTART_PORT"]),
        log_level="warning",
    )
'''


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _child_env(
    base_url: str,
    *,
    port: int,
    worker_id: str | None = None,
    openai_key: str | None = None,
) -> dict[str, str]:
    env = dict(os.environ)
    pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(BACKEND_ROOT) + (os.pathsep + pythonpath if pythonpath else "")
    env.update(
        {
            "ENVIRONMENT": "test",
            # httpx's cookie jar (like a browser's) refuses to send a Secure cookie over
            # plain http. Browsers treat http://127.0.0.1 as a secure context; httpx does
            # not, so the test backend relaxes only this attribute.
            "COOKIE_SECURE": "false",
            "ENABLE_SCHEDULED_CLEANUP": "false",
            "OS_RESTART_PORT": str(port),
            "DEFAULT_LLM_PROVIDER": "ollama",
            "DEFAULT_LLM_MODEL": CHAT_MODEL,
            "OLLAMA_BASE_URL": base_url,
            "CHAT_FALLBACK_ORDER": "",  # no paid fallback may ever be reached here
            "WORKER_POLL_INTERVAL_SECONDS": "1",
            "WORKER_LEASE_SECONDS": "5",
        }
    )
    if worker_id is not None:
        env["WORKER_ID"] = worker_id
    if openai_key is not None:
        env["OPENAI_API_KEY"] = openai_key
    return env


class _ModelRecorder:
    """A pass-through recorder in front of the real Ollama server.

    Every request is forwarded unchanged and the response returned as-is; only the request
    bodies of `/api/chat` are kept, so the test can prove the project context reached the
    real model without faking the model itself.
    """

    def __init__(self) -> None:
        self.chat_requests: list[dict] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "_ModelRecorder":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _make_handler(self):
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep pytest output clean
                return

            def _read_body(self) -> bytes:
                length = int(self.headers.get("content-length") or 0)
                return self.rfile.read(length) if length else b""

            def _respond(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _forward(self, method: str, body: bytes | None) -> None:
                request = urllib.request.Request(
                    OLLAMA_URL + self.path,
                    data=body,
                    method=method,
                    headers={"content-type": "application/json"},
                )
                try:
                    with urllib.request.urlopen(request, timeout=180) as response:
                        data, status = response.read(), response.status
                        content_type = response.headers.get("content-type", "application/json")
                except urllib.error.HTTPError as exc:
                    data, status = exc.read(), exc.code
                    content_type = exc.headers.get("content-type", "application/json")
                self._respond(status, data, content_type)

            def do_GET(self) -> None:
                self._forward("GET", None)

            def do_POST(self) -> None:
                body = self._read_body()
                if self.path.split("?")[0].rstrip("/").endswith("/api/chat"):
                    with recorder._lock:
                        recorder.chat_requests.append(json.loads(body))
                self._forward("POST", body)

        return Handler


class _Child:
    """One real OS process, started from the same interpreter as pytest."""

    def __init__(self, *, name: str, args: list[str], env: dict[str, str], log_path: Path) -> None:
        self.name = name
        self.args = args
        self.env = env
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None
        self._log = None

    def start(self) -> None:
        self._log = open(self.log_path, "ab", buffering=0)  # noqa: SIM115 - closed in stop/kill
        self.proc = subprocess.Popen(
            self.args,
            cwd=str(BACKEND_ROOT),
            env=self.env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def output(self) -> str:
        try:
            return self.log_path.read_text(errors="replace")[-4000:]
        except FileNotFoundError:
            return ""

    def kill(self) -> int:
        assert self.proc is not None, f"{self.name} was never started"
        self.proc.kill()
        code = self.proc.wait(timeout=30)
        self._close_log()
        return code

    def terminate(self) -> int:
        assert self.proc is not None, f"{self.name} was never started"
        self.proc.terminate()
        try:
            code = self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            code = self.proc.wait(timeout=30)
        self._close_log()
        return code

    def _close_log(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None

    def stop_if_alive(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=30)
        self._close_log()


def _wait_ready(child: _Child, port: int, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    last = "no attempt"
    while time.monotonic() < deadline:
        if child.proc is not None and child.proc.poll() is not None:
            raise AssertionError(
                f"backend exited with {child.proc.returncode} before becoming ready:\n{child.output()}"
            )
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/api/health/ready", timeout=3.0)
            if response.status_code == 200:
                return
            last = f"{response.status_code} {response.text[:200]}"
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        time.sleep(0.2)
    raise AssertionError(f"backend not ready within {timeout}s (last={last}):\n{child.output()}")


def _wait_heartbeat(client: redis.Redis, worker_id: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = client.get(HEARTBEAT_KEY)
        if value and str(value).startswith(f"{worker_id}:"):
            return
        time.sleep(0.1)
    raise AssertionError(f"worker {worker_id} never recorded a heartbeat")


def _login(client: httpx.Client, email: str = FOUNDER_EMAIL, password: str = FOUNDER_PASSWORD) -> str:
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def _prompt_text(request: dict) -> str:
    return "\n".join(str(message.get("content", "")) for message in request.get("messages", []))


def _messages(db, conversation_id: str) -> list[MessageModel]:
    db.rollback()
    return (
        db.query(MessageModel)
        .filter_by(conversation_id=uuid.UUID(conversation_id))
        .order_by(MessageModel.created_at.asc(), MessageModel.id.asc())
        .all()
    )


def test_project_journey_survives_real_os_process_restarts(tmp_path, superuser_db, make_verified_user):
    launcher = tmp_path / "os_restart_backend.py"
    launcher.write_text(_LAUNCHER)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    children: list[_Child] = []

    def start_backend(name: str, model_url: str) -> _Child:
        # apply_rls() at startup takes table locks; an open test transaction here would
        # block the new process exactly as a stray long-running transaction would in prod.
        superuser_db.rollback()
        child = _Child(
            name=name,
            args=[sys.executable, str(launcher)],
            env=_child_env(model_url, port=port),
            log_path=tmp_path / f"{name}.log",
        )
        children.append(child)
        child.start()
        _wait_ready(child, port)
        return child

    def start_worker(name: str, worker_id: str) -> _Child:
        superuser_db.rollback()
        child = _Child(
            name=name,
            args=[sys.executable, "-m", "app.worker"],
            env=_child_env(
                recorder.url,
                port=port,
                worker_id=worker_id,
                # No embedding provider configured in the worker: the worker-restart
                # assertion is about the process and its claims, not a network probe.
                openai_key="",
            ),
            log_path=tmp_path / f"{name}.log",
        )
        children.append(child)
        child.start()
        return child

    with _ModelRecorder() as recorder:
        clients: list[httpx.Client] = []
        try:
            # ---------------------------------------------------------------- process A
            backend_a = start_backend("backend-a", recorder.url)
            client = httpx.Client(base_url=base_url, timeout=180.0)
            clients.append(client)
            headers = {"X-CSRF-Token": _login(client)}

            project = client.post(
                "/api/projects",
                json={"name": PROJECT_NAME, "description": "Ska överleva en riktig processdöd", "status": "active"},
                headers=headers,
            )
            assert project.status_code == 200, project.text
            project_id = project.json()["id"]

            task = client.post(
                "/api/projects/tasks",
                json={
                    "title": TASK_TITLE,
                    "description": "Kontexten ska nå en riktig lokal modell.",
                    "project_id": project_id,
                    "status": "todo",
                },
                headers=headers,
            )
            assert task.status_code == 201, task.text

            first = client.post(
                "/api/chat",
                json={
                    "message": FIRST_QUESTION,
                    "project_id": project_id,
                    "client_message_id": "os-restart-1",
                },
                headers=headers,
            )
            assert first.status_code == 200, first.text
            first_body = first.json()
            assert first_body["assistant_status"] == "succeeded", first_body
            assert first_body["provider"] == "ollama", first_body
            assert first_body["model"] == CHAT_MODEL, first_body
            assert first_body["reply"].strip(), first_body
            conversation_id = first_body["conversation_id"]
            first_assistant_id = first_body["assistant_message_id"]

            # Deterministic: the real model's prompt carried the project and its open task.
            first_prompt = _prompt_text(recorder.chat_requests[0])
            assert PROJECT_NAME in first_prompt
            assert TASK_TITLE in first_prompt

            stored = _messages(superuser_db, conversation_id)
            assert [row.role for row in stored] == [MessageRole.user, MessageRole.assistant]
            assert stored[0].content == FIRST_QUESTION
            assert stored[1].content == first_body["reply"]

            # A real crash: SIGKILL, no shutdown hook, no chance to flush anything.
            assert backend_a.kill() == -9

            # ---------------------------------------------------------------- process B
            backend_b = start_backend("backend-b", recorder.url)

            # The pre-crash browser session still works: the refresh token is DB-backed and
            # signed with the same key, so the cookie jar survives a process replacement.
            resumed = client.get(f"/api/conversations/{conversation_id}")
            assert resumed.status_code == 200, resumed.text
            history = resumed.json()["messages"]
            assert [item["role"] for item in history] == ["user", "assistant"]
            assert history[0]["content"] == FIRST_QUESTION
            assert history[1]["content"] == first_body["reply"]

            headers_b = {"X-CSRF-Token": _login(client)}

            # Idempotency across a real process boundary: the same logical message replays
            # the stored answer and never reaches the model a second time.
            replay = client.post(
                "/api/chat",
                json={
                    "message": FIRST_QUESTION,
                    "project_id": project_id,
                    "client_message_id": "os-restart-1",
                },
                headers=headers_b,
            )
            assert replay.status_code == 200, replay.text
            assert replay.json()["assistant_message_id"] == first_assistant_id, replay.json()
            assert len(recorder.chat_requests) == 1

            second = client.post(
                "/api/chat",
                json={
                    "message": SECOND_QUESTION,
                    "conversation_id": conversation_id,
                    "client_message_id": "os-restart-2",
                },
                headers=headers_b,
            )
            assert second.status_code == 200, second.text
            second_body = second.json()
            assert second_body["assistant_status"] == "succeeded", second_body
            assert second_body["conversation_id"] == conversation_id
            assert second_body["reply"].strip(), second_body

            # Deterministic: the resumed turn sent the pre-crash exchange to the real model.
            resumed_prompt = _prompt_text(recorder.chat_requests[1])
            assert FIRST_QUESTION in resumed_prompt
            assert first_body["reply"] in resumed_prompt

            # Graceful shutdown is a different path than SIGKILL and must also be clean.
            assert backend_b.terminate() in (0, -15)

            # ---------------------------------------------------------------- process C
            # The model is unreachable in this process: a real connection refusal, not a
            # stubbed exception, and no paid fallback is configured.
            start_backend("backend-c", CLOSED_OLLAMA)
            headers_c = {"X-CSRF-Token": _login(client)}
            down = client.post(
                "/api/chat",
                json={
                    "message": THIRD_QUESTION,
                    "conversation_id": conversation_id,
                    "client_message_id": "os-restart-3",
                },
                headers=headers_c,
            )
            assert down.status_code == 200, down.text
            down_body = down.json()
            assert down_body["assistant_status"] == "failed", down_body
            assert down_body["error_category"] == "unreachable", down_body
            assert down_body["retryable"] is True, down_body
            assert down_body["provider"] is None, down_body
            failed_user_message_id = down_body["user_message_id"]
            failed_assistant_id = down_body["assistant_message_id"]

            # The founder's message is durable even though the answer is not, and the failed
            # row never stores partial or invented text.
            stored = _messages(superuser_db, conversation_id)
            assert [row.role for row in stored] == [
                MessageRole.user,
                MessageRole.assistant,
                MessageRole.user,
                MessageRole.assistant,
                MessageRole.user,
                MessageRole.assistant,
            ]
            assert stored[4].content == THIRD_QUESTION
            assert stored[5].status == MessageStatus.failed
            assert stored[5].content == ""
            assert len(recorder.chat_requests) == 2  # the failed call never reached the model

            assert children[-1].kill() == -9

            # ---------------------------------------------------------------- process D
            start_backend("backend-d", recorder.url)
            headers_d = {"X-CSRF-Token": _login(client)}
            retried = client.post(
                f"/api/chat/messages/{failed_user_message_id}/retry",
                headers=headers_d,
            )
            assert retried.status_code == 200, retried.text
            retried_body = retried.json()
            assert retried_body["assistant_status"] == "succeeded", retried_body
            assert retried_body["assistant_message_id"] == failed_assistant_id, retried_body
            assert retried_body["reply"].strip(), retried_body
            assert len(recorder.chat_requests) == 3

            stored = _messages(superuser_db, conversation_id)
            assert len(stored) == 6
            assert stored[5].status == MessageStatus.succeeded
            assert stored[5].content == retried_body["reply"]

            # Another founder is denied by the same founder gate, in a process that has
            # only ever seen the post-restart database.
            other, other_password = make_verified_user(
                email="os-restart-other@example.com", role="founder"
            )
            other_client = httpx.Client(base_url=base_url, timeout=60.0)
            clients.append(other_client)
            other_headers = {"X-CSRF-Token": _login(other_client, other.email, other_password)}
            assert other_client.get(f"/api/conversations/{conversation_id}").status_code == 403
            assert other_client.get(f"/api/projects/{project_id}").status_code == 403
            denied = other_client.post(
                "/api/chat",
                json={"message": "Fortsätt", "conversation_id": conversation_id},
                headers=other_headers,
            )
            assert denied.status_code == 403

            # ------------------------------------------------- worker A: alive, then killed
            redis_client = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
            worker_a = start_worker("worker-a", "os-restart-worker-a")
            _wait_heartbeat(redis_client, "os-restart-worker-a")
            assert worker_a.kill() == -9

            # The row a worker killed mid-claim leaves behind: running, leased to the dead
            # process, lease already expired.
            superuser_db.rollback()
            founder = superuser_db.query(User).filter_by(email=FOUNDER_EMAIL).one()
            abandoned = ImportJob(
                owner_id=founder.id,
                status=ImportJobStatus.running,
                source_filename="crashed-worker.zip",
                source_checksum="0" * 64,
                progress_total=1,
                locked_by="os-restart-worker-a",
                lease_expires_at=datetime.utcnow() - timedelta(seconds=5),  # noqa: DTZ003 - naive UTC column
            )
            superuser_db.add(abandoned)
            superuser_db.commit()
            abandoned_id = str(abandoned.id)
            superuser_db.rollback()

            # ------------------------------------------------ worker B: reclaims the claim
            worker_b = start_worker("worker-b", "os-restart-worker-b")
            _wait_heartbeat(redis_client, "os-restart-worker-b")

            observed = None
            deadline = time.monotonic() + 60.0
            while time.monotonic() < deadline:
                row = superuser_db.execute(
                    text(
                        "SELECT locked_by, lease_expires_at FROM knowledge_import_jobs "
                        "WHERE id = :job_id"
                    ),
                    {"job_id": abandoned_id},
                ).first()
                superuser_db.rollback()
                if row is not None and row[0] == "os-restart-worker-b":
                    observed = row
                    break
                time.sleep(0.2)
            assert observed is not None, (
                "the restarted worker never reclaimed the crashed worker's expired lease:\n"
                f"{worker_b.output()}"
            )
            assert observed[1] is not None
            assert observed[1] > datetime.utcnow()  # noqa: DTZ003 - naive UTC column

            # The worker restart changed nothing about the conversation. Scoped to this
            # conversation on purpose: other tests in the same session may own their own.
            superuser_db.rollback()
            assert len(_messages(superuser_db, conversation_id)) == 6
            assert (
                superuser_db.query(Conversation)
                .filter_by(id=uuid.UUID(conversation_id))
                .count()
                == 1
            )

            # Not asserted as SIGKILL: the restarted worker may already have finished the
            # abandoned job and moved on; only the takeover above is the claim under test.
            worker_b.stop_if_alive()
        finally:
            for child in children:
                child.stop_if_alive()
            for client in clients:
                client.close()
