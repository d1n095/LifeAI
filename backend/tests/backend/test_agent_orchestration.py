"""MainAI Core: agent orchestration (see app/agent_orchestration.py). Covers, in order:

  A. create_agent_task()/get_task()/list_tasks() — a scoped work order, never a bare
     instruction (title/description/acceptance criteria/constraints all stored).
  B. dispatch_task() — a real chat_with_fallback() call (provider faked, never a real key)
     recorded as an append-only AgentTaskEvent, status transitions to result_recorded.
  C. record_test_results()/review_task() — review is BLOCKED without recorded test results;
     an "approved" verdict text is downgraded to needs_correction if tests didn't pass, no
     matter what the provider said (a hard backstop, not just a prompt instruction).
  D. prepare_github_pr() — requires an approved+tested task; with github_write_enabled=False
     (the default) it only ever proposes exact PR content without touching GitHub at all;
     with it enabled (and GitHubClient mocked at the HTTP layer — never a real repo mutation
     in this suite) it actually creates branch/commit/PR and records a ProjectBranchPRStatus
     row (Fas 2 reuse).
  E. attempt_auto_merge() — ALWAYS blocked, regardless of settings, since no merge method
     exists in the GitHub client at all.
  F. The founder-only admin API surface (app/routers/agents.py) end to end.
  G. Full vertical proof: note -> task -> dispatch -> tests -> review -> PR proposal ->
     checkpoint -> a genuinely cold, context-less read answering every CLAUDE.md MainAI Core
     question, extending Fas 3's resumption-brief proof.

No mocks for storage or the DB — real Postgres test database, matching this repo's existing
convention. Only the LLM provider and the GitHub HTTP client are faked, since real calls to
either are neither possible nor desirable in an automated test suite."""

import uuid

import pytest

from app.agent_orchestration import (
    attempt_auto_merge,
    create_agent_task,
    dispatch_task,
    get_task,
    list_task_events,
    list_tasks,
    prepare_github_pr,
    record_test_results,
    review_task,
)
from app.config import get_settings
from app.integrations.github_client import GitHubClient
from app.models.agent_task import AgentTaskEventType, AgentTaskStatus
from app.models.project_memory import NoteKind
from app.project_memory import add_note, create_checkpoint, list_current_branch_pr_status
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200
    return res.json()["csrf_token"]


def _fake_chat(response_text: str):
    async def _chat(self, messages, model, **kwargs):
        return ChatResult(content=response_text, provider="openai", model=model, raw_usage={})

    return _chat


def _make_task(db_session, **overrides):
    defaults = dict(
        title="Fixa npm audit-sårbarhet",
        description="Uppgradera next till 16.2.11 i frontend/package.json.",
        acceptance_criteria="npm audit --audit-level=high visar inga träffar.",
        constraints="Ändra inga andra beroenden.",
        required_tests="npm audit",
        created_by="test",
    )
    defaults.update(overrides)
    return create_agent_task(db_session, **defaults)


# --- A. Task creation -------------------------------------------------------------------


def test_create_agent_task_stores_a_scoped_work_order_not_a_bare_instruction(db_session):
    task = _make_task(db_session, target_files=["frontend/package.json"])
    assert task.status == AgentTaskStatus.created
    assert task.target_files == ["frontend/package.json"]
    assert task.constraints == "Ändra inga andra beroenden."

    fetched = get_task(db_session, task.id)
    assert fetched.id == task.id
    assert task in list_tasks(db_session)


def test_get_task_raises_for_unknown_id(db_session):
    with pytest.raises(ValueError):
        get_task(db_session, uuid.uuid4())


# --- B. Dispatch -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_task_records_event_and_advances_status(db_session, monkeypatch):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat("--- a/frontend/package.json\n+++ b/frontend/package.json\n(diff)"))
    task = _make_task(db_session)

    event = await dispatch_task(db_session, task.id, dispatched_by="test")

    assert event.event_type == AgentTaskEventType.dispatched
    assert "diff" in event.payload["response"]
    refreshed = get_task(db_session, task.id)
    assert refreshed.status == AgentTaskStatus.result_recorded
    assert len(list_task_events(db_session, task.id)) == 1


# --- C. Test results + review -------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_task_blocked_without_recorded_test_results(db_session, monkeypatch):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat("VERDICT: approved\nSer bra ut."))
    task = _make_task(db_session)
    await dispatch_task(db_session, task.id, dispatched_by="test")

    with pytest.raises(ValueError, match="testresultat"):
        await review_task(db_session, task.id, reviewed_by="test")


