"""AR-9..AR-12 — races on the new duplicate-work guards, and truthfulness of "independently verified".

  AR-9   Two CONCURRENT dispatches of the same created agent task -> at most ONE paid provider call
         (PR claim: "a second paid agent dispatch is refused").
  AR-9h  Same, through the Founder HTTP route (two concurrent POST /dispatch).
  AR-10  Two CONCURRENT delegation requests for the same MainAI task -> at most one open request
         (PR claim: "duplicate live workforce assignment ... refused").
  AR-11  A Path A task completed by a real worker cycle, verified only by the builder's own job
         (same job ran the tests), must NOT be labelled `independently_verified`
         (status.py docstring: requires evidence "from a party that is not the builder").
  AR-12  Chat in safe test mode (no provider keys): no provider call reaches the network, the failed
         assistant turn is durable and visible after reload.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.orm import sessionmaker

from _guards import egress  # noqa: F401
from _harness import _privileges, _production_spend_gate, github, owner_id  # noqa: F401

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


# ------------------------------------------------------------------ AR-9
@pytest.mark.asyncio
async def test_AR9_concurrent_dispatch_single_paid_call(db_session, monkeypatch):
    from app.agent_orchestration import create_agent_task, dispatch_task
    from app.db import SessionLocal
    from app.mainai_founder_ops.assignment_guard import DuplicateWorkError
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    calls = []

    async def slow_chat(self, messages, model, **kwargs):
        calls.append(model)
        await asyncio.sleep(0.3)  # a real provider round-trip is seconds, not 0
        return ChatResult(content="diff", provider="openai", model=model, raw_usage={})

    monkeypatch.setattr(OpenAIProvider, "chat", slow_chat)
    task = create_agent_task(db_session, title="race", description="d", acceptance_criteria="a", created_by="test")
    db_session.commit()

    async def one():
        s = SessionLocal()
        try:
            await dispatch_task(s, task.id, dispatched_by="founder")
            s.commit()
            return "ok"
        except DuplicateWorkError:
            s.rollback()
            return "refused"
        except Exception as exc:  # noqa: BLE001
            s.rollback()
            return f"error:{type(exc).__name__}"
        finally:
            s.close()

    results = await asyncio.gather(one(), one())
    print(f"AR9|results={results} paid_calls={len(calls)}")
    assert len(calls) <= 1, f"AR-9 two concurrent dispatches made {len(calls)} paid provider calls ({results})"


def test_AR9h_concurrent_dispatch_over_http(monkeypatch, db_session):
    """Two Founder tabs pressing 'dispatch' at once (separate TestClients, separate threads)."""
    from fastapi.testclient import TestClient

    from app.agent_orchestration import create_agent_task
    from app.limiter import limiter
    from app.main import app
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    monkeypatch.setattr(limiter, "enabled", False)
    calls = []

    async def slow_chat(self, messages, model, **kwargs):
        calls.append(model)
        await asyncio.sleep(0.5)
        return ChatResult(content="diff", provider="openai", model=model, raw_usage={})

    monkeypatch.setattr(OpenAIProvider, "chat", slow_chat)
    task = create_agent_task(db_session, title="race-http", description="d", acceptance_criteria="a", created_by="test")
    db_session.commit()
    paths = [r.path for r in app.routes if getattr(r, "path", "").endswith("/dispatch") and "{task_id}" in r.path]
    assert paths, "no dispatch route"
    url = paths[0].replace("{task_id}", str(task.id))
    clients = []
    for _ in range(2):
        c = TestClient(app, base_url="https://testserver")
        c.__enter__()
        res = c.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
        assert res.status_code == 200, res.text
        clients.append((c, res.json()["csrf_token"]))
    codes = []
    barrier = threading.Barrier(2)

    def fire(c, csrf):
        barrier.wait()
        codes.append(c.post(url, headers={"X-CSRF-Token": csrf}).status_code)

    ts = [threading.Thread(target=fire, args=cc) for cc in clients]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    for c, _ in clients:
        c.__exit__(None, None, None)
    print(f"AR9h|url={paths[0]} codes={sorted(codes)} paid_calls={len(calls)}")
    assert len(calls) <= 1, f"AR-9h two concurrent HTTP dispatches -> {len(calls)} paid calls, codes={codes}"


# ------------------------------------------------------------------ AR-10
def _one_delegation_race(superuser_db):
    from app.db import migration_engine
    from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
    from app.models.mainai_execution import MainAITask
    from app.models.user import User
    from app.workforce import submit_delegation_request

    owner = User(email=f"ops-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(owner)
    superuser_db.flush()
    goal = create_goal(superuser_db, owner_id=owner.id, title="g", original_instruction="g", created_by="test")
    create_plan(superuser_db, goal=goal, rationale="r", tasks=[PlannedTaskSpec(description="w", task_type="read_only_audit")], created_by="test")
    task = superuser_db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    superuser_db.commit()
    owner_id, task_id = owner.id, task.id  # plain values: never touch another session's objects from a thread
    barrier = threading.Barrier(2)
    out = []

    def worker(i):
        with sessionmaker(bind=migration_engine)() as s:
            barrier.wait()
            try:
                submit_delegation_request(s, owner_id=owner_id, goal_text=f"g{i}", required_capability="low_risk_classification",
                                          task_id=task_id, verification_requirement="independent_verifier")
                s.commit()
                out.append("ok")
            except Exception as exc:  # noqa: BLE001
                s.rollback()
                out.append(type(exc).__name__)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    return superuser_db.execute(sa_text("SELECT count(*) FROM workforce_delegation_requests WHERE task_id=:t"), {"t": str(task_id)}).scalar_one()


def test_AR10_concurrent_delegation_single_request(superuser_db):
    counts = [_one_delegation_race(superuser_db) for _ in range(20)]
    dup = sum(1 for n in counts if n > 1)
    print(f"AR10|races=20 duplicated={dup} counts={counts}")
    assert dup == 0, f"AR-10 concurrent delegation produced duplicate open requests for one task in {dup}/20 races"


# ------------------------------------------------------------------ AR-11
@pytest.mark.asyncio
async def test_AR11_builder_self_verification_is_not_independent(db_session, superuser_db, owner_id, monkeypatch, github):
    from app.mainai_execution import execution_job
    from app.mainai_founder_ops.snapshot import build_founder_ops_snapshot
    from app.models.mainai_execution import MainAITaskEvent, MainAITaskEventType, MainAITaskStatus
    from app.models.mainai_wait import MainAITaskWaitStatus
    from _harness import PATHS, drive, edit_payload, install_provider, plan_payload, planned_goal, tasks_by_type

    install_provider(monkeypatch, plan=plan_payload((PATHS, True)), edit_for=lambda u: edit_payload(PATHS))
    goal = await planned_goal(db_session, owner_id, paths=PATHS)

    async def green(db, wait):
        wait.status = MainAITaskWaitStatus.satisfied
        wait.evidence = {"scripted": "ci green"}
        wait.resolved_at = datetime.utcnow()
        db.flush()
        return wait

    monkeypatch.setattr(execution_job, "poll_ci_wait", green)
    await drive(superuser_db, goal.id, rounds=3)
    edit = tasks_by_type(superuser_db, goal.id)["repo_edit"][0]
    assert edit.status == MainAITaskStatus.completed, f"cycle did not complete the repo_edit task: {edit.status}"
    ev = superuser_db.query(MainAITaskEvent).filter(MainAITaskEvent.task_id == edit.id,
                                                   MainAITaskEvent.event_type == MainAITaskEventType.verification_passed).all()
    dispatch_jobs = {e.detail.get("mainai_job_id") for e in superuser_db.query(MainAITaskEvent).filter(
        MainAITaskEvent.task_id == edit.id, MainAITaskEvent.event_type == MainAITaskEventType.dispatched)}
    snap = build_founder_ops_snapshot(superuser_db, owner_id=owner_id, health={"status": "ok", "worker": "unknown"})
    item = next(i for i in snap["work_items"] if i.get("task_id") == str(edit.id))
    print(f"AR11|verification_events={len(ev)} dispatch_jobs={len(dispatch_jobs)} founder_status={item['founder_status']} "
          f"independently_verified={item['independently_verified']}")
    assert item["founder_status"] != "independently_verified", (
        "AR-11 a task verified only by its own builder job (verification_passed emitted by execution_job after its own "
        "pytest run) is shown to the Founder as 'Självständigt verifierad'")


# ------------------------------------------------------------------ AR-12
def test_AR12_chat_safe_mode_no_paid_egress_and_failed_turn_visible(client, egress, monkeypatch):
    from app.limiter import limiter

    monkeypatch.setattr(limiter, "enabled", False)
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    csrf = res.json()["csrf_token"]
    sent = client.post("/api/chat", json={"message": "hej"}, headers={"X-CSRF-Token": csrf})
    print(f"AR12|chat_status={sent.status_code} provider_attempts={egress.provider_calls} egress={egress.sockets}")
    assert not egress.sockets, f"chat opened egress in safe mode: {egress.sockets}"
    if sent.status_code == 200:
        cid = sent.json().get("conversation_id")
        msgs = client.get(f"/api/conversations/{cid}").json()["messages"]
        assistants = [m for m in msgs if m["role"] == "assistant"]
        assert assistants and assistants[-1]["status"] == "failed", f"failed turn not durable/visible: {assistants}"


# ------------------------------------------------------------------ AR-13 / AR-11b
async def _safe_mode_cycle(db_session, superuser_db, owner_id, monkeypatch, github, tmp_path):
    """Full real-worker Path A cycle with GitHub fully configured but github_write_enabled at its safe default (False).

    Proposal mode writes the code agent's files into the backend's OWN checkout (`_repo_root()`) and runs pytest
    there. That is redirected to a disposable clone here (an earlier, unredirected run wrote into the examiner's
    checkout and its pytest picked up the real tests/conftest.py, which dropped the examiner's test database)."""
    import subprocess

    from app.config import get_settings
    from app.mainai_execution import execution_job
    from app.mainai_founder_ops.snapshot import build_founder_ops_snapshot
    from _harness import PATHS, drive, edit_payload, install_provider, plan_payload, planned_goal, task_branches, trail

    clone = tmp_path / "server-checkout"
    subprocess.run(["git", "clone", "-q", str(github.bare), str(clone)], check=True)
    monkeypatch.setattr(execution_job, "_repo_root", lambda: clone)
    monkeypatch.setattr(get_settings(), "github_write_enabled", False)
    install_provider(monkeypatch, plan=plan_payload((PATHS, True)), edit_for=lambda u: edit_payload(PATHS))
    goal = await planned_goal(db_session, owner_id, paths=PATHS)
    await drive(superuser_db, goal.id, rounds=3)
    branches = task_branches(github.bare)
    local = [l for l in subprocess.run(["git", "-C", str(clone), "status", "--porcelain", "-uall"], capture_output=True, text=True).stdout.split("\n") if l]
    snap = build_founder_ops_snapshot(superuser_db, owner_id=owner_id, health={"status": "ok", "worker": "unknown"})
    items = [i for i in snap["work_items"] if i["source"] == "mainai_task"]
    print(f"SAFE|remote_task_branches={branches} prs={len(github.prs)} local_server_checkout_writes={local} "
          f"labels={sorted((i['description'][:20], i['source_status'], i['founder_status']) for i in items)} trail_tail={trail(superuser_db, goal.id)[-1:]}")
    return branches, github.prs, items, local, snap


@pytest.mark.asyncio
async def test_AR13_safe_launch_default_never_pushes_or_opens_pr(db_session, superuser_db, owner_id, monkeypatch, github, tmp_path):
    branches, prs, _, _, _ = await _safe_mode_cycle(db_session, superuser_db, owner_id, monkeypatch, github, tmp_path)
    assert branches == [] and prs == [], f"remote effect with github_write_enabled=False: branches={branches} prs={prs}"


@pytest.mark.asyncio
async def test_AR11b_unopened_pr_with_zero_checks_is_not_independently_verified(db_session, superuser_db, owner_id, monkeypatch, github, tmp_path):
    _, prs, items, _, _ = await _safe_mode_cycle(db_session, superuser_db, owner_id, monkeypatch, github, tmp_path)
    pr_task = next(i for i in items if "PR" in i["description"])
    assert not prs
    assert pr_task["founder_status"] != "independently_verified", (
        "AR-11b the 'Open a draft PR' task opened NO pull request and ran ZERO verification steps, yet the Founder sees "
        "'Självständigt verifierad'")


@pytest.mark.asyncio
async def test_AR13b_safe_launch_never_writes_the_server_checkout_unannounced(db_session, superuser_db, owner_id, monkeypatch, github, tmp_path):
    """P-3: in the safe-launch profile the paid code agent's output must not be written into the backend's own
    checkout, unless the Founder-facing safe_launch flags say so explicitly (a key naming local writes, set true)."""
    _, _, _, local, snap = await _safe_mode_cycle(db_session, superuser_db, owner_id, monkeypatch, github, tmp_path)
    announced = any("local" in k and v is True for k, v in snap["safe_launch"].items())
    print(f"AR13b|server_checkout_writes={local} announced_in_safe_launch={announced}")
    assert not local or announced, f"P-3 safe mode wrote {local} into the server checkout; safe_launch={snap['safe_launch']}"
