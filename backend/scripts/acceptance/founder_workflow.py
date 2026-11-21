#!/usr/bin/env python3
"""The founder-facing workflow, exercised end to end against a real running system.

This is not a unit test and not a set of API curl calls. It starts the real backend and the
real durable worker, talks to them **through the frontend's same-origin proxy** (the only path a
browser uses), and performs the twelve steps a founder actually performs:

   1. start MainAI                       (this harness supervises the processes)
   2. authenticate                       (real login, cookie + CSRF, through the proxy)
   3. see whether the local model works  (/api/health, not a guess)
   4. submit a question                  (real chat endpoint)
   5. receive a real model-generated answer
   6. upload a document                  (real multipart import -> durable worker)
   7. retrieve from it with citations    (real embedding + pgvector + citation text)
   8. save an authorized decision        (workbench, label=decision)
   9. restart the application            (processes are stopped and started again)
  10. recover the decision and rationale (fresh session after restart)
  11. submit a permitted task            (agent task + MainAI job)
  12. see its progress/failure/completion (polled, reported honestly)

The only thing faked is nothing: the chat and embedding providers are the local Ollama models
this environment has installed. If a step cannot be verified, the run exits non-zero and names
the step; it never prints a green summary for work it did not do.

Usage (supervised, recommended):
    python backend/scripts/acceptance/founder_workflow.py \
        --base-url http://127.0.0.1:3020 --supervise --backend-port 8010 \
        --out founder-workflow-report.json

The frontend must already be running at --base-url and proxying /api to --backend-port. When
--supervise is omitted, the script only exercises an already-running system and reports the
restart step as not performed.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent
FOUNDER_EMAIL = os.environ.get("FOUNDER_EMAIL", "founder@lifeos.local")
FOUNDER_PASSWORD = os.environ.get("FOUNDER_PASSWORD", "TestFounderPassword123!")
# MainAIJobStatus's own vocabulary — `completed` is the success terminal state here; the
# harness's first version omitted it, kept polling a successfully finished job and then
# reported "never reached terminal" while the stored job said completed.
TERMINAL_JOB_STATUSES = {"succeeded", "completed", "failed", "cancelled"}


class StepFailed(RuntimeError):
    def __init__(self, step: str, message: str):
        super().__init__(f"{step}: {message}")
        self.step = step


def _wait_port(port: int, *, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.5)
    raise StepFailed("startup", f"nothing listening on 127.0.0.1:{port} after {timeout:.0f}s")


def _wait_http(client: httpx.Client, path: str, *, timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            response = client.get(path)
            if response.status_code == 200:
                return response.json()
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(1.0)
    raise StepFailed("readiness", f"{path} never became ready ({last_error})")


class Supervisor:
    """Owns the backend and worker processes so a restart is real, not simulated."""

    def __init__(self, port: int):
        self.port = port
        self.processes: list[subprocess.Popen] = []
        self.logs: list[Path] = []
        self.env = self._env()

    def _env(self) -> dict:
        env = os.environ.copy()
        env.setdefault("ENVIRONMENT", "development")
        env.setdefault("DATABASE_URL", "postgresql://lifeos@localhost:5433/lifeos_founder_flow")
        env.setdefault("APP_DATABASE_URL", "postgresql://mainai_app:mainai_app_pw@localhost:5433/lifeos_founder_flow")
        env.setdefault("REDIS_URL", "redis://localhost:6379/10")
        env.setdefault("SECRET_KEY", "founder-workflow-secret-key-not-a-placeholder")
        env.setdefault("FOUNDER_EMAIL", FOUNDER_EMAIL)
        env.setdefault("FOUNDER_PASSWORD", FOUNDER_PASSWORD)
        env.setdefault("PUBLIC_APP_URL", "http://127.0.0.1:3020")
        env.setdefault("FRONTEND_ORIGINS", "http://127.0.0.1:3020")
        env.setdefault("ENABLE_SCHEDULED_CLEANUP", "false")
        # The product default is Secure + SameSite=None, which Chromium accepts on localhost
        # but a plain HTTP client correctly refuses to send over http. This harness is a
        # loopback development client, so it opts into the loopback-safe cookie mode; the
        # production default is untouched.
        env.setdefault("COOKIE_SECURE", "false")
        env.setdefault("COOKIE_SAMESITE", "lax")
        env.setdefault("DEFAULT_LLM_PROVIDER", "ollama")
        env.setdefault("DEFAULT_LLM_MODEL", os.environ.get("MAINAI_REAL_CHAT_MODEL", "qwen2.5:0.5b-instruct"))
        env.setdefault("DEFAULT_EMBEDDING_PROVIDER", "ollama")
        env.setdefault("DEFAULT_EMBEDDING_MODEL", os.environ.get("MAINAI_REAL_EMBED_MODEL", "nomic-embed-text"))
        env.setdefault("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
        env.setdefault("STORAGE_ROOT", str(Path(os.environ.get("TMPDIR", "/tmp")) / "mainai-founder-flow-storage"))
        return env

    def start(self) -> None:
        self.logs = []
        commands = [
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "warning",
            ],
            [sys.executable, "-m", "app.worker"],
        ]
        for command in commands:
            log_path = Path(os.environ.get("TMPDIR", "/tmp")) / f"mainai-flow-{command[-1] if command[-1] != 'app.main:app' else 'backend'}.log"
            log = open(log_path, "w")
            process = subprocess.Popen(
                command,
                cwd=str(BACKEND_ROOT),
                env=self.env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self.processes.append(process)
            self.logs.append(log_path)
        _wait_port(self.port)

    def stop(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 20
        for process in self.processes:
            try:
                process.wait(timeout=max(1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.processes = []

    def tail_logs(self) -> str:
        chunks = []
        for path in self.logs:
            if path.exists():
                chunks.append(f"--- {path.name} ---\n" + "\n".join(path.read_text(errors="replace").splitlines()[-25:]))
        return "\n".join(chunks)


class FounderWorkflow:
    def __init__(self, base_url: str, *, supervise: bool, backend_port: int):
        self.base_url = base_url.rstrip("/")
        self.supervisor = Supervisor(backend_port) if supervise else None
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=120.0,
            follow_redirects=True,
            headers={"Origin": self.base_url, "Referer": f"{self.base_url}/"},
        )
        self.csrf: str | None = None
        self.state: dict = {}
        self.steps: list[dict] = []

    def record(self, step: str, status: str, detail: dict) -> None:
        self.steps.append({"step": step, "status": status, "detail": detail})
        print(f"[{status:7}] {step}: {json.dumps(detail, ensure_ascii=False)[:220]}")

    def login(self) -> dict:
        response = self.client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
        if response.status_code != 200:
            raise StepFailed("authenticate", f"HTTP {response.status_code}: {response.text[:300]}")
        body = response.json()
        self.csrf = body["csrf_token"]
        self.client.headers["X-CSRF-Token"] = self.csrf
        return body

    def health(self) -> dict:
        body = _wait_http(self.client, "/api/health")
        model = body.get("local_model") or {}
        # /api/health answers 503 when the database or Redis is down (and only then), so a 200
        # here already means both dependencies answered; the body carries the model/worker
        # detail. Checking for `database`/`redis` keys was wrong: they are not in the payload.
        if body.get("status") != "ok":
            raise StepFailed("readiness", f"health did not report ok: {body}")
        if not model.get("reachable"):
            raise StepFailed("local_model", f"local model not reachable: {model}")
        return body

    def chat(self, message: str, *, conversation_id: str | None = None) -> dict:
        payload = {"message": message}
        if conversation_id:
            payload["conversation_id"] = conversation_id
        response = self.client.post("/api/chat", json=payload)
        if response.status_code != 200:
            raise StepFailed("chat", f"HTTP {response.status_code}: {response.text[:300]}")
        body = response.json()
        if body.get("assistant_status") != "succeeded" or not body.get("reply"):
            raise StepFailed("chat", f"assistant did not answer: {body}")
        return body

    def upload(self, title: str, text: str) -> dict:
        files = {"file": (f"{title}.md", text.encode(), "text/markdown")}
        response = self.client.post("/api/library/import", files=files)
        if response.status_code not in (200, 201):
            raise StepFailed("upload", f"HTTP {response.status_code}: {response.text[:300]}")
        job = response.json()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            listing = self.client.get("/api/library")
            listing.raise_for_status()
            for item in listing.json():
                # The importer derives the stored title from the uploaded filename, so the
                # source of truth is the original filename, not the bare label we generated.
                if item.get("title") == title or (item.get("title") or "").startswith(title):
                    if item.get("status") == "indexed":
                        return item
                    if item.get("status") in {"failed", "extraction_failed", "indexing_failed"}:
                        raise StepFailed("upload", f"{title} failed to index: {item.get('error_message')}")
            time.sleep(1.0)
        raise StepFailed("upload", f"{title} never reached 'indexed' (import job {job.get('id')})")

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        response = self.client.post("/api/knowledge/search", json={"query": query, "top_k": top_k})
        if response.status_code != 200:
            raise StepFailed("retrieval", f"HTTP {response.status_code}: {response.text[:300]}")
        return response.json()

    def run(self, *, restart: bool, permitted_task: bool) -> dict:
        started = datetime.now(timezone.utc)
        if self.supervisor:
            self.supervisor.start()
        try:
            _wait_http(self.client, "/api/health")
            self.record("1.start", "passed", {"base_url": self.base_url, "supervised": bool(self.supervisor)})

            session = self.login()
            self.record("2.authenticate", "passed", {"email": session.get("email"), "csrf_issued": bool(self.csrf)})

            health = self.health()
            model = health.get("local_model") or {}
            self.record(
                "3.local_model",
                "passed",
                {"reachable": model.get("reachable"), "missing_models": model.get("missing_models")},
            )

            token = uuid.uuid4().hex[:12]
            # Upload first so the chat below is grounded in a real source, not an empty library.
            document = self.upload(
                f"Founder workflow {token}",
                f"Driftdokument {token}. Återställningstiden för produktionsmiljön är 45 minuter. "
                "Ansvarig för återställningen är grundaren. Dokumentet beskriver också hur en "
                "kontroll av backup görs innan en återställning påbörjas.",
            )
            self.state["document_id"] = document["id"]
            self.record("6.upload", "passed", {"document_id": document["id"], "chunks": document.get("chunk_count")})

            hits = self.search(f"Vad är återställningstiden för produktionsmiljön? {token}")
            citation = next((h for h in hits if h.get("document_id") == document["id"]), None)
            if citation is None:
                raise StepFailed("retrieval", f"uploaded document was not retrieved; hits={[h.get('title') for h in hits]}")
            if "45 minuter" not in (citation.get("text") or ""):
                raise StepFailed("retrieval", "the retrieved citation does not contain the document's fact")
            self.record(
                "7.cited_retrieval",
                "passed",
                {"title": citation.get("title"), "score": citation.get("score"), "phrase": "45 minuter"},
            )

            answer = self.chat(f"Vad är återställningstiden för produktionsmiljön? Svara kort. ({token})")
            answer_text = answer.get("reply") or ""
            if "45" not in answer_text:
                raise StepFailed("grounded_answer", f"the model answer does not use the source fact: {answer_text[:200]}")
            self.state["conversation_id"] = answer["conversation_id"]
            self.record(
                "4-5.question_and_answer",
                "passed",
                {
                    "provider": answer.get("provider"),
                    "model": answer.get("model"),
                    "confidence": answer.get("confidence"),
                    "sources": [s.get("title") for s in answer.get("sources", [])],
                    "answer_excerpt": answer_text[:160],
                },
            )

            analysis = self.client.post(
                "/api/workbench/analyze",
                json={"question": "Ska lansering 2 ske i två steg?", "document_id": document["id"]},
            )
            if analysis.status_code != 200:
                raise StepFailed("decision", f"workbench analyze HTTP {analysis.status_code}: {analysis.text[:300]}")
            analysis_body = analysis.json()
            saved = self.client.post(
                "/api/workbench/save",
                json={
                    "question": "Ska lansering 2 ske i två steg?",
                    "conclusion": "Ja — privat beta först, därefter en begränsad release.",
                    "critique": analysis_body.get("critique"),
                    "label": "decision",
                    "source_document_ids": [document["id"]],
                },
            )
            if saved.status_code not in (200, 201):
                raise StepFailed("decision", f"workbench save HTTP {saved.status_code}: {saved.text[:300]}")
            decision = saved.json()
            self.state["decision_id"] = decision["id"]
            self.record(
                "8.save_decision",
                "passed",
                {
                    "decision_id": decision["id"],
                    "classification": decision.get("classification"),
                    "status": decision.get("active_truth_status"),
                },
            )

            if restart:
                self.supervisor.stop()
                closed = False
                for _ in range(40):
                    with socket.socket() as sock:
                        sock.settimeout(0.2)
                        if sock.connect_ex(("127.0.0.1", self.supervisor.port)) != 0:
                            closed = True
                            break
                    time.sleep(0.25)
                if not closed:
                    raise StepFailed("restart", "the backend port never closed; the restart was not real")
                self.supervisor.start()
                _wait_http(self.client, "/api/health")
                self.record("9.restart", "passed", {"backend_restarted": True})

                # Fresh session: do not rely on the old access token.
                self.login()
                conversation = self.client.get(f"/api/conversations/{self.state['conversation_id']}")
                if conversation.status_code != 200:
                    raise StepFailed("recover", f"conversation HTTP {conversation.status_code}")
                messages = conversation.json().get("messages", [])
                recovered = any(m.get("role") == "assistant" and "45" in (m.get("content") or "") for m in messages)

                library = {item["id"]: item for item in self.client.get("/api/library").json()}
                decision_row = library.get(self.state["decision_id"])
                if decision_row is None:
                    raise StepFailed("recover", "the saved decision is missing after restart")
                decision_hits = self.search("Ska lansering 2 ske i två steg?", top_k=5)
                decision_cited = any(h.get("document_id") == self.state["decision_id"] for h in decision_hits)
                if not recovered or not decision_cited:
                    raise StepFailed(
                        "recover",
                        f"post-restart recovery incomplete (conversation={recovered}, decision_cited={decision_cited})",
                    )
                self.record(
                    "10.recover_decision",
                    "passed",
                    {
                        "conversation_recovered": recovered,
                        "decision_present": True,
                        "decision_retrievable": decision_cited,
                        "decision_status": decision_row.get("active_truth_status"),
                    },
                )
            else:
                self.record("9-10.restart_recovery", "skipped", {"reason": "not running under --supervise"})

            if permitted_task:
                permission_test = self.client.post("/api/knowledge/search", json={"query": "förbjuden kapacitet", "top_k": 1})
                if permission_test.status_code != 200:
                    raise StepFailed("task", "knowledge endpoint unavailable before task submission")

                task = self.client.post(
                    "/api/admin/agents/tasks",
                    json={
                        "title": "Verifiera återställningsrutinen",
                        "description": "Läs driftdokumentet och kontrollera att återställningstiden är dokumenterad.",
                        "acceptance_criteria": "Återställningstiden är dokumenterad och ansvarig är namngiven.",
                        "target_files": [],
                    },
                )
                if task.status_code not in (200, 201):
                    raise StepFailed("task", f"agent task HTTP {task.status_code}: {task.text[:300]}")
                task_body = task.json()
                self.state["task_id"] = task_body["id"]

                job_response = self.client.post(
                    "/api/mainai/jobs",
                    json={"job_type": "corpus_review", "input_refs": [{"type": "document", "id": document["id"]}]},
                )
                if job_response.status_code not in (200, 201):
                    raise StepFailed("job", f"job submission HTTP {job_response.status_code}: {job_response.text[:300]}")
                job = job_response.json()
                job_id = job["id"]
                deadline = time.monotonic() + 180
                detail = job
                while time.monotonic() < deadline:
                    detail = self.client.get(f"/api/mainai/jobs/{job_id}").json()
                    if detail.get("status") in TERMINAL_JOB_STATUSES:
                        break
                    time.sleep(1.5)
                self.record(
                    "11-12.task_and_progress",
                    "passed",
                    {
                        "task_id": task_body["id"],
                        "job_id": job_id,
                        "job_status": detail.get("status"),
                        "progress": f"{detail.get('progress_current')}/{detail.get('progress_total')}",
                        "error_category": detail.get("error_category"),
                        "events": len(detail.get("events", [])),
                    },
                )
                if detail.get("status") not in TERMINAL_JOB_STATUSES:
                    raise StepFailed("job", "the job did not reach a terminal state within 180s")
            else:
                self.record("11-12.task_and_progress", "skipped", {"reason": "--no-task"})

            return {
                "tool": "mainai-founder-workflow",
                "version": 1,
                "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "base_url": self.base_url,
                "seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
                "steps": self.steps,
                "state": self.state,
                "passed": all(s["status"] in {"passed", "skipped"} for s in self.steps),
            }
        finally:
            if self.supervisor:
                self.supervisor.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=os.environ.get("MAINAI_WORKFLOW_BASE_URL", "http://127.0.0.1:3020"))
    parser.add_argument("--supervise", action="store_true", help="start and restart the backend + worker")
    parser.add_argument("--backend-port", type=int, default=int(os.environ.get("E2E_BACKEND_PORT", "8010")))
    parser.add_argument("--no-task", action="store_true", help="skip the permitted-task step")
    parser.add_argument("--out", default="founder-workflow-report.json")
    args = parser.parse_args(argv)

    workflow = FounderWorkflow(args.base_url, supervise=args.supervise, backend_port=args.backend_port)
    try:
        report = workflow.run(restart=args.supervise, permitted_task=not args.no_task)
    except StepFailed as exc:
        report = {
            "tool": "mainai-founder-workflow",
            "version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "base_url": args.base_url,
            "steps": workflow.steps,
            "state": workflow.state,
            "passed": False,
            "failed_step": exc.step,
            "error": str(exc),
        }
        if workflow.supervisor:
            report["server_logs_tail"] = workflow.supervisor.tail_logs()
        print(f"\nFAILED [{exc.step}]: {exc}", file=sys.stderr)
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return 1

    Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nfounder workflow passed in {report['seconds']}s — report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