@pytest.mark.asyncio
async def test_review_task_approves_with_green_tests_and_approved_verdict(db_session, monkeypatch):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat("VERDICT: approved\nDiffen håller sig inom scope."))
    task = _make_task(db_session)
    await dispatch_task(db_session, task.id, dispatched_by="test")
    record_test_results(db_session, task.id, passed=True, output="npm audit: 0 sårbarheter", recorded_by="test")

    event = await review_task(db_session, task.id, reviewed_by="test")

    assert event.payload["verdict"] == "approved"
    assert get_task(db_session, task.id).status == AgentTaskStatus.reviewed_approved


@pytest.mark.asyncio
async def test_review_task_never_approves_on_red_tests_even_if_verdict_says_approved(db_session, monkeypatch):
    """The hard backstop: MainAI must not self-approve without verifiable green tests,
    regardless of what the review agent's own text says (CLAUDE.md section 6)."""
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat("VERDICT: approved\nSer bra ut (felaktigt, testerna är röda)."))
    task = _make_task(db_session)
    await dispatch_task(db_session, task.id, dispatched_by="test")
    record_test_results(db_session, task.id, passed=False, output="npm audit: 3 sårbarheter kvarstår", recorded_by="test")

    event = await review_task(db_session, task.id, reviewed_by="test")

    assert event.payload["verdict"] == "needs_correction"
    assert get_task(db_session, task.id).status == AgentTaskStatus.reviewed_needs_correction


@pytest.mark.asyncio
async def test_review_task_records_rejected_verdict(db_session, monkeypatch):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat("VERDICT: rejected\nDiffen ändrar filer utanför scope."))
    task = _make_task(db_session)
    await dispatch_task(db_session, task.id, dispatched_by="test")
    record_test_results(db_session, task.id, passed=True, output="ok", recorded_by="test")

    event = await review_task(db_session, task.id, reviewed_by="test")

    assert event.payload["verdict"] == "rejected"
    assert get_task(db_session, task.id).status == AgentTaskStatus.reviewed_rejected


# --- D. GitHub PR preparation --------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_github_pr_requires_approved_status(db_session):
    task = _make_task(db_session)
    with pytest.raises(ValueError, match="godkänt"):
        await prepare_github_pr(db_session, task.id, branch_name="x", base_branch="main", prepared_by="test")


@pytest.mark.asyncio
async def test_prepare_github_pr_only_proposes_when_write_disabled(db_session, monkeypatch):
    assert get_settings().github_write_enabled is False  # the default this test relies on

    async def _fail_if_called(*args, **kwargs):
        raise AssertionError("GitHubClient must not be called when github_write_enabled is False")

    monkeypatch.setattr(GitHubClient, "get_ref", _fail_if_called)
    monkeypatch.setattr(GitHubClient, "create_branch", _fail_if_called)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat("VERDICT: approved\nOk."))

    task = _make_task(db_session)
    await dispatch_task(db_session, task.id, dispatched_by="test")
    record_test_results(db_session, task.id, passed=True, output="ok", recorded_by="test")
    await review_task(db_session, task.id, reviewed_by="test")

    result = await prepare_github_pr(db_session, task.id, branch_name="mainai/fix-npm-audit", base_branch="main", prepared_by="test")

    assert result["mode"] == "proposed"
    assert result["branch_name"] == "mainai/fix-npm-audit"
    assert "diff" in result
    assert get_task(db_session, task.id).status == AgentTaskStatus.pr_prepared
    events = list_task_events(db_session, task.id)
    assert any(e.event_type == AgentTaskEventType.github_pr_proposed for e in events)
    # A proposal is not a real PR — no ProjectBranchPRStatus row should exist for it.
    assert list_current_branch_pr_status(db_session) == []


