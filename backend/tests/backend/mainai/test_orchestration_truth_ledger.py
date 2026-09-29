"""Orchestration truth ledger: GitHub is software truth, agent text is not.

Covers the real Founder Alpha occupancy pattern: Claude examining, Cursor/Codex idle,
frozen SHA present, builder candidate pushed, validator completed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.integrations.github_client import GitHubClient, GitHubClientError
from app.mainai_orchestration_ledger.assignment import decide_assignment
from app.mainai_orchestration_ledger.claims import (
    apply_github_snapshot,
    bind_test_run,
    examiner_evidence_not_certification,
    ingest_agent_claim,
)
from app.mainai_orchestration_ledger.founder_interrupt import (
    FounderAttentionKind,
    classify_founder_attention,
    sha_relay_is_internal,
)
from app.mainai_orchestration_ledger.github_truth import (
    FakeSoftwareTruthSource,
    GitHubSoftwareTruthSource,
    answers_without_founder_relay,
)
from app.mainai_orchestration_ledger.orchestrate import founder_alpha_regression_world, plan_orchestration
from app.mainai_orchestration_ledger.service import apply_snapshot_to_task_row, persist_claim, persist_github_snapshot
from app.mainai_orchestration_ledger.types import (
    FOUNDER_ALPHA_FINAL_BRANCH,
    FOUNDER_ALPHA_FINAL_SHA,
    GITHUB_BACKED_FIELDS,
    AgentClaim,
    AgentOccupancy,
    AgentRole,
    AssignmentRefusal,
    ClaimKind,
    GitHubCheckRun,
    GitHubPullTruth,
    GitHubTruthSnapshot,
    NextActionKind,
    OccupancyStatus,
    ProposedAssignment,
    TaskRecord,
    TaskStatus,
    TestRunEvidence,
)
from app.models.orchestration_ledger import OrchestrationAgent, OrchestrationClaim, OrchestrationTask
from app.models.user import User

FROZEN = FOUNDER_ALPHA_FINAL_SHA
FROZEN_TREE = "ba9a1616cbc2ee40a56f9e5ac7b07cf9815c1c87"


def _github_frozen(*, local_sha: str | None = FROZEN) -> FakeSoftwareTruthSource:
    return FakeSoftwareTruthSource(
        branches={
            FOUNDER_ALPHA_FINAL_BRANCH: FROZEN,
            "claude/det-kommer-mer-879lcm": "64c07d5b0c4821bfb175700283021aa6e95cb61c",
        },
        trees={FROZEN: FROZEN_TREE},
        check_runs={
            FROZEN: [
                GitHubCheckRun("backend", "completed", "success", FROZEN),
                GitHubCheckRun("frontend", "completed", "success", FROZEN),
            ]
        },
        pull_requests={
            FOUNDER_ALPHA_FINAL_BRANCH: [
                GitHubPullTruth(248, "open", False, FROZEN, FOUNDER_ALPHA_FINAL_BRANCH, "claude/det-kommer-mer-879lcm")
            ]
        },
        default_branch="claude/det-kommer-mer-879lcm",
        default_branch_sha="64c07d5b0c4821bfb175700283021aa6e95cb61c",
    )


def _task(**kwargs) -> TaskRecord:
    defaults = dict(
        task_id="t1",
        title="work",
        owner_id="founder",
        role=AgentRole.BUILDER,
        status=TaskStatus.RUNNING,
        agent_key="codex",
        working_branch="cursor/example",
    )
    defaults.update(kwargs)
    return TaskRecord(**defaults)


def _cursor_lane() -> ProposedAssignment:
    return ProposedAssignment(
        agent_key="cursor",
        title="MainAI orchestration truth ledger",
        role=AgentRole.BUILDER,
        working_branch="cursor/mainai-orchestration-truth-ledger",
        exact_input_sha=FROZEN,
    )


def _codex_lane() -> ProposedAssignment:
    return ProposedAssignment(
        agent_key="codex",
        title="Post-alpha independent builder lane",
        role=AgentRole.BUILDER,
        working_branch="codex/post-alpha-independent-lane",
        exact_input_sha=FROZEN,
    )


def _claude_duplicate() -> ProposedAssignment:
    return ProposedAssignment(
        agent_key="claude",
        title="Founder Alpha independent examination",
        role=AgentRole.EXAMINER,
        working_branch=FOUNDER_ALPHA_FINAL_BRANCH,
        exact_input_sha=FROZEN,
    )


@pytest.mark.asyncio
async def test_github_answers_without_founder_relay():
    snapshot = await _github_frozen().inspect_branch(
        FOUNDER_ALPHA_FINAL_BRANCH, local_sha=FROZEN, expected_sha=FROZEN
    )
    answers = answers_without_founder_relay(snapshot)
    assert answers["branch_exists_remotely"] is True
    assert answers["exact_branch_sha"] == FROZEN
    assert answers["exact_tree_hash"] == FROZEN_TREE
    assert answers["local_remote_match"] is True
    assert answers["ci_all_completed_success"] is True
    assert answers["open_pr"] is True
    assert answers["merged"] is False
    assert answers["default_branch"] == "claude/det-kommer-mer-879lcm"
    assert answers["source"] == "github"
    assert snapshot.ci_binds_to_sha(FROZEN)


@pytest.mark.asyncio
async def test_missing_branch_is_not_exists_not_a_guess():
    source = FakeSoftwareTruthSource(branches={})
    snapshot = await source.inspect_branch("cursor/does-not-exist")
    assert snapshot.exists_remotely is False
    assert snapshot.commit_sha is None
    assert snapshot.local_matches_remote is None


def test_codex_said_pushed_does_not_set_remote_pushed():
    task = _task(remote_pushed=False, remote_sha=None)
    claim = AgentClaim(
        agent_key="codex",
        kind=ClaimKind.REMOTE_PUSHED,
        raw_text="branch pushed",
        claimed_value={"remote_pushed": True},
    )
    result = ingest_agent_claim(task, claim)
    assert result.github_fields_unchanged is True
    assert result.task.remote_pushed is False
    assert result.task.remote_sha is None
    assert result.claim.authoritative is False
    assert "remote_pushed" in GITHUB_BACKED_FIELDS


@pytest.mark.asyncio
async def test_github_confirm_sets_remote_pushed():
    task = _task(remote_pushed=False, working_branch=FOUNDER_ALPHA_FINAL_BRANCH)
    snapshot = await _github_frozen().inspect_branch(FOUNDER_ALPHA_FINAL_BRANCH)
    updated = apply_github_snapshot(task, snapshot)
    assert updated.remote_pushed is True
    assert updated.remote_sha == FROZEN
    assert updated.last_verified_source == "github"


def test_claude_pass_is_evidence_not_certification():
    claim = AgentClaim(
        agent_key="claude",
        kind=ClaimKind.EXAMINER_RESULT,
        raw_text="PASS",
        claimed_value={"result": "PASS"},
    )
    evidence = examiner_evidence_not_certification(claim, FROZEN)
    assert evidence["certified"] is False
    assert evidence["reviewed_sha"] == FROZEN
    assert evidence["claimed_result"] == "PASS"
    assert evidence["source"] == "agent_claim"


def test_cursor_test_claim_is_not_a_bound_test_run():
    task = _task()
    claim = AgentClaim(
        agent_key="cursor",
        kind=ClaimKind.TESTS_PASSED,
        raw_text="2883 tests passed",
        claimed_value={"passed": 2883},
    )
    result = ingest_agent_claim(task, claim)
    assert result.task.test_runs == []
    evidence = TestRunEvidence(
        sha=FROZEN,
        tree_sha=FROZEN_TREE,
        passed=2883,
        failed=0,
        skipped=1,
        command="pytest tests/backend",
        source="pytest_execution",
    )
    bound = bind_test_run(result.task, evidence)
    assert bound.test_runs[0]["sha"] == FROZEN
    assert bound.test_runs[0]["source"] == "pytest_execution"
    assert bound.last_verified_source == "pytest_execution"


def test_test_run_evidence_rejects_agent_text_source():
    with pytest.raises(ValueError, match="pytest_execution"):
        TestRunEvidence(
            sha=FROZEN,
            passed=2883,
            failed=0,
            skipped=1,
            command="said so",
            source="agent_claim",
        )


def test_running_agent_rejects_second_assignment():
    claude = AgentOccupancy("claude", OccupancyStatus.RUNNING, AgentRole.EXAMINER)
    decision = decide_assignment(claude, _claude_duplicate())
    assert decision.allowed is False
    assert decision.refusal is AssignmentRefusal.AGENT_RUNNING


def test_parallel_requires_explicit_new_slot():
    cursor = AgentOccupancy(
        "cursor", OccupancyStatus.RUNNING, AgentRole.BUILDER, supports_parallel_workers=True, max_slots=2, occupied_slots=1
    )
    without_slot = ProposedAssignment(
        agent_key="cursor",
        title="second job",
        role=AgentRole.BUILDER,
        working_branch="cursor/second",
    )
    assert decide_assignment(cursor, without_slot).refusal is AssignmentRefusal.NO_FREE_SLOT
    with_slot = ProposedAssignment(
        agent_key="cursor",
        title="second job",
        role=AgentRole.BUILDER,
        working_branch="cursor/second",
        slot_key="session-2",
        create_new_slot=True,
    )
    allowed = decide_assignment(cursor, with_slot, occupied_slot_keys=("default",))
    assert allowed.allowed is True
    assert allowed.slot_key == "session-2"


def test_builder_cannot_modify_frozen_candidate():
    codex = AgentOccupancy("codex", OccupancyStatus.IDLE, AgentRole.BUILDER)
    proposal = ProposedAssignment(
        agent_key="codex",
        title="tweak founder alpha",
        role=AgentRole.BUILDER,
        working_branch=FOUNDER_ALPHA_FINAL_BRANCH,
        modifies_branch=FOUNDER_ALPHA_FINAL_BRANCH,
        exact_input_sha=FROZEN,
    )
    decision = decide_assignment(codex, proposal)
    assert decision.allowed is False
    assert decision.refusal is AssignmentRefusal.FROZEN_CANDIDATE


@pytest.mark.asyncio
async def test_founder_alpha_regression_does_not_reassign_claude():
    github = await _github_frozen().inspect_branch(FOUNDER_ALPHA_FINAL_BRANCH, local_sha=FROZEN, expected_sha=FROZEN)
    world = founder_alpha_regression_world(
        github=github,
        cursor_proposal=_cursor_lane(),
        codex_proposal=_codex_lane(),
    )
    world.queued_assignments.append(_claude_duplicate())
    plan = plan_orchestration(world)

    wait_claude = [action for action in plan.actions if action.kind is NextActionKind.WAIT_FOR_RUNNING_AGENT and action.agent_key == "claude"]
    assert wait_claude, "Claude is busy — do not duplicate work"

    assigned = [action for action in plan.actions if action.kind is NextActionKind.ASSIGN_INDEPENDENT_LANE]
    assigned_agents = {action.agent_key for action in assigned}
    assert "cursor" in assigned_agents
    assert "codex" in assigned_agents
    assert "claude" not in assigned_agents

    claude_refused = [item for item in plan.refused if item.refusal is AssignmentRefusal.AGENT_RUNNING]
    assert claude_refused

    frozen = [action for action in plan.actions if action.kind is NextActionKind.HOLD_FROZEN_CANDIDATE]
    assert frozen
    assert all(action.assignment is None or action.assignment.working_branch != FOUNDER_ALPHA_FINAL_BRANCH for action in assigned)

    assert not any(action.interrupt_founder for action in plan.actions)
    assert plan.founder_messages == ()
    discover = [action for action in plan.actions if action.kind is NextActionKind.DISCOVER_FROM_GITHUB]
    assert discover
    assert FROZEN in discover[0].reason


def test_examiner_defect_returns_to_builder():
    world = founder_alpha_regression_world(
        github=None,
        cursor_proposal=_cursor_lane(),
        codex_proposal=_codex_lane(),
    )
    for task in world.tasks:
        if task.task_id == "examine-fa":
            task.status = TaskStatus.FAILED
            task.agent_key = "claude"
    world.agents[0].occupancy = OccupancyStatus.IDLE
    world.agents[0].current_task_id = None
    plan = plan_orchestration(world)
    returned = [action for action in plan.actions if action.kind is NextActionKind.RETURN_DEFECT_TO_BUILDER]
    assert returned
    assert returned[0].agent_key == "codex"


def test_builder_finished_requests_independent_examination():
    world = founder_alpha_regression_world(
        github=None,
        cursor_proposal=_cursor_lane(),
        codex_proposal=_codex_lane(),
    )
    world.tasks = [task for task in world.tasks if task.task_id == "build-fa"]
    world.agents[0].occupancy = OccupancyStatus.IDLE
    plan = plan_orchestration(world)
    exams = [action for action in plan.actions if action.kind is NextActionKind.REQUEST_INDEPENDENT_EXAMINATION]
    assert exams
    assert exams[0].agent_key != "codex"


def test_early_stop_without_blocker_continues():
    world = founder_alpha_regression_world(
        github=None,
        cursor_proposal=_cursor_lane(),
        codex_proposal=_codex_lane(),
    )
    world.tasks.append(
        _task(task_id="stopped-early", title="stopped", agent_key="cursor", status=TaskStatus.FAILED, blockers=[])
    )
    plan = plan_orchestration(world)
    assert any(action.kind is NextActionKind.CONTINUE_OR_REASSIGN for action in plan.actions)


def test_founder_interrupt_only_for_real_authority():
    assert classify_founder_attention(FounderAttentionKind.AGENT_MESSAGE_RELAY).interrupt is False
    assert classify_founder_attention(FounderAttentionKind.WAIT_GRAPH).interrupt is False
    assert classify_founder_attention(FounderAttentionKind.NEXT_AGENT).interrupt is False
    assert sha_relay_is_internal().interrupt is False
    assert classify_founder_attention(FounderAttentionKind.SECURITY_POLICY_DECISION).interrupt is True
    assert classify_founder_attention(FounderAttentionKind.MONEY_BUDGET_APPROVAL).interrupt is True
    assert classify_founder_attention(FounderAttentionKind.DESTRUCTIVE_ACTION_APPROVAL).interrupt is True
    assert classify_founder_attention(FounderAttentionKind.GENUINE_BLOCKER_NO_AGENT_CAN_RESOLVE).interrupt is True


@pytest.mark.asyncio
async def test_github_client_missing_branch_is_none(monkeypatch):
    async def _fake_request(self, method, url, *, headers=None, json=None, **kwargs):
        request = httpx.Request(method, url)
        return httpx.Response(404, request=request, json={"message": "Not Found"})

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)
    client = GitHubClient()
    monkeypatch.setattr(client.settings, "github_token", "fake")
    monkeypatch.setattr(client.settings, "github_repo", "d1n095/LifeAI")
    assert await client.get_ref_or_none("no-such-branch") is None
    with pytest.raises(GitHubClientError) as exc:
        await client.get_ref("no-such-branch")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_github_software_truth_source_binds_tree_and_ci(monkeypatch):
    frozen = FROZEN
    responses = {
        ("GET", f"/repos/d1n095/LifeAI/git/ref/heads/{FOUNDER_ALPHA_FINAL_BRANCH}"): {"object": {"sha": frozen}},
        ("GET", f"/repos/d1n095/LifeAI/git/commits/{frozen}"): {"sha": frozen, "tree": {"sha": FROZEN_TREE}},
        ("GET", f"/repos/d1n095/LifeAI/commits/{frozen}/check-runs"): {
            "check_runs": [{"name": "backend", "status": "completed", "conclusion": "success", "head_sha": frozen}]
        },
        ("GET", f"/repos/d1n095/LifeAI/deployments?sha={frozen}"): [],
        ("GET", "/repos/d1n095/LifeAI"): {"default_branch": "claude/det-kommer-mer-879lcm"},
        ("GET", "/repos/d1n095/LifeAI/git/ref/heads/claude/det-kommer-mer-879lcm"): {
            "object": {"sha": "64c07d5b0c4821bfb175700283021aa6e95cb61c"}
        },
        (
            "GET",
            "/repos/d1n095/LifeAI/pulls?head=d1n095:codex/founder-alpha-final-composed-candidate&base=claude/det-kommer-mer-879lcm&state=all",
        ): [{"number": 1, "state": "open", "merged": False, "head": {"sha": frozen, "ref": FOUNDER_ALPHA_FINAL_BRANCH}, "base": {"ref": "claude/det-kommer-mer-879lcm"}}],
    }

    async def _fake_request(self, method, url, *, headers=None, json=None, **kwargs):
        path = str(url).removeprefix("https://api.github.com")
        request = httpx.Request(method, url)
        body = responses.get((method, path))
        if body is None:
            return httpx.Response(404, request=request, json={"message": path})
        return httpx.Response(200, request=request, json=body)

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)
    client = GitHubClient()
    monkeypatch.setattr(client.settings, "github_token", "fake")
    monkeypatch.setattr(client.settings, "github_repo", "d1n095/LifeAI")
    snapshot = await GitHubSoftwareTruthSource(client).inspect_branch(FOUNDER_ALPHA_FINAL_BRANCH, local_sha=frozen)
    assert snapshot.exists_remotely is True
    assert snapshot.commit_sha == frozen
    assert snapshot.tree_sha == FROZEN_TREE
    assert snapshot.local_matches_remote is True
    assert snapshot.ci_all_completed_success is True
    assert snapshot.any_open_pr is True
    assert snapshot.source == "github"


def _owner(db):
    user = User(email=f"orch-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def test_remote_pushed_requires_github_source(superuser_db):
    owner = _owner(superuser_db)
    agent = OrchestrationAgent(
        owner_id=owner.id, agent_key="codex", display_name="Codex", occupancy_status="idle"
    )
    superuser_db.add(agent)
    superuser_db.flush()
    task = OrchestrationTask(
        owner_id=owner.id,
        agent_id=agent.id,
        title="claim-only",
        role="builder",
        status="running",
        remote_pushed=True,
        remote_sha=FROZEN,
        last_verified_source="agent_claim",
    )
    superuser_db.add(task)
    with pytest.raises(IntegrityError):
        superuser_db.flush()
    superuser_db.rollback()


def test_claim_row_cannot_be_authoritative(superuser_db):
    owner = _owner(superuser_db)
    claim = OrchestrationClaim(
        owner_id=owner.id,
        agent_key="codex",
        claim_kind="remote_pushed",
        claimed_value={"remote_pushed": True},
        raw_text="branch pushed",
        authoritative=True,
    )
    superuser_db.add(claim)
    with pytest.raises(IntegrityError):
        superuser_db.flush()
    superuser_db.rollback()


def test_persist_github_snapshot_and_claim(superuser_db):
    owner = _owner(superuser_db)
    agent = OrchestrationAgent(
        owner_id=owner.id, agent_key="codex", display_name="Codex", occupancy_status="idle"
    )
    superuser_db.add(agent)
    superuser_db.flush()
    task = OrchestrationTask(
        owner_id=owner.id,
        agent_id=agent.id,
        title="fa",
        role="builder",
        status="completed",
        exact_input_sha=FROZEN,
        working_branch=FOUNDER_ALPHA_FINAL_BRANCH,
        frozen=True,
        protects_sha=FROZEN,
    )
    superuser_db.add(task)
    superuser_db.flush()
    snapshot = GitHubTruthSnapshot(
        branch=FOUNDER_ALPHA_FINAL_BRANCH,
        exists_remotely=True,
        commit_sha=FROZEN,
        tree_sha=FROZEN_TREE,
        local_sha=FROZEN,
        local_matches_remote=True,
        captured_at=datetime.now(timezone.utc),
        source="github",
    )
    row = persist_github_snapshot(superuser_db, owner_id=owner.id, snapshot=snapshot, task_id=task.id)
    apply_snapshot_to_task_row(task, snapshot)
    superuser_db.flush()
    assert row.source == "github"
    assert task.remote_pushed is True
    assert task.last_verified_source == "github"
    persist_claim(
        superuser_db,
        owner_id=owner.id,
        task=_task(task_id=str(task.id)),
        claim=AgentClaim("codex", ClaimKind.REMOTE_PUSHED, "branch pushed", {"remote_pushed": True}),
    )
    superuser_db.flush()
    stored = superuser_db.query(OrchestrationClaim).filter_by(owner_id=owner.id).one()
    assert stored.authoritative is False
    assert stored.claim_kind == "remote_pushed"


def test_rls_hides_other_owners_orchestration_rows(db_session, superuser_db, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()
    agent_a = OrchestrationAgent(owner_id=user_a.id, agent_key="claude", display_name="Claude")
    agent_b = OrchestrationAgent(owner_id=user_b.id, agent_key="claude", display_name="Claude")
    superuser_db.add_all([agent_a, agent_b])
    superuser_db.commit()

    db_session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user_a.id)})
    visible = db_session.query(OrchestrationAgent).all()
    assert [row.owner_id for row in visible] == [user_a.id]
