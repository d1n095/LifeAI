#!/usr/bin/env python3
"""V6 Founder Alpha acceptance journey against a real running test lab.

The driver talks to the real FastAPI API and the real durable worker. It never mutates
production and refuses a non-loopback base URL.

Steps:
  1 authenticate as founder
  2 protected routes reject unauthenticated users
  3 start a local-AI conversation (SSE)
  4 observe streaming deltas reach the client
  5 persist and reopen conversation history
  6 upload and ingest a document
  7 query the ingested knowledge through the pgvector-backed chat path
  8 create a supervised MainAI goal/plan task
  9 verify explicit execution-scope authorization and task approval boundaries
 10 execute the approved read-only task
 11 cancel an active durable operation
 12 verify conversation, job/task evidence and audit records

With --allow-fake-provider the provider may be the existing deterministic E2E fake; that
evidence is explicitly marked provider_mode=fake and cannot satisfy the release gate's
real-local-model requirement.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg2
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = ROOT / "backend"
STEPS = [
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redacted(url: str) -> str:
    try:
        parsed = make_url(url)
    except Exception:
        return "<unparseable>"
    return f"{parsed.drivername}://{parsed.username or ''}@{parsed.host or ''}:{parsed.port or ''}/{parsed.database or ''}"


class Journey:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.client = httpx.Client(
            base_url=args.base_url.rstrip("/"),
            timeout=httpx.Timeout(args.timeout, connect=10.0),
            follow_redirects=False,
        )
        self.csrf = ""
        self.me: dict = {}
        self.steps: dict[str, dict] = {}
        self.failures: list[str] = []
        self.blockers: list[str] = []
        self.artifacts: dict = {}

    def close(self) -> None:
        self.client.close()

    def step(self, name: str, status: str, detail: str, data: dict | None = None) -> None:
        assert name in STEPS, name
        self.steps[name] = {
            "step": name,
            "status": status,
            "detail": detail[:1000],
            "data": data or {},
            "at": _now(),
        }
        print(f"[{status}] {name}: {detail}", flush=True)
        if status == "FAIL":
            self.failures.append(f"{name}: {detail}")
        elif status == "BLOCKED":
            self.blockers.append(f"{name}: {detail}")

    def headers(self) -> dict:
        return {"X-CSRF-Token": self.csrf} if self.csrf else {}

    def require(self, response: httpx.Response, expected: tuple[int, ...], label: str) -> bool:
        if response.status_code in expected:
            return True
        self.step(
            "authentication" if label == "login" else "evidence_and_audit",
            "FAIL",
            f"{label}: expected {expected}, got {response.status_code}: {response.text[:300]}",
        )
        return False

    def run(self) -> int:
        if not self._health():
            return self._finish()
        if not self._unauthorized():
            return self._finish()
        if not self._login():
            return self._finish()
        if not self._stream():
            return self._finish()
        self._history()
        source_id = self._ingest()
        if source_id:
            self._retrieve(source_id)
        goal_id, task_id = self._supervised_task()
        if goal_id and task_id:
            self._approve_and_execute(goal_id, task_id)
        self._cancel()
        self._evidence()
        return self._finish()

    def _health(self) -> bool:
        try:
            response = self.client.get("/api/health")
        except Exception as exc:
            self.step("authentication", "BLOCKED", f"backend unreachable: {exc}")
            return False
        if response.status_code != 200:
            self.step("authentication", "BLOCKED", f"/api/health returned {response.status_code}")
            return False
        return True

    def _unauthorized(self) -> bool:
        with httpx.Client(base_url=self.args.base_url.rstrip("/"), timeout=20.0) as anonymous:
            checks = {
                "library": "/api/library",
                "execution_goals": "/api/mainai/execution/goals",
                "agent_tasks": "/api/admin/agents/tasks",
                "incidents": "/api/incidents/health",
            }
            results = {}
            for name, path in checks.items():
                response = anonymous.get(path)
                results[name] = response.status_code
            ok = all(status in (401, 403) for status in results.values())
            self.step(
                "unauthorized_routes",
                "PASS" if ok else "FAIL",
                "all protected routes rejected anonymous callers" if ok else f"unexpected statuses: {results}",
                results,
            )
            return ok

    def _login(self) -> bool:
        response = self.client.post(
            "/api/auth/login",
            json={"email": self.args.founder_email, "password": self.args.founder_password},
        )
        if response.status_code != 200:
            self.step(
                "authentication",
                "FAIL",
                f"founder login returned {response.status_code}: {response.text[:300]}",
            )
            return False
        body = response.json()
        self.csrf = body.get("csrf_token", "")
        self.me = body
        if not self.csrf:
            self.step("authentication", "FAIL", "login succeeded without a CSRF token")
            return False
        self.step(
            "authentication",
            "PASS",
            "founder authenticated with session + CSRF",
            {"user_id": body.get("id"), "email": body.get("email")},
        )
        return True

    def _stream(self) -> bool:
        marker = f"V6LAB-{uuid.uuid4().hex[:12]}"
        payload = {
            "message": f"{marker}: svara kort att den lokala modellen är redo.",
            "client_message_id": f"v6-stream-{uuid.uuid4()}",
        }
        started = time.perf_counter()
        start: dict | None = None
        deltas: list[str] = []
        done: dict | None = None
        error: str | None = None
        try:
            with self.client.stream(
                "POST",
                "/api/chat/stream",
                json=payload,
                headers=self.headers(),
                timeout=httpx.Timeout(240.0, connect=10.0),
            ) as response:
                if response.status_code != 200:
                    self.step(
                        "streaming_chat",
                        "FAIL",
                        f"stream returned {response.status_code}: {response.read()[:300]!r}",
                    )
                    return False
                event = None
                data_lines: list[str] = []
                for line in response.iter_lines():
                    if line == "":
                        if data_lines:
                            raw = "\n".join(data_lines)
                            try:
                                data = json.loads(raw)
                            except json.JSONDecodeError:
                                data = None
                            if event == "start":
                                start = data
                            elif event == "delta" and isinstance(data, dict):
                                deltas.append(str(data.get("text", "")))
                            elif event == "done":
                                done = data
                            elif event == "error":
                                error = (data or {}).get("message", "stream error")
                        event = None
                        data_lines = []
                    elif line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].strip())
        except Exception as exc:
            self.step("streaming_chat", "FAIL", f"stream failed: {type(exc).__name__}: {exc}")
            return False
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        if start is None or done is None or error:
            self.step(
                "streaming_chat",
                "FAIL",
                f"incomplete SSE: start={start is not None}, done={done is not None}, error={error}",
            )
            return False
        reply = done.get("reply") or ""
        provider = done.get("provider") or ""
        self.artifacts.update(
            {
                "conversation_id": done.get("conversation_id"),
                "stream_user_message_id": done.get("user_message_id"),
                "stream_assistant_message_id": done.get("assistant_message_id"),
                "provider": provider,
                "model": done.get("model"),
                "stream_latency_ms": elapsed,
                "stream_delta_count": len(deltas),
                "stream_text": "".join(deltas)[:500],
            }
        )
        self.step(
            "streaming_chat",
            "PASS",
            f"SSE start/done received; reply length {len(reply)}; provider={provider or 'unknown'}",
            {"provider": provider, "model": done.get("model"), "latency_ms": elapsed},
        )
        if not deltas or not reply:
            self.step("streaming_delivery", "FAIL", "stream completed without rendered deltas/reply")
            return False
        self.step(
            "streaming_delivery",
            "PASS",
            f"{len(deltas)} SSE delta(s) reached the client before done",
            {"deltas": len(deltas)},
        )
        if self.args.require_local_provider and provider != "ollama":
            self.step(
                "streaming_chat",
                "BLOCKED",
                f"real local provider required, observed {provider or 'none'}",
                {"provider": provider},
            )
            return False
        return True

    def _history(self) -> None:
        conversation_id = self.artifacts.get("conversation_id")
        if not conversation_id:
            self.step("conversation_history", "FAIL", "no conversation id from streaming")
            return
        response = self.client.get(f"/api/conversations/{conversation_id}", headers=self.headers())
        if response.status_code != 200:
            self.step("conversation_history", "FAIL", f"conversation GET {response.status_code}")
            return
        messages = response.json().get("messages") or []
        texts = [m.get("content", "") for m in messages]
        ok = any("V6LAB-" in text for text in texts) and any(
            m.get("role") == "assistant" and m.get("content") for m in messages
        )
        self.step(
            "conversation_history",
            "PASS" if ok else "FAIL",
            f"reopened {len(messages)} persisted message(s)" if ok else "user/assistant message missing after reopen",
            {"messages": len(messages)},
        )

    def _ingest(self) -> str | None:
        marker = f"V6LAB-KNOWLEDGE-{uuid.uuid4().hex[:12]}"
        filename = f"v6-testlab-{uuid.uuid4().hex[:8]}.txt"
        content = (
            "LifeAI V6 isolated acceptance source.\n"
            f"{marker} ar en unik markor for pgvector-retrieval.\n"
            "Dokumentet ar syntetiskt och far endast finnas i testlabbet.\n"
        ).encode()
        response = self.client.post(
            "/api/library/import",
            files={"file": (filename, content, "text/plain")},
            headers=self.headers(),
        )
        if response.status_code != 200:
            self.step("document_ingestion", "FAIL", f"import {response.status_code}: {response.text[:300]}")
            return None
        job_id = response.json().get("id")
        deadline = time.monotonic() + self.args.ingest_timeout
        last: dict = {}
        while time.monotonic() < deadline:
            poll = self.client.get(f"/api/library/jobs/{job_id}", headers=self.headers())
            if poll.status_code != 200:
                self.step("document_ingestion", "FAIL", f"job poll {poll.status_code}")
                return None
            last = poll.json()
            if last.get("status") not in {"pending", "running"}:
                break
            time.sleep(1)
        source_id = None
        for item in last.get("file_results") or []:
            if item.get("source_id"):
                source_id = str(item["source_id"])
                break
        if not source_id:
            listing = self.client.get("/api/library", headers=self.headers())
            if listing.status_code == 200:
                for item in listing.json():
                    if item.get("original_filename") == filename or item.get("title") == filename:
                        source_id = str(item.get("id"))
                        break
        if not source_id:
            self.step(
                "document_ingestion",
                "FAIL",
                f"import job {last.get('status')} produced no source_id",
                {"job_id": job_id, "status": last.get("status")},
            )
            return None
        detail = self.client.get(f"/api/library/{source_id}", headers=self.headers())
        body = detail.json() if detail.status_code == 200 else {}
        indexed = body.get("status") == "indexed" and int(body.get("chunk_count") or 0) > 0
        self.artifacts.update(
            {
                "import_job_id": job_id,
                "source_id": source_id,
                "source_status": body.get("status"),
                "source_chunk_count": body.get("chunk_count"),
                "source_marker": marker,
                "source_filename": filename,
            }
        )
        self.step(
            "document_ingestion",
            "PASS" if indexed else "FAIL",
            f"job={last.get('status')}, source={body.get('status')}, chunks={body.get('chunk_count')}",
            {"job_id": job_id, "source_id": source_id},
        )
        return source_id if indexed else None

    def _retrieve(self, source_id: str) -> None:
        marker = self.artifacts.get("source_marker")
        response = self.client.post(
            "/api/chat",
            json={
                "message": f"Var namns {marker}? Svara kort och citera kallan.",
                "client_message_id": f"v6-retrieval-{uuid.uuid4()}",
            },
            headers=self.headers(),
            timeout=httpx.Timeout(240.0, connect=10.0),
        )
        if response.status_code != 200:
            self.step("pgvector_retrieval", "FAIL", f"retrieval chat {response.status_code}: {response.text[:300]}")
            return
        body = response.json()
        sources = body.get("sources") or []
        cited = any(str(item.get("document_id")) == source_id for item in sources)
        ok = body.get("assistant_status") == "succeeded" and cited
        self.artifacts["retrieval_sources"] = len(sources)
        self.artifacts["retrieval_cited_source"] = cited
        self.step(
            "pgvector_retrieval",
            "PASS" if ok else "FAIL",
            f"assistant={body.get('assistant_status')}, sources={len(sources)}, cited_uploaded_source={cited}",
            {"sources": len(sources), "cited": cited},
        )

    def _supervised_task(self) -> tuple[str | None, str | None]:
        title = f"V6 supervised audit {uuid.uuid4().hex[:8]}"
        goal_response = self.client.post(
            "/api/mainai/execution/goals",
            json={
                "title": title,
                "original_instruction": "Perform one isolated read-only audit. Change no files.",
                "risk_level": "low",
                "approval_policy": "autonomous_development_work",
            },
            headers=self.headers(),
        )
        if goal_response.status_code != 201:
            self.step("supervised_task", "FAIL", f"goal create {goal_response.status_code}: {goal_response.text[:300]}")
            return None, None
        goal_id = str(goal_response.json()["id"])
        plan_response = self.client.post(
            f"/api/mainai/execution/goals/{goal_id}/plan",
            headers=self.headers(),
            timeout=httpx.Timeout(240.0, connect=10.0),
        )
        if plan_response.status_code != 201:
            self.step(
                "supervised_task",
                "FAIL",
                f"plan {plan_response.status_code}: {plan_response.text[:300]}",
                {"goal_id": goal_id},
            )
            return None, None
        tasks_response = self.client.get(
            f"/api/mainai/execution/goals/{goal_id}/tasks", headers=self.headers()
        )
        tasks = tasks_response.json() if tasks_response.status_code == 200 else []
        if not tasks:
            self.step("supervised_task", "FAIL", "plan created no tasks", {"goal_id": goal_id})
            return None, None
        task = tasks[0]
        task_id = str(task["id"])
        self.artifacts.update(
            {
                "goal_id": goal_id,
                "task_id": task_id,
                "task_type": task.get("task_type"),
                "approval_required": task.get("approval_required"),
            }
        )
        self.step(
            "supervised_task",
            "PASS",
            f"goal/plan created task {task_id} ({task.get('task_type')}), approval_required={task.get('approval_required')}",
            {"goal_id": goal_id, "task_id": task_id},
        )
        return goal_id, task_id

    def _approve_and_execute(self, goal_id: str, task_id: str) -> None:
        before = self.client.get(f"/api/mainai/execution/tasks/{task_id}", headers=self.headers())
        before_body = before.json() if before.status_code == 200 else {}
        if before_body.get("approval_granted"):
            self.step("approval_authorization", "FAIL", "task was already approved before founder action")
            return
        proposal = self.client.post(
            f"/api/execution-envelopes/goals/{goal_id}/propose",
            json={
                "proposed_paths": ["README.md"],
                "proposed_capabilities": ["read_file"],
                "proposed_risk": "low",
            },
            headers=self.headers(),
        )
        if proposal.status_code != 201:
            self.step(
                "approval_authorization",
                "FAIL",
                f"scope proposal {proposal.status_code}: {proposal.text[:300]}",
            )
            return
        proposal_id = proposal.json()["id"]
        current_before = self.client.get(
            f"/api/execution-envelopes/current?goal_id={goal_id}", headers=self.headers()
        )
        if current_before.status_code == 200 and current_before.json() is not None:
            self.step("approval_authorization", "FAIL", "an envelope existed before explicit authorization")
            return
        authorization = self.client.post(
            f"/api/execution-envelopes/proposals/{proposal_id}/authorize",
            json={
                "authorized_paths": ["README.md"],
                "authorized_capabilities": ["read_file"],
                "authorized_risk": "low",
            },
            headers=self.headers(),
        )
        if authorization.status_code != 201:
            self.step(
                "approval_authorization",
                "FAIL",
                f"scope authorize {authorization.status_code}: {authorization.text[:300]}",
            )
            return
        envelope = authorization.json()
        approval = self.client.post(
            f"/api/mainai/execution/tasks/{task_id}/approve", headers=self.headers()
        )
        if approval.status_code != 200:
            self.step(
                "approval_authorization",
                "FAIL",
                f"task approve {approval.status_code}: {approval.text[:300]}",
            )
            return
        after = approval.json()
        current = self.client.get(
            f"/api/execution-envelopes/current?goal_id={goal_id}", headers=self.headers()
        )
        current_body = current.json() if current.status_code == 200 else None
        # REPO-WRITE AUTHORITY != PROVIDER-SPEND AUTHORITY. read_only_audit makes a real
        # provider call, so the founder must grant a separate, bounded spend budget. Without
        # this grant the supervisor correctly parks the task PROVIDER_SPEND_NOT_AUTHORIZED;
        # granting it is also what wakes that exact park. Allowlist the provider/model the
        # live stream actually reported instead of granting an unbounded provider scope.
        provider = self.artifacts.get("provider")
        model = self.artifacts.get("model")
        spend = self.client.post(
            "/api/provider-spend/authorize",
            json={
                "goal_id": goal_id,
                "execution_envelope_id": envelope.get("id"),
                "max_cost_usd": "1.00",
                "max_requests": 3,
                "max_cost_per_request_usd": "0.25",
                "max_prompt_tokens": 4000,
                "max_completion_tokens": 1000,
                "allowed_providers": [provider] if provider else [],
                "allowed_models": [model] if model else [],
                "idempotency_key": f"v6-spend-{uuid.uuid4()}",
            },
            headers=self.headers(),
        )
        spend_body = spend.json() if spend.status_code == 201 else {}
        ok = (
            after.get("approval_granted") is True
            and current_body is not None
            and current_body.get("authorized_paths") == ["README.md"]
            and current_body.get("authorized_capabilities") == ["read_file"]
            and spend.status_code == 201
            and spend_body.get("status") == "active"
        )
        self.artifacts["envelope_id"] = envelope.get("id")
        self.artifacts["provider_spend_authorization_id"] = spend_body.get("id")
        self.step(
            "approval_authorization",
            "PASS" if ok else "FAIL",
            "explicit founder scope + provider-spend authorization and task approval recorded"
            if ok
            else (
                f"approval/envelope/spend mismatch: {after.get('approval_granted')}, "
                f"{current_body}, spend={spend.status_code}:{spend.text[:200]}"
            ),
            {
                "envelope_id": envelope.get("id"),
                "provider_spend_authorization_id": spend_body.get("id"),
            },
        )
        if not ok:
            return

        deadline = time.monotonic() + self.args.execution_timeout
        last = after
        while time.monotonic() < deadline:
            poll = self.client.get(f"/api/mainai/execution/tasks/{task_id}", headers=self.headers())
            if poll.status_code == 200:
                last = poll.json()
                if last.get("status") in {"completed", "failed", "blocked", "cancelled"}:
                    break
            time.sleep(1)
        status = last.get("status")
        events = last.get("events") or []
        self.artifacts["task_status"] = status
        self.artifacts["task_events"] = [event.get("event_type") for event in events]
        if status == "completed":
            self.step(
                "approved_execution",
                "PASS",
                f"approved isolated read-only task completed with {len(events)} event(s)",
                {"status": status},
            )
        elif status in {"failed", "blocked", "cancelled"}:
            self.step("approved_execution", "FAIL", f"task ended {status}")
        else:
            self.step(
                "approved_execution",
                "BLOCKED",
                f"task did not reach a terminal state within {self.args.execution_timeout}s (last={status})",
            )

    def _cancel(self) -> None:
        key = f"v6-cancel-{uuid.uuid4()}"
        response = self.client.post(
            "/api/mainai/jobs",
            json={"job_type": "message_sequence_backfill", "input_refs": [], "idempotency_key": key},
            headers=self.headers(),
        )
        if response.status_code == 409:
            self.step("active_operation_cancel", "BLOCKED", f"job capability unavailable: {response.text[:300]}")
            return
        if response.status_code != 201:
            self.step("active_operation_cancel", "FAIL", f"job create {response.status_code}: {response.text[:300]}")
            return
        job_id = response.json()["id"]
        cancel = self.client.post(f"/api/mainai/jobs/{job_id}/cancel", headers=self.headers())
        if cancel.status_code not in (200, 409):
            self.step("active_operation_cancel", "FAIL", f"cancel {cancel.status_code}: {cancel.text[:300]}")
            return
        # request_cancel() intentionally only records intent. The worker acknowledges at its
        # next safe checkpoint and then marks the job cancelled, so poll the durable state
        # machine instead of requiring a terminal status in the same instant as the request.
        deadline = time.monotonic() + self.args.cancel_timeout
        body: dict = {}
        while time.monotonic() < deadline:
            detail = self.client.get(f"/api/mainai/jobs/{job_id}", headers=self.headers())
            body = detail.json() if detail.status_code == 200 else {}
            events = [e.get("event_type") for e in body.get("events") or []]
            if body.get("status") == "cancelled" and "cancel_acknowledged" in events:
                break
            time.sleep(0.5)
        status = body.get("status")
        events = [e.get("event_type") for e in body.get("events") or []]
        ok = (
            cancel.status_code == 200
            and status == "cancelled"
            and "cancel_requested" in events
            and "cancel_acknowledged" in events
        )
        self.artifacts["cancelled_job_id"] = job_id
        self.artifacts["cancelled_job_status"] = status
        self.artifacts["cancelled_job_events"] = events
        self.step(
            "active_operation_cancel",
            "PASS" if ok else "FAIL",
            f"durable job cancel status={status}, events={events}",
            {"job_id": job_id, "status": status, "events": events},
        )

    def _evidence(self) -> None:
        conversation_id = self.artifacts.get("conversation_id")
        task_id = self.artifacts.get("task_id")
        audit_count = None
        if self.args.database_url and self.me.get("id"):
            try:
                conn = psycopg2.connect(self.args.database_url, connect_timeout=5)
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT count(*) FROM audit_log WHERE user_id = %s", (self.me["id"],))
                        audit_count = int(cur.fetchone()[0])
                finally:
                    conn.close()
            except Exception as exc:
                self.artifacts["audit_error"] = f"{type(exc).__name__}: {exc}"
        self.artifacts["audit_rows"] = audit_count
        conversation_ok = False
        if conversation_id:
            response = self.client.get(f"/api/conversations/{conversation_id}", headers=self.headers())
            conversation_ok = response.status_code == 200 and len(response.json().get("messages") or []) >= 2
        task_ok = bool(task_id and self.artifacts.get("task_events"))
        ok = conversation_ok and task_ok and audit_count is not None and audit_count > 0
        self.step(
            "evidence_and_audit",
            "PASS" if ok else ("BLOCKED" if audit_count is None else "FAIL"),
            f"conversation_persisted={conversation_ok}, task_events={len(self.artifacts.get('task_events') or [])}, audit_rows={audit_count}",
            {
                "conversation_persisted": conversation_ok,
                "task_events": self.artifacts.get("task_events"),
                "audit_rows": audit_count,
            },
        )

    def _finish(self) -> int:
        missing = [name for name in STEPS if name not in self.steps]
        for name in missing:
            self.step(name, "BLOCKED", "step did not run")
        statuses = [self.steps[name]["status"] for name in STEPS]
        if "FAIL" in statuses:
            status = "FAIL"
        elif "BLOCKED" in statuses:
            status = "BLOCKED"
        else:
            status = "PASS"
        provider = self.artifacts.get("provider")
        payload = {
            "generated_at": _now(),
            "commit": self.args.commit
            or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "status": status,
            "provider_mode": "local" if provider == "ollama" else "fake",
            "environment": {
                "base_url": self.args.base_url,
                "frontend_url": self.args.frontend_url,
                "database_url": _redacted(self.args.database_url) if self.args.database_url else None,
                "redis_url": _redacted(self.args.redis_url) if self.args.redis_url else None,
            },
            "steps": [self.steps[name] for name in STEPS],
            "artifacts": self.artifacts,
            "failures": self.failures,
            "blockers": self.blockers,
        }
        out = Path(self.args.evidence)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(json.dumps({"status": status, "commit": payload["commit"], "failures": self.failures, "blockers": self.blockers}, indent=2))
        return 0 if status == "PASS" else (3 if status == "BLOCKED" else 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--frontend-url", default="http://127.0.0.1:13000")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--redis-url", default=os.environ.get("REDIS_URL", ""))
    parser.add_argument("--founder-email", default=os.environ.get("FOUNDER_EMAIL", "founder@lifeos.local"))
    parser.add_argument("--founder-password", default=os.environ.get("FOUNDER_PASSWORD", "TestFounderPassword123!"))
    parser.add_argument("--evidence", default="delivery/v6-testlab/evidence/founder-journey.json")
    parser.add_argument("--commit", default="")
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--ingest-timeout", type=float, default=180.0)
    parser.add_argument("--execution-timeout", type=float, default=120.0)
    parser.add_argument("--cancel-timeout", type=float, default=30.0)
    parser.add_argument("--allow-fake-provider", action="store_true")
    parser.add_argument("--require-local-provider", dest="require_local_provider", action="store_true", default=True)
    parser.add_argument("--no-require-local-provider", dest="require_local_provider", action="store_false")
    args = parser.parse_args()
    if args.allow_fake_provider:
        args.require_local_provider = False
    parsed = make_url(args.base_url)
    if parsed.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("refusing a non-loopback test-lab base URL")
    journey = Journey(args)
    try:
        return journey.run()
    finally:
        journey.close()


if __name__ == "__main__":
    raise SystemExit(main())