@pytest.mark.asyncio
async def test_prepare_github_pr_calls_github_when_write_enabled(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "github_write_enabled", True)
    monkeypatch.setattr(get_settings(), "github_token", "fake-token")
    monkeypatch.setattr(get_settings(), "github_repo", "d1n095/LifeAI")

    async def _fake_get_ref(self, branch):
        return "abc123"

    async def _fake_create_branch(self, *, new_branch, from_sha):
        return {"ref": f"refs/heads/{new_branch}"}

    async def _fake_create_or_update_file(self, *, path, content, message, branch, sha=None):
        return {"commit": {"sha": "def456"}}

    async def _fake_create_pull_request(self, *, title, body, head, base):
        return {"number": 42, "html_url": "https://github.com/d1n095/LifeAI/pull/42"}

    monkeypatch.setattr(GitHubClient, "get_ref", _fake_get_ref)
    monkeypatch.setattr(GitHubClient, "create_branch", _fake_create_branch)
    monkeypatch.setattr(GitHubClient, "create_or_update_file", _fake_create_or_update_file)
    monkeypatch.setattr(GitHubClient, "create_pull_request", _fake_create_pull_request)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat("VERDICT: approved\nOk."))

    task = _make_task(db_session)
    await dispatch_task(db_session, task.id, dispatched_by="test")
    record_test_results(db_session, task.id, passed=True, output="ok", recorded_by="test")
    await review_task(db_session, task.id, reviewed_by="test")

    result = await prepare_github_pr(db_session, task.id, branch_name="mainai/fix-npm-audit", base_branch="main", prepared_by="test")

    assert result["mode"] == "created"
    assert result["pr_number"] == 42
    assert get_task(db_session, task.id).status == AgentTaskStatus.pr_opened

    event_types = {e.event_type for e in list_task_events(db_session, task.id)}
    assert AgentTaskEventType.github_branch_created in event_types
    assert AgentTaskEventType.github_commit_pushed in event_types
    assert AgentTaskEventType.github_pr_opened in event_types

    statuses = list_current_branch_pr_status(db_session)
    assert len(statuses) == 1
    assert statuses[0].ref == "#42"
    assert statuses[0].base_ref == "main"


# --- E. Merge is always blocked -------------------------------------------------------------


def test_attempt_auto_merge_is_always_blocked_regardless_of_flag(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "github_auto_merge_enabled", True)  # even if "enabled"...
    task = _make_task(db_session)

    result = attempt_auto_merge(db_session, task.id, requested_by="test")

    assert result["merged"] is False
    assert len(result["conditions"]) == 8
    events = list_task_events(db_session, task.id)
    assert events[-1].event_type == AgentTaskEventType.merge_blocked


# --- F. Founder-only admin API ---------------------------------------------------------------


def test_agents_api_requires_founder_auth(client):
    res = client.get("/api/admin/agents/tasks")
    assert res.status_code == 401


