"""Examiner-only adversarial probes for Cursor's Path A PR #264 (21ff3cc). Run from OUTSIDE the
candidate with the target's own conftest. Each probe prints `MATRIX|id|HOLDS/GAP|evidence` and
never raises on GAP, so the full matrix always prints. No live provider, GitHub or agent call:
providers are monkeypatched, GitHub is never configured."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution import billed_chat, execution_job, planner
from app.mainai_execution.billed_chat import ExecutionSpendError, billed_execution_chat
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import MainAITask
from app.models.provider_spend import ProviderSpendUsageEvent
from app.provider_spend import authorize_provider_spend
from app.providers.base import ChatResult, Message
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var


def M(pid, holds, evidence):
    print(f"\nMATRIX|{pid}|{'HOLDS' if holds else 'GAP'}|{evidence}")


@pytest.fixture(autouse=True, scope="module")
def _privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    return user.id


def _goal(db, owner_id):
    return planner.create_goal(db, owner_id=owner_id, title="exam", original_instruction="examiner probe goal", created_by="examiner")


def _task(db, goal, description, plan=None):
    planner.create_plan(db, goal=goal, rationale="exam", created_by="examiner", tasks=[PlannedTaskSpec(
        description=description, task_type="repo_edit",
        verification_plan=plan if plan is not None else [{"kind": "targeted_tests", "target": "tests/backend/test_x.py"}])])
    db.commit()
    return db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()


def _fake_model(captured, files):
    async def _chat(self, messages, model, **kwargs):
        captured["prompt"] = messages[1].content
        return ChatResult(content=json.dumps({"files": files, "commit_message": "exam"}), provider="openai", model=model, raw_usage={})
    return _chat


# ---------------------------------------------------------------- file context the editing model sees
@pytest.mark.asyncio
async def test_unseen_existing_file_cannot_be_overwritten(db_session, owner_id, monkeypatch, tmp_path):
    target = tmp_path / "backend" / "app" / "core.py"
    target.parent.mkdir(parents=True)
    target.write_text("IMPORTANT = 'original logic'\n")
    monkeypatch.setattr(execution_job, "_repo_root", lambda: tmp_path)
    captured = {}
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_model(captured, [{"path": "backend/app/core.py", "content": "IMPORTANT = 'rewritten blind'\n"}]))
    task = _task(db_session, _goal(db_session, owner_id), "Improve error handling in the core module.")  # names no path
    try:
        await execution_job._propose_repo_edit(db_session, task)
        refused = False
    except Exception as exc:  # noqa: BLE001
        refused, captured["error"] = True, str(exc)[:80]
    seen = "original logic" in captured.get("prompt", "")
    M("CTX-1 edit of a file the model never saw", refused,
      f"model saw original={seen}; write {'refused' if refused else 'ACCEPTED'}; file now={target.read_text().strip()!r}")


@pytest.mark.asyncio
async def test_large_file_is_not_silently_truncated_by_an_edit(db_session, owner_id, monkeypatch, tmp_path):
    target = tmp_path / "backend" / "app" / "big.py"
    target.parent.mkdir(parents=True)
    body = "".join(f"LINE_{i:05d} = {i}\n" for i in range(2000))  # ~30k chars, > 16k per-file context cap
    target.write_text(body)
    monkeypatch.setattr(execution_job, "_repo_root", lambda: tmp_path)
    captured = {}

    async def _faithful_model(self, messages, model, **kwargs):
        # A faithful model returns the COMPLETE text it was shown, with one edit at the top.
        prompt = messages[1].content
        shown = prompt.split("### backend/app/big.py\n```\n", 1)[1].split("\n```", 1)[0].replace("\n...[truncated]...\n", "")
        captured["shown_chars"] = len(shown)
        content = "# edited\n" + shown
        return ChatResult(content=json.dumps({"files": [{"path": "backend/app/big.py", "content": content}]}), provider="openai", model=model, raw_usage={})

    monkeypatch.setattr(OpenAIProvider, "chat", _faithful_model)
    task = _task(db_session, _goal(db_session, owner_id), "Add a header comment to backend/app/big.py")
    try:
        await execution_job._propose_repo_edit(db_session, task)
        refused = False
    except Exception:  # noqa: BLE001
        refused = True
    after = target.read_text()
    lost = sum(1 for i in range(2000) if f"LINE_{i:05d} = {i}\n" not in after)
    M("CTX-2 large file edit keeps the unseen tail", refused or lost == 0,
      f"original={len(body)} chars, shown={captured.get('shown_chars')} chars; lines silently deleted={lost}/2000; write {'refused' if refused else 'ACCEPTED'}")


# ---------------------------------------------------------------- targeted-test enforcement
def test_repo_edit_without_targeted_tests_is_rejected_at_plan_time(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    try:
        planner.create_plan(db_session, goal=goal, rationale="exam", created_by="examiner",
                            tasks=[PlannedTaskSpec(description="edit backend/app/x.py", task_type="repo_edit", verification_plan=[])])
        db_session.commit()
        rejected = False
    except Exception as exc:  # noqa: BLE001
        db_session.rollback()
        rejected = "targeted" in str(exc).lower() or "verification" in str(exc).lower()
    M("TEST-1 repo_edit without targeted_tests rejected at plan time", rejected,
      "create_plan " + ("rejected it" if rejected else "ACCEPTED it; enforcement only happens in verify_task after the paid edit and local commit"))


# ---------------------------------------------------------------- per-goal provider spend
@pytest.mark.asyncio
async def test_default_configuration_does_not_make_unbilled_ai_calls(db_session, owner_id, monkeypatch):
    settings = get_settings()
    calls = []

    async def _chat(self, messages, model, **kwargs):
        calls.append(model)
        return ChatResult(content="ok", provider="openai", model=model, raw_usage={"prompt_tokens": 1, "completion_tokens": 1})

    monkeypatch.setattr(OpenAIProvider, "chat", _chat)
    goal = _goal(db_session, owner_id)
    try:
        await billed_execution_chat(db_session, [Message(role="user", content="x")], owner_id=owner_id, goal_id=goal.id, purpose="exam", requested_by="exam")
        refused = False
    except ExecutionSpendError:
        refused = True
    events = db_session.query(ProviderSpendUsageEvent).filter(ProviderSpendUsageEvent.goal_id == goal.id).count()
    M("SPEND-1 default config refuses an AI call with no per-goal grant", refused,
      f"MAINAI_EXECUTION_REQUIRE_PROVIDER_SPEND default={settings.mainai_execution_require_provider_spend}; provider calls={len(calls)}; spend events={events}")


def _grant(db, owner_id, goal):
    proposal = propose_execution_scope(db, owner_id=owner_id, goal_id=goal.id, idempotency_key=f"prop-{uuid.uuid4()}")
    _, envelope = authorize_execution_scope(db, owner_id=owner_id, proposal_id=proposal.id, authorized_by="founder",
                                            authorized_paths=["backend/app/x.py"], authorized_capabilities=["read_file", "patch_file"],
                                            authorized_risk="low", envelope_idempotency_key=f"env-{uuid.uuid4()}")
    authorize_provider_spend(db, owner_id=owner_id, goal_id=goal.id, execution_envelope_id=envelope.id, authorized_by="founder",
                             max_cost_usd=Decimal("1.00"), max_requests=5, max_cost_per_request_usd=Decimal("0.50"),
                             idempotency_key=f"spend-{uuid.uuid4()}", allowed_providers=["openai"], allowed_models=["gpt-4o-mini"])
    db.commit()


def _patch_fallback(monkeypatch, *, prompt_tokens, completion_tokens):
    monkeypatch.setattr(billed_chat, "resolve_chat_chain", lambda db: [(SimpleNamespace(name="openai"), "gpt-4o-mini")])

    async def _fallback(db, messages, **kwargs):
        # The primary (allowlisted) provider failed; chat_with_fallback answered from the next one.
        return ChatResult(content="ok", provider="openrouter", model="vendor/unlisted-model",
                          raw_usage={"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}), ["openai", "openrouter"]

    monkeypatch.setattr(billed_chat, "chat_with_fallback", _fallback)


@pytest.mark.asyncio
async def test_fallback_provider_outside_the_grant_allowlist_is_refused(db_session, owner_id, monkeypatch):
    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", True)
    goal = _goal(db_session, owner_id)
    _grant(db_session, owner_id, goal)
    _patch_fallback(monkeypatch, prompt_tokens=10, completion_tokens=5)
    try:
        await billed_execution_chat(db_session, [Message(role="user", content="x")], owner_id=owner_id, goal_id=goal.id, purpose="exam", requested_by="exam")
        refused = False
    except ExecutionSpendError:
        refused = True
    events = db_session.query(ProviderSpendUsageEvent).filter(ProviderSpendUsageEvent.goal_id == goal.id).all()
    settled = [(e.provider, e.model, str(e.cost_usd), e.status) for e in events]
    M("SPEND-2 answer from a provider/model the Founder did not allowlist is refused", refused,
      f"grant allows openai/gpt-4o-mini only; answer came from openrouter/vendor/unlisted-model; accepted={not refused}; recorded={settled}")
    zero = any(Decimal(str(e.cost_usd or 0)) == 0 for e in events)
    M("SPEND-3 an unpriced answer is not settled as $0", not zero, f"settled cost per event={[c for _, _, c, _ in settled]}")


@pytest.mark.asyncio
async def test_answer_exceeding_the_reservation_is_a_handled_spend_failure(db_session, owner_id, monkeypatch):
    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", True)
    goal = _goal(db_session, owner_id)
    _grant(db_session, owner_id, goal)
    _patch_fallback(monkeypatch, prompt_tokens=50_000, completion_tokens=50_000)
    try:
        await billed_execution_chat(db_session, [Message(role="user", content="x")], owner_id=owner_id, goal_id=goal.id, purpose="exam", requested_by="exam")
        kind = "accepted"
    except ExecutionSpendError:
        kind = "ExecutionSpendError"
    except Exception as exc:  # noqa: BLE001
        kind = type(exc).__name__
        db_session.rollback()
    M("SPEND-4 an answer larger than its reservation fails as a handled spend error", kind == "ExecutionSpendError",
      f"raised {kind}; run_task_execution_job only handles TaskExecutionError/VerificationStepError/GitHubClientError/WorktreeError/ExecutionSpendError")


@pytest.mark.asyncio
async def test_over_reservation_answer_does_not_strand_the_task(db_session, superuser_db, owner_id, monkeypatch):
    """End to end through dispatch_ready_task()/run_task_execution_job() with a read_only_audit task."""
    from app.jobs.mainai_job_lease import claim_next_mainai_job
    from app.mainai_execution import executor
    from app.mainai_execution.execution_job import run_task_execution_job
    from app.models.mainai_job import MainAIJob

    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", True)
    goal = _goal(db_session, owner_id)
    planner.create_plan(db_session, goal=goal, rationale="exam", created_by="examiner",
                        tasks=[PlannedTaskSpec(description="audit", task_type="read_only_audit", verification_plan=[])])
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    _grant(db_session, owner_id, goal)
    _patch_fallback(monkeypatch, prompt_tokens=50_000, completion_tokens=50_000)
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="examiner")
    db_session.commit()
    _, _, generation = claim_next_mainai_job(superuser_db, "exam-worker", 120)
    current_user_id_var.set(str(owner_id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})
    escaped = None
    try:
        await run_task_execution_job(db_session, job.id, owner_id, worker_id="exam-worker", lease_generation=generation, lease_seconds=120)
    except Exception as exc:  # noqa: BLE001
        escaped = type(exc).__name__
    superuser_db.expire_all()
    status = superuser_db.get(MainAITask, task.id).status.value
    job_status = superuser_db.get(MainAIJob, job.id).status.value
    M("SPEND-5 over-reservation answer leaves the task in a handled terminal/retry state", escaped is None and status != "running",
      f"exception escaped run_task_execution_job={escaped}; task status={status}; job status={job_status}")
