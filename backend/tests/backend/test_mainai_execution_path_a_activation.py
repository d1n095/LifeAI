"""Focused Path A first-cycle capability: configurable bases, existing file contents,
targeted-test requirement, pytest failure feedback, draft PRs, per-goal spend cap."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.integrations.github_client import GitHubClient
from app.mainai_execution import planner
from app.mainai_execution.billed_chat import ExecutionSpendError, billed_execution_chat
from app.mainai_execution.code_agent_context import build_code_agent_user_prompt, candidate_paths_from_text
from app.mainai_execution.execution_job import _handle_open_pr, _propose_repo_edit
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.verification_feedback import latest_verification_failure_text
from app.mainai_execution.verify import VerificationStepError, verify_task
from app.mainai_execution.worktree import execution_pr_base_branch, execution_worktree_base_branch
from app.models.mainai_execution import MainAITask, MainAITaskEvent, MainAITaskEventType
from app.models.provider_spend import ProviderSpendUsageEvent, ProviderSpendUsageStatus
from app.provider_spend import authorize_provider_spend
from app.providers.base import ChatResult, Message
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _apply_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls_user(db_session, user.id)
    return user.id


def _goal(db_session, owner_id, *, title="Path A goal"):
    return planner.create_goal(
        db_session,
        owner_id=owner_id,
        title=title,
        original_instruction="Edit backend/app/example.py and add tests/backend/test_example.py coverage.",
        created_by="test",
    )


def _single_task(db_session, goal, **spec_kwargs) -> MainAITask:
    spec_kwargs.setdefault("description", "edit backend/app/example.py")
    spec_kwargs.setdefault("task_type", "repo_edit")
    spec_kwargs.setdefault("verification_plan", [{"kind": "targeted_tests", "target": "tests/backend/test_example.py"}])
    planner.create_plan(db_session, goal=goal, rationale="path a", tasks=[PlannedTaskSpec(**spec_kwargs)], created_by="test")
    db_session.commit()
    return db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()


def test_execution_branches_are_configurable(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "github_worktree_base_branch", "cursor/founder-alpha-base")
    monkeypatch.setattr(settings, "github_pr_base_branch", "codex/founder-alpha-final-composed-candidate")
    assert execution_worktree_base_branch() == "cursor/founder-alpha-base"
    assert execution_pr_base_branch() == "codex/founder-alpha-final-composed-candidate"


def test_empty_pr_base_falls_back_to_worktree_base(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "github_worktree_base_branch", "cursor/worktree-base")
    monkeypatch.setattr(settings, "github_pr_base_branch", "")
    assert execution_pr_base_branch() == "cursor/worktree-base"


def test_candidate_paths_and_existing_file_contents(tmp_path):
    target = tmp_path / "backend" / "app" / "example.py"
    target.parent.mkdir(parents=True)
    target.write_text("VALUE = 1\n", encoding="utf-8")
    description = "Change backend/app/example.py and ignore ../etc/passwd"
    assert candidate_paths_from_text(description) == ["backend/app/example.py"]
    prompt = build_code_agent_user_prompt(description, tmp_path, failure_context="target=tests/x.py\nstdout:\nFAILED")
    assert "VALUE = 1" in prompt
    assert "Previous verification failure" in prompt
    assert "../etc/passwd" not in candidate_paths_from_text(description)


def test_verify_task_requires_targeted_tests_for_repo_edit():
    class _Task:
        task_type = "repo_edit"
        verification_plan = []

    with pytest.raises(VerificationStepError, match="targeted_tests"):
        verify_task(_Task(), cwd=".")


def test_verify_task_still_allows_empty_plan_for_read_only_audit():
    class _Task:
        task_type = "read_only_audit"
        verification_plan = []

    result = verify_task(_Task(), cwd=".")
    assert result.passed is True
    assert result.steps == []


def test_latest_verification_failure_text_extracts_pytest_tails(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    task = _single_task(db_session, goal)
    db_session.add(
        MainAITaskEvent(
            task_id=task.id,
            owner_id=owner_id,
            event_type=MainAITaskEventType.verification_failed,
            detail={
                "passed": False,
                "steps": [
                    {
                        "kind": "targeted_tests",
                        "passed": False,
                        "detail": {
                            "target": "tests/backend/test_example.py",
                            "stdout_tail": "E       assert 1 == 2",
                            "stderr_tail": "",
                        },
                    }
                ],
            },
        )
    )
    db_session.commit()
    text = latest_verification_failure_text(db_session, task.id)
    assert "tests/backend/test_example.py" in text
    assert "assert 1 == 2" in text


@pytest.mark.asyncio
async def test_propose_repo_edit_supplies_existing_file_contents(db_session, owner_id, monkeypatch, tmp_path):
    from app.mainai_execution import execution_job

    existing = tmp_path / "backend" / "app" / "example.py"
    existing.parent.mkdir(parents=True)
    existing.write_text("OLD = True\n", encoding="utf-8")
    monkeypatch.setattr(execution_job, "_repo_root", lambda: tmp_path)
    captured = {}

    async def _fake_chat(self, messages, model, **kwargs):
        captured["user"] = messages[1].content
        return ChatResult(
            content='{"files": [{"path": "backend/app/example.py", "content": "NEW = True\\n"}], "commit_message": "update"}',
            provider="openai",
            model=model,
            raw_usage={},
        )

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)
    goal = _goal(db_session, owner_id)
    task = _single_task(db_session, goal, description="Update backend/app/example.py")
    result = await _propose_repo_edit(db_session, task)
    assert "OLD = True" in captured["user"]
    assert result["files"][0]["content"] == "NEW = True\n"
    assert (tmp_path / "backend" / "app" / "example.py").read_text(encoding="utf-8") == "NEW = True\n"


@pytest.mark.asyncio
async def test_propose_plan_via_ai_includes_verification_failure_context(db_session, owner_id, monkeypatch):
    captured = {}

    async def _fake_chat(self, messages, model, **kwargs):
        captured["user"] = messages[1].content
        return ChatResult(
            content='{"tasks": [{"description": "retry", "task_type": "read_only_audit"}]}',
            provider="openai",
            model=model,
            raw_usage={},
        )

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)
    goal = _goal(db_session, owner_id)
    await planner.propose_plan_via_ai(db_session, goal=goal, failure_context="target=tests/backend/test_example.py\nstdout:\nFAILED")
    assert "Föregående verifieringsfel" in captured["user"]
    assert "tests/backend/test_example.py" in captured["user"]


@pytest.mark.asyncio
async def test_handle_open_pr_uses_configured_pr_base_and_opens_draft(db_session, owner_id, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "github_write_enabled", True)
    monkeypatch.setattr(settings, "github_repo", "d1n095/LifeAI")
    monkeypatch.setattr(settings, "github_pr_base_branch", "codex/founder-alpha-final-composed-candidate")
    created = {}

    async def _fake_create_pull_request(self, *, title, body, head, base, draft=False):
        created.update({"head": head, "base": base, "draft": draft, "title": title})
        return {"number": 11, "html_url": "https://example.test/pr/11"}

    async def _fake_list(self, *, head, base, state="all"):
        return []

    monkeypatch.setattr(GitHubClient, "create_pull_request", _fake_create_pull_request)
    monkeypatch.setattr(GitHubClient, "list_pull_requests_for_head", _fake_list)
    monkeypatch.setattr(GitHubClient, "is_configured", lambda self: True)

    goal = _goal(db_session, owner_id)
    task = _single_task(db_session, goal, task_type="open_pr", verification_plan=[], description="open pr")
    db_session.add(
        MainAITaskEvent(
            task_id=task.id,
            owner_id=owner_id,
            event_type=MainAITaskEventType.completed,
            detail={"work_result": {"branch": "claude/mainai-task-abc"}},
        )
    )
    db_session.commit()
    result = await _handle_open_pr(db_session, task, goal)
    assert result["proposed"] is False
    assert created["draft"] is True
    assert created["base"] == "codex/founder-alpha-final-composed-candidate"


@pytest.mark.asyncio
async def test_billed_chat_fails_closed_when_spend_is_required_and_missing(db_session, owner_id, monkeypatch):
    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", True)
    goal = _goal(db_session, owner_id)
    with pytest.raises(ExecutionSpendError, match="no live provider-spend"):
        await billed_execution_chat(
            db_session,
            [Message(role="user", content="plan")],
            owner_id=owner_id,
            goal_id=goal.id,
            purpose="mainai_planning",
            requested_by="test",
        )


@pytest.mark.asyncio
async def test_billed_chat_refuses_grant_above_path_a_cap(db_session, owner_id, monkeypatch):
    monkeypatch.setattr(get_settings(), "mainai_execution_goal_spend_cap_usd", 1.0)
    goal = _goal(db_session, owner_id)
    proposal = propose_execution_scope(db_session, owner_id=owner_id, goal_id=goal.id, idempotency_key=f"prop-{uuid.uuid4()}")
    _, envelope = authorize_execution_scope(
        db_session,
        owner_id=owner_id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=["backend/app/example.py"],
        authorized_capabilities=["read_file", "patch_file"],
        authorized_risk="low",
        envelope_idempotency_key=f"env-{uuid.uuid4()}",
    )
    authorize_provider_spend(
        db_session,
        owner_id=owner_id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal("9.00"),
        max_requests=2,
        max_cost_per_request_usd=Decimal("0.50"),
        idempotency_key=f"spend-{uuid.uuid4()}",
        allowed_providers=["openai"],
        allowed_models=["gpt-4o-mini"],
    )
    db_session.commit()
    with pytest.raises(ExecutionSpendError, match="exceeds"):
        await billed_execution_chat(
            db_session,
            [Message(role="user", content="plan")],
            owner_id=owner_id,
            goal_id=goal.id,
            purpose="mainai_planning",
            requested_by="test",
        )


@pytest.mark.asyncio
async def test_billed_chat_reserves_and_settles_against_existing_spend(db_session, owner_id, monkeypatch):
    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", True)
    monkeypatch.setattr(get_settings(), "mainai_execution_goal_spend_cap_usd", 5.0)

    async def _fake_chat(self, messages, model, **kwargs):
        return ChatResult(content="ok", provider="openai", model=model, raw_usage={"prompt_tokens": 10, "completion_tokens": 5})

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)
    goal = _goal(db_session, owner_id)
    proposal = propose_execution_scope(db_session, owner_id=owner_id, goal_id=goal.id, idempotency_key=f"prop-{uuid.uuid4()}")
    _, envelope = authorize_execution_scope(
        db_session,
        owner_id=owner_id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=["backend/app/example.py"],
        authorized_capabilities=["read_file", "patch_file"],
        authorized_risk="low",
        envelope_idempotency_key=f"env-{uuid.uuid4()}",
    )
    authorize_provider_spend(
        db_session,
        owner_id=owner_id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal("1.00"),
        max_requests=3,
        max_cost_per_request_usd=Decimal("0.50"),
        idempotency_key=f"spend-{uuid.uuid4()}",
        allowed_providers=["openai"],
        allowed_models=["gpt-4o-mini"],
    )
    db_session.commit()
    result, attempted = await billed_execution_chat(
        db_session,
        [Message(role="user", content="plan")],
        owner_id=owner_id,
        goal_id=goal.id,
        purpose="mainai_planning",
        requested_by="test",
    )
    assert result.content == "ok"
    assert "openai" in attempted
    events = db_session.query(ProviderSpendUsageEvent).filter(ProviderSpendUsageEvent.goal_id == goal.id).all()
    assert len(events) == 1
    assert events[0].status == ProviderSpendUsageStatus.settled.value


@pytest.mark.asyncio
async def test_billed_chat_exhausted_grant_is_a_failure_path(db_session, owner_id, monkeypatch):
    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", True)

    async def _fake_chat(self, messages, model, **kwargs):
        return ChatResult(content="ok", provider="openai", model=model, raw_usage={"prompt_tokens": 1, "completion_tokens": 1})

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)
    goal = _goal(db_session, owner_id)
    proposal = propose_execution_scope(db_session, owner_id=owner_id, goal_id=goal.id, idempotency_key=f"prop-{uuid.uuid4()}")
    _, envelope = authorize_execution_scope(
        db_session,
        owner_id=owner_id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=["backend/app/example.py"],
        authorized_capabilities=["read_file", "patch_file"],
        authorized_risk="low",
        envelope_idempotency_key=f"env-{uuid.uuid4()}",
    )
    authorize_provider_spend(
        db_session,
        owner_id=owner_id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal("1.00"),
        max_requests=1,
        max_cost_per_request_usd=Decimal("0.50"),
        idempotency_key=f"spend-{uuid.uuid4()}",
        allowed_providers=["openai"],
        allowed_models=["gpt-4o-mini"],
    )
    db_session.commit()
    first, _attempted = await billed_execution_chat(
        db_session,
        [Message(role="user", content="plan")],
        owner_id=owner_id,
        goal_id=goal.id,
        purpose="mainai_planning",
        requested_by="test",
    )
    assert first.content == "ok"
    with pytest.raises(ExecutionSpendError, match="refused"):
        await billed_execution_chat(
            db_session,
            [Message(role="user", content="plan again")],
            owner_id=owner_id,
            goal_id=goal.id,
            purpose="mainai_planning",
            requested_by="test",
        )