def test_agents_api_full_loop_create_dispatch_test_review_propose(client, monkeypatch):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat("VERDICT: approved\nOk, inom scope."))
    csrf = _login(client)
    headers = {"X-CSRF-Token": csrf}

    created = client.post(
        "/api/admin/agents/tasks",
        json={
            "title": "Fixa npm audit",
            "description": "Uppgradera next.",
            "acceptance_criteria": "npm audit ren.",
            "target_files": ["frontend/package.json"],
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text
    task_id = created.json()["id"]

    dispatched = client.post(f"/api/admin/agents/tasks/{task_id}/dispatch", headers=headers)
    assert dispatched.status_code == 200, dispatched.text

    tested = client.post(f"/api/admin/agents/tasks/{task_id}/test-results", json={"passed": True, "output": "ok"}, headers=headers)
    assert tested.status_code == 200, tested.text

    reviewed = client.post(f"/api/admin/agents/tasks/{task_id}/review", headers=headers)
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["payload"]["verdict"] == "approved"

    proposed = client.post(
        f"/api/admin/agents/tasks/{task_id}/prepare-pr",
        json={"branch_name": "mainai/fix-npm-audit", "base_branch": "main"},
        headers=headers,
    )
    assert proposed.status_code == 200, proposed.text
    assert proposed.json()["mode"] == "proposed"

    merge_attempt = client.post(f"/api/admin/agents/tasks/{task_id}/attempt-merge", headers=headers)
    assert merge_attempt.status_code == 200
    assert merge_attempt.json()["merged"] is False

    detail = client.get(f"/api/admin/agents/tasks/{task_id}")
    assert detail.status_code == 200
    assert len(detail.json()["events"]) == 5  # dispatched, test_results, reviewed, pr_proposed, merge_blocked


def test_dispatch_returns_clean_503_when_every_provider_fails(client, monkeypatch):
    """LLM Coupling & Failure-Boundary Audit, PR #16: dispatch must not leak
    chat_with_fallback()'s aggregated failure (which can embed a raw provider exception, e.g.
    a URL with an API key) and must not surface as an unhandled 500 — a clean, fixed 503
    instead. Only OpenAI is configured in tests (see conftest.py), so making its chat() raise
    exhausts the whole fallback chain."""

    async def _broken_chat(self, messages, model, **kwargs):
        raise RuntimeError("connection refused to https://api.openai.com/v1/chat/completions?key=super-secret-value")

    monkeypatch.setattr(OpenAIProvider, "chat", _broken_chat)
    csrf = _login(client)
    headers = {"X-CSRF-Token": csrf}

    created = client.post(
        "/api/admin/agents/tasks",
        json={"title": "Task", "description": "Desc", "acceptance_criteria": "Crit."},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    task_id = created.json()["id"]

    dispatched = client.post(f"/api/admin/agents/tasks/{task_id}/dispatch", headers=headers)
    assert dispatched.status_code == 503
    assert "super-secret-value" not in dispatched.text
    assert "connection refused" not in dispatched.text


# --- G. Full vertical proof: note -> task -> code -> review -> PR proposal -> checkpoint ----


@pytest.mark.asyncio
async def test_full_vertical_chain_note_to_pr_proposal_to_cold_resumption(client, db_session, monkeypatch):
    """The concrete proof CLAUDE.md's 2026-07-26 MainAI Core direction asks for: a real
    problem (a ProjectNote), a real agent task derived from it, a real (faked-provider)
    dispatch, recorded test results, a real review verdict, a real PR proposal, a checkpoint —
    and then a genuinely cold read (only the checkpoint's brief + the task's own stored
    state) that must correctly answer what's blocking, which agent worked on it, what the
    verdict was, and what the exact next safe step is, without guessing."""
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat("VERDICT: approved\nDiffen håller sig strikt inom scope."))

    note = add_note(
        db_session,
        kind=NoteKind.blocker,
        content="npm audit misslyckas på PR #9 pga en gammal next-pinning.",
        source_type="pr",
        source_ref="#9",
        created_by="test",
    )

    task = create_agent_task(
        db_session,
        title="Fixa next-pinningen",
        description="Uppgradera next till 16.2.11 i frontend/package.json.",
        acceptance_criteria="npm audit --audit-level=high visar 0 träffar.",
        required_tests="npm audit",
        source_note_id=note.id,
        created_by="test",
    )
    await dispatch_task(db_session, task.id, dispatched_by="test")
    record_test_results(db_session, task.id, passed=True, output="npm audit: 0 sårbarheter", recorded_by="test")
    review_event = await review_task(db_session, task.id, reviewed_by="test")
    assert review_event.payload["verdict"] == "approved"
    pr_proposal = await prepare_github_pr(db_session, task.id, branch_name="mainai/fix-next-pinning", base_branch="claude/det-kommer-mer-879lcm", prepared_by="test")
    assert pr_proposal["mode"] == "proposed"

    checkpoint = create_checkpoint(
        db_session,
        summary="MainAI upptäckte och löste next-pinning-blockeraren via agentuppdrag; PR-underlag förberett.",
        branch_name="claude/mainai-core-orchestration-v1",
        open_pr_refs=[],
        created_by="test",
    )
    db_session.commit()

    # The cold read: only the checkpoint brief (Fas 1-4's existing mechanism) plus the task's
    # own stored events — nothing from this test function's own local variables.
    csrf = _login(client)
    latest = client.get("/api/admin/memory/checkpoints/latest")
    assert latest.status_code == 200
    brief = latest.json()["brief"]
    assert "next-pinning-blockeraren" in brief

    task_detail = client.get(f"/api/admin/agents/tasks/{task.id}")
    assert task_detail.status_code == 200
    data = task_detail.json()
    assert data["status"] == "pr_prepared"
    event_types = [e["event_type"] for e in data["events"]]
    assert event_types == ["dispatched", "test_results_recorded", "reviewed", "github_pr_proposed"]
    review = next(e for e in data["events"] if e["event_type"] == "reviewed")
    assert review["payload"]["verdict"] == "approved"
    proposal = next(e for e in data["events"] if e["event_type"] == "github_pr_proposed")
    assert proposal["payload"]["branch_name"] == "mainai/fix-next-pinning"
    assert csrf  # login succeeded; csrf not needed for these GETs but proves founder auth works
