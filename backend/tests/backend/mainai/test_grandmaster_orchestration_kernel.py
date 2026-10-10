from __future__ import annotations

import threading
import uuid
import multiprocessing
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.mainai_grandmaster.kernel import GrandmasterKernel
from app.mainai_grandmaster.coordination import SqliteCoordinationStore
from app.mainai_grandmaster.planner import GrandmasterPlanner
from app.mainai_grandmaster.types import (
    Agent,
    Board,
    BoardStatus,
    Branch,
    CandidateSHA,
    Contingency,
    Dependency,
    EvidencePointer,
    Execution,
    Examination,
    ProposedMove,
    Task,
    Workspace,
    WorkspaceMode,
    FixedClock,
)


NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
SHA_A = "a" * 40
SHA_B = "b" * 40


def evidence(
    source="task_ledger", *, at=NOW, authoritative=True, mutable=True, payload=None
):
    return EvidencePointer(
        source=source,
        source_ref=f"{source}:1",
        observed_at=at,
        authoritative=authoritative,
        mutable=mutable,
        expires_at=at + timedelta(days=1) if mutable else None,
        payload=payload or {},
    )


def agent(
    key, status=BoardStatus.IDLE, roles=("builder",), capabilities=("repo_edit",)
):
    return Agent(
        key,
        frozenset(roles),
        frozenset(capabilities),
        status,
        evidence("execution_ledger"),
    )


def task(
    key,
    *,
    status=BoardStatus.IDLE,
    role="builder",
    deps=(),
    migrations=(),
    artifact=None,
):
    return Task(
        key,
        key,
        role,
        frozenset({"repo_edit"}),
        status,
        frozenset(deps),
        frozenset(migrations),
        artifact,
        evidence("task_ledger"),
    )


def execution(
    key,
    task_key,
    agent_key,
    *,
    status=BoardStatus.IDLE,
    workspace=None,
    branch=None,
    sha=None,
    at=NOW,
):
    return Execution(
        key,
        task_key,
        agent_key,
        status,
        workspace,
        branch,
        sha,
        evidence("execution_ledger", at=at),
    )


def workspace(
    key,
    *,
    owner=None,
    execution_id=None,
    branch=None,
    mode=WorkspaceMode.MUTABLE,
    base=SHA_A,
):
    return Workspace(
        key,
        f"/worktrees/{key}",
        mode,
        owner,
        execution_id,
        branch,
        base,
        evidence("filesystem"),
    )


def move(
    key,
    task_key,
    agent_key,
    workspace_key,
    *,
    branch=None,
    base=SHA_B,
    child=None,
    action="build",
    **scores,
):
    return ProposedMove(
        key,
        task_key,
        agent_key,
        key,
        workspace_key,
        branch or f"{agent_key}/{task_key}",
        base,
        action=action,
        child_of_sha=child,
        **scores,
    )


def ready_board() -> Board:
    b = Board(refreshed_at=NOW)
    b.agents = {key: agent(key) for key in ("claude", "cursor", "codex")}
    b.tasks = {key: task(key) for key in ("examine-x", "safe-a", "safe-b", "security")}
    b.executions = {
        "exec-exam": execution(
            "exec-exam",
            "examine-x",
            "claude",
            status=BoardStatus.RUNNING,
            workspace="exam-ws",
            branch="review/a",
            sha=SHA_A,
        ),
        "move-a": execution("move-a", "safe-a", "cursor"),
        "move-b": execution("move-b", "safe-b", "codex"),
        "move-sec": execution("move-sec", "security", "codex"),
    }
    b.agents["claude"] = replace(b.agents["claude"], status=BoardStatus.RUNNING)
    b.workspaces = {
        "exam-ws": workspace(
            "exam-ws",
            owner="claude",
            execution_id="exec-exam",
            branch="review/a",
            mode=WorkspaceMode.READ_ONLY,
        ),
        "ws-a": workspace("ws-a"),
        "ws-b": workspace("ws-b"),
        "ws-sec": workspace("ws-sec"),
        "child-ws": workspace("child-ws"),
    }
    b.candidates[SHA_A] = CandidateSHA(
        SHA_A,
        "examine-x",
        "feature/a",
        BoardStatus.UNDER_EXAMINATION,
        True,
        evidence("github", mutable=False),
    )
    b.examinations["exam-1"] = Examination(
        "exam-1",
        SHA_A,
        "claude",
        "exam-ws",
        BoardStatus.UNDER_EXAMINATION,
        None,
        evidence("verification_registry"),
    )
    return b


def kernel_with(board=None, *, store=True):
    coordination = (
        SqliteCoordinationStore(f"/tmp/grandmaster-test-{uuid.uuid4()}.sqlite3")
        if store
        else None
    )
    k = GrandmasterKernel(
        board or ready_board(), clock=FixedClock(NOW), coordination=coordination
    )
    return k


def verdict_evidence(
    examination, verdict, *, source="verification_registry", examiner=None
):
    return evidence(
        source,
        at=NOW + timedelta(minutes=1),
        payload={
            "examination_id": examination.examination_id,
            "candidate_sha": examination.candidate_sha,
            "examiner_agent": examiner or examination.examiner_agent,
            "verdict": verdict,
        },
    )


def _process_lease_attempt(store_path, suffix, start, output):
    candidate = ProposedMove(
        f"process-{suffix}",
        f"task-{suffix}",
        f"agent-{suffix}",
        f"exec-{suffix}",
        f"workspace-{suffix}",
        "branch/process",
        SHA_A,
    )
    start.wait()
    lease = SqliteCoordinationStore(store_path).acquire(
        candidate, "/tmp/shared-grandmaster-process-worktree", timedelta(minutes=5), NOW
    )
    output.put(lease is not None)


def test_a_busy_examiner_does_not_idle_safe_capacity():
    k = kernel_with()
    decisions = GrandmasterPlanner(k).plan(
        [
            move("move-a", "safe-a", "cursor", "ws-a", critical_path_impact=5),
            move("move-b", "safe-b", "codex", "ws-b", expected_value=5),
        ]
    )
    assert {d.move.agent_key for d in decisions if d.legal} == {"cursor", "codex"}


def test_b_running_exact_task_is_not_duplicated():
    b = ready_board()
    b.tasks["safe-a"] = replace(b.tasks["safe-a"], status=BoardStatus.RUNNING)
    b.executions["existing"] = execution(
        "existing", "safe-a", "claude", status=BoardStatus.RUNNING
    )
    result = kernel_with(b).validate_move(move("move-a", "safe-a", "cursor", "ws-a"))
    assert not result.legal
    assert "duplicate_active_task" in result.reasons


def test_started_execution_is_not_reissued():
    b = ready_board()
    b.executions["move-a"] = execution(
        "move-a", "safe-a", "cursor", status=BoardStatus.RUNNING
    )
    result = kernel_with(b).validate_move(
        move("move-a", "safe-a", "cursor", "ws-a"), now=NOW
    )
    assert "execution_already_started" in result.reasons


def test_existing_implementation_is_not_blindly_rebuilt():
    b = ready_board()
    b.tasks["safe-a"] = replace(
        b.tasks["safe-a"], status=BoardStatus.COMPLETE_UNVERIFIED
    )
    result = kernel_with(b).validate_move(
        move("move-a", "safe-a", "cursor", "ws-a"), now=NOW
    )
    assert "existing_implementation_requires_state_check" in result.reasons


def test_c_builder_denied_examiner_workspace():
    b = ready_board()
    b.executions["move-a"] = execution("move-a", "safe-a", "cursor")
    result = kernel_with(b).validate_move(
        move("move-a", "safe-a", "cursor", "exam-ws", child=SHA_A)
    )
    assert "examiner_workspace_is_read_only_for_builder" in result.reasons


def test_d_builder_child_workspace_from_examined_sha_is_allowed():
    b = ready_board()
    b.tasks["safe-a"] = replace(b.tasks["safe-a"], dependencies=frozenset())
    result = kernel_with(b).validate_move(
        move("move-a", "safe-a", "cursor", "child-ws", base=SHA_A, child=SHA_A)
    )
    assert result.legal


def test_e_agent_push_report_does_not_override_github_remote_sha():
    b = ready_board()
    b.branches["cursor/safe-a"] = Branch(
        "cursor/safe-a", SHA_B, SHA_A, "cursor", evidence("github")
    )
    k = kernel_with(b)
    k.ingest_reports(
        "msg",
        [
            {
                "agent_key": "cursor",
                "task_key": "safe-a",
                "execution_id": "move-a",
                "branch": "cursor/safe-a",
                "sha": SHA_B,
                "result_type": "BUILDER_REPORT",
                "status": "COMPLETE_UNVERIFIED",
            }
        ],
    )
    assert k.remote_pushed("cursor/safe-a", SHA_B) is False


def test_f_multiple_reports_create_distinct_events():
    k = kernel_with()
    reports = [
        {
            "agent_key": who,
            "task_key": f"task-{who}",
            "execution_id": f"exec-{who}",
            "result_type": kind,
            "status": status,
        }
        for who, kind, status in (
            ("cursor", "BUILDER_REPORT", "COMPLETE_UNVERIFIED"),
            ("claude", "EXAMINER_REPORT", "CERTIFIED"),
            ("codex", "BUILDER_REPORT", "RUNNING"),
        )
    ]
    events = k.ingest_reports("founder-1", reports)
    assert len(events) == 3
    assert len({e.event_id for e in events}) == 3
    assert {e.agent_key for e in events} == {"cursor", "claude", "codex"}


def test_g_fresh_live_execution_beats_old_idle_report():
    b = ready_board()
    k = kernel_with(b)
    k.ingest_reports(
        "old",
        [
            {
                "agent_key": "cursor",
                "task_key": "safe-a",
                "execution_id": "move-a",
                "result_type": "BUILDER_REPORT",
                "status": "IDLE",
            }
        ],
        received_at=NOW - timedelta(days=2),
    )
    k.ingest_machine_execution(
        execution(
            "move-a",
            "safe-a",
            "cursor",
            status=BoardStatus.RUNNING,
            at=NOW + timedelta(minutes=1),
        )
    )
    assert k.board.executions["move-a"].status == BoardStatus.RUNNING


def test_h_blocked_exam_dependency_still_allows_independent_security_task():
    b = ready_board()
    b.tasks["safe-a"] = task("safe-a", deps=("examine-x",))
    b.dependencies.append(
        Dependency("safe-a", "examine-x", False, evidence("task_ledger"))
    )
    k = kernel_with(b)
    blocked = k.validate_move(move("move-a", "safe-a", "cursor", "ws-a"))
    independent = k.validate_move(move("move-sec", "security", "codex", "ws-sec"))
    assert not blocked.legal and independent.legal


def test_i_migration_sibling_collision_detected():
    b = ready_board()
    b.tasks["safe-a"] = task("safe-a", migrations=("alembic-head",))
    b.tasks["safe-b"] = task("safe-b", migrations=("alembic-head",))
    b.executions["move-b"] = execution(
        "move-b", "safe-b", "codex", status=BoardStatus.RUNNING
    )
    b.agents["codex"] = replace(b.agents["codex"], status=BoardStatus.RUNNING)
    result = kernel_with(b).validate_move(move("move-a", "safe-a", "cursor", "ws-a"))
    assert "migration_namespace_collision" in result.reasons


def test_j_unknown_occupancy_fails_closed():
    b = ready_board()
    del b.agents["cursor"]
    result = kernel_with(b).validate_move(move("move-a", "safe-a", "cursor", "ws-a"))
    assert result.outcome == "NEEDS_STATE_REFRESH"
    assert "unknown_agent" in result.reasons


def test_k_return_to_builder_requires_new_child_and_workspace():
    b = ready_board()
    exam = b.examinations["exam-1"]
    b.examinations["exam-1"] = replace(
        exam,
        status=BoardStatus.COMPLETE_UNVERIFIED,
        verdict="RETURN_TO_BUILDER",
        evidence=verdict_evidence(exam, "RETURN_TO_BUILDER"),
    )
    planner = GrandmasterPlanner(kernel_with(b))
    bad = planner.next_builder_child(
        "exam-1", move("move-a", "safe-a", "cursor", "exam-ws", base=SHA_A, child=SHA_A)
    )
    good = planner.next_builder_child(
        "exam-1",
        move("move-a", "safe-a", "cursor", "child-ws", base=SHA_A, child=SHA_A),
    )
    assert not bad.legal and good.legal


def test_l_freeze_certifies_but_does_not_merge_or_deploy():
    b = ready_board()
    b.tasks["examine-x"] = task("examine-x", role="examiner")
    # Builder and examiner are distinct.
    b.executions["builder-x"] = execution(
        "builder-x", "examine-x", "cursor", status=BoardStatus.COMPLETE_UNVERIFIED
    )
    k = kernel_with(b)
    k.record_examiner_verdict(
        "exam-1", "FREEZE_SHA", verdict_evidence(b.examinations["exam-1"], "FREEZE_SHA")
    )
    candidate = k.board.candidates[SHA_A]
    assert candidate.status == BoardStatus.CERTIFIED and candidate.frozen
    assert "merged" not in candidate.__dict__ and "deployed" not in candidate.__dict__


def test_m_machine_event_advances_without_founder_relay():
    k = kernel_with()
    k.ingest_machine_execution(
        execution(
            "move-a",
            "safe-a",
            "cursor",
            status=BoardStatus.COMPLETE_UNVERIFIED,
            at=NOW + timedelta(minutes=1),
        )
    )
    assert k.board.executions["move-a"].status == BoardStatus.COMPLETE_UNVERIFIED


def test_authoritative_source_adapter_refreshes_board_without_message_relay():
    snapshot = ready_board()

    class LedgerSource:
        def snapshot(self):
            return snapshot

    board = GrandmasterKernel().refresh_from_sources([LedgerSource()], now=NOW)
    assert board.executions["exec-exam"].status == BoardStatus.RUNNING


def test_two_agents_racing_for_same_mutable_workspace_have_one_winner():
    b = ready_board()
    b.executions["race-a"] = execution("race-a", "safe-a", "cursor")
    b.executions["race-b"] = execution("race-b", "safe-b", "codex")
    moves = [
        move("race-a", "safe-a", "cursor", "ws-a"),
        move("race-b", "safe-b", "codex", "ws-a"),
    ]
    k = kernel_with(b)
    barrier = threading.Barrier(2)
    results = []

    def acquire(candidate):
        barrier.wait()
        results.append(k.acquire_lease(candidate, ttl=timedelta(minutes=5), now=NOW))

    threads = [
        threading.Thread(target=acquire, args=(candidate,)) for candidate in moves
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(result is not None for result in results) == 1


def test_lease_heartbeat_expiry_and_release_are_execution_bound():
    k = kernel_with()
    candidate = move("move-a", "safe-a", "cursor", "ws-a")
    lease = k.acquire_lease(candidate, ttl=timedelta(minutes=1), now=NOW)
    assert lease is not None
    assert (
        k.heartbeat_lease(
            "ws-a",
            agent_key="codex",
            execution_id="move-a",
            fencing_token=lease.fencing_token,
            ttl=timedelta(minutes=2),
            now=NOW,
        )
        is None
    )
    assert (
        k.heartbeat_lease(
            "ws-a",
            agent_key="cursor",
            execution_id="move-a",
            fencing_token=lease.fencing_token,
            ttl=timedelta(minutes=2),
            now=NOW,
        )
        is not None
    )
    assert (
        k.release_lease(
            "ws-a",
            agent_key="cursor",
            execution_id="wrong",
            fencing_token=lease.fencing_token,
            now=NOW,
        )
        is False
    )
    assert k.expire_leases(now=NOW + timedelta(minutes=3)) == 1


def test_frozen_sha_cannot_be_mutated_without_child_binding():
    result = kernel_with().validate_move(
        move("move-a", "safe-a", "cursor", "child-ws", base=SHA_A)
    )
    assert "frozen_candidate_requires_new_child" in result.reasons


def test_branch_owned_by_another_agent_is_rejected():
    b = ready_board()
    b.branches["shared"] = Branch("shared", SHA_B, SHA_B, "codex", evidence("github"))
    result = kernel_with(b).validate_move(
        move("move-a", "safe-a", "cursor", "ws-a", branch="shared"), now=NOW
    )
    assert "branch_owned_by_other_agent" in result.reasons


def test_builder_self_certification_rejected():
    b = ready_board()
    b.examinations["exam-1"] = replace(
        b.examinations["exam-1"], examiner_agent="cursor"
    )
    b.executions["builder-x"] = execution(
        "builder-x", "examine-x", "cursor", status=BoardStatus.COMPLETE_UNVERIFIED
    )
    k = kernel_with(b)
    with pytest.raises(ValueError, match="cannot certify"):
        k.record_examiner_verdict(
            "exam-1",
            "FREEZE_SHA",
            verdict_evidence(b.examinations["exam-1"], "FREEZE_SHA", examiner="cursor"),
        )


@pytest.mark.parametrize(
    "action",
    [
        "merge",
        "deploy",
        "activate_recall",
        "spend",
        "purchase",
        "override_security",
        "mutate_founder_policy",
    ],
)
def test_planning_never_escalates_authority(action):
    result = kernel_with().validate_move(
        move("move-a", "safe-a", "cursor", "ws-a", action=action)
    )
    assert "action_requires_external_governed_authority" in result.reasons


def test_github_sha_is_discovered_without_founder_relay():
    b = ready_board()
    b.branches["cursor/safe-a"] = Branch(
        "cursor/safe-a", SHA_A, SHA_B, "cursor", evidence("github")
    )
    assert kernel_with(b).discover_sha(branch="cursor/safe-a") == SHA_B


def test_requested_artifact_does_not_return_adjacent_task_sha():
    b = ready_board()
    b.candidates[SHA_B] = CandidateSHA(
        SHA_B,
        "security",
        "codex/security",
        BoardStatus.COMPLETE_UNVERIFIED,
        False,
        evidence("github", at=NOW + timedelta(minutes=1), mutable=False),
    )
    k = kernel_with(b)
    assert k.discover_sha(task_key="examine-x") == SHA_A
    assert k.discover_sha(task_key="missing") is None


def test_refresh_rejects_stale_chat_and_uses_live_authority():
    old = Board()
    old.agents["cursor"] = Agent(
        "cursor",
        frozenset({"builder"}),
        frozenset({"repo_edit"}),
        BoardStatus.IDLE,
        evidence("chat", at=NOW - timedelta(days=1), authoritative=False),
    )
    live = Board()
    live.agents["cursor"] = agent("cursor", BoardStatus.RUNNING)
    board = GrandmasterKernel().refresh([old, live], now=NOW)
    assert board.agents["cursor"].status == BoardStatus.RUNNING


def test_stale_authoritative_mutable_state_is_unknown_after_refresh():
    stale = Board()
    stale.agents["cursor"] = Agent(
        "cursor",
        frozenset({"builder"}),
        frozenset({"repo_edit"}),
        BoardStatus.IDLE,
        EvidencePointer(
            "ledger",
            "old",
            NOW - timedelta(days=1),
            True,
            True,
            NOW - timedelta(hours=1),
        ),
    )
    assert "cursor" not in GrandmasterKernel().refresh([stale], now=NOW).agents


def test_dependency_unknown_blocks_assignment():
    b = ready_board()
    b.tasks["safe-a"] = task("safe-a", deps=("missing",))
    result = kernel_with(b).validate_move(move("move-a", "safe-a", "cursor", "ws-a"))
    assert "dependency_not_ready_or_unknown" in result.reasons


def test_role_policy_is_configuration_not_provider_name():
    b = ready_board()
    b.agents["reviewer-42"] = agent(
        "reviewer-42", roles=("examiner",), capabilities=("repo_edit",)
    )
    b.tasks["review"] = task("review", role="examiner")
    b.executions["review-exec"] = execution("review-exec", "review", "reviewer-42")
    b.workspaces["review-ws"] = workspace("review-ws")
    result = kernel_with(b).validate_move(
        move("review-exec", "review", "reviewer-42", "review-ws")
    )
    assert result.legal


def test_contingency_resolution_is_validated_not_auto_executed():
    k = kernel_with()
    candidate = move("move-a", "safe-a", "cursor", "ws-a")
    result = GrandmasterPlanner(k).resolve_contingency(
        Contingency("safe-a", on_agent_idle=candidate), "AGENT_IDLE"
    )
    assert result.legal
    assert "ws-a" not in k.board.leases


def test_multi_move_ranking_prefers_critical_path_then_value():
    k = kernel_with()
    decisions = GrandmasterPlanner(k).plan(
        [
            move(
                "move-a",
                "safe-a",
                "cursor",
                "ws-a",
                expected_value=10,
                critical_path_impact=1,
            ),
            move(
                "move-b",
                "safe-b",
                "codex",
                "ws-b",
                expected_value=1,
                critical_path_impact=10,
            ),
        ]
    )
    assert [d.move.move_id for d in decisions if d.legal] == ["move-b", "move-a"]


def test_clock_is_injected_and_not_wall_clock_dependent():
    k = kernel_with()
    assert k.validate_move(move("move-a", "safe-a", "cursor", "ws-a")).legal
    assert k.remote_pushed("missing", SHA_A) is False


def test_one_active_durable_lease_per_agent():
    b = ready_board()
    path = f"/tmp/grandmaster-agent-{uuid.uuid4()}.sqlite3"
    store = SqliteCoordinationStore(path)
    first = GrandmasterKernel(b, clock=FixedClock(NOW), coordination=store)
    assert first.acquire_lease(
        move("move-a", "safe-a", "cursor", "ws-a"), ttl=timedelta(minutes=5)
    )
    b.executions["agent-second"] = execution("agent-second", "safe-b", "cursor")
    b.workspaces["agent-second-ws"] = workspace("agent-second-ws")
    second = GrandmasterKernel(
        b, clock=FixedClock(NOW), coordination=SqliteCoordinationStore(path)
    )
    candidate = move("agent-second", "safe-b", "cursor", "agent-second-ws")
    assert "agent_active_lease_collision" in second.validate_move(candidate).reasons
    assert second.acquire_lease(candidate, ttl=timedelta(minutes=5)) is None


def test_one_active_durable_lease_per_task():
    b = ready_board()
    path = f"/tmp/grandmaster-task-{uuid.uuid4()}.sqlite3"
    first = GrandmasterKernel(
        b, clock=FixedClock(NOW), coordination=SqliteCoordinationStore(path)
    )
    assert first.acquire_lease(
        move("move-a", "safe-a", "cursor", "ws-a"), ttl=timedelta(minutes=5)
    )
    b.executions["task-second"] = execution("task-second", "safe-a", "codex")
    b.workspaces["task-second-ws"] = workspace("task-second-ws")
    second = GrandmasterKernel(
        b, clock=FixedClock(NOW), coordination=SqliteCoordinationStore(path)
    )
    candidate = move("task-second", "safe-a", "codex", "task-second-ws")
    assert "task_active_lease_collision" in second.validate_move(candidate).reasons


def test_workspace_aliases_resolve_to_same_physical_path():
    b = ready_board()
    b.workspaces["alias"] = replace(
        workspace("alias", owner="codex", execution_id="move-b", branch="codex/safe-b"),
        path="/tmp/grandmaster/../grandmaster/ws-a",
    )
    b.workspaces["ws-a"] = replace(b.workspaces["ws-a"], path="/tmp/grandmaster/ws-a")
    result = kernel_with(b).validate_move(move("move-a", "safe-a", "cursor", "ws-a"))
    assert "physical_workspace_path_collision" in result.reasons


def test_reserved_examination_and_frozen_branch_are_immutable():
    b = ready_board()
    b.examinations["exam-1"] = replace(
        b.examinations["exam-1"], status=BoardStatus.RESERVED
    )
    by_workspace = kernel_with(b).validate_move(
        move("move-a", "safe-a", "cursor", "exam-ws", base=SHA_A, child=SHA_A)
    )
    by_branch = kernel_with(b).validate_move(
        move(
            "move-a",
            "safe-a",
            "cursor",
            "child-ws",
            branch="feature/a",
            base=SHA_A,
            child=SHA_A,
        )
    )
    assert "examiner_workspace_is_read_only_for_builder" in by_workspace.reasons
    assert "frozen_candidate_branch_is_immutable" in by_branch.reasons


def test_return_to_builder_is_enforced_by_kernel_not_only_planner():
    b = ready_board()
    exam = b.examinations["exam-1"]
    b.examinations["exam-1"] = replace(
        exam,
        status=BoardStatus.COMPLETE_UNVERIFIED,
        verdict="RETURN_TO_BUILDER",
        evidence=verdict_evidence(exam, "RETURN_TO_BUILDER"),
    )
    same_branch = move(
        "move-a",
        "safe-a",
        "cursor",
        "child-ws",
        branch="feature/a",
        base=SHA_A,
        child=SHA_A,
    )
    assert (
        "return_to_builder_requires_new_child_branch_workspace"
        in kernel_with(b).validate_move(same_branch).reasons
    )


@pytest.mark.parametrize(
    "action",
    [
        "release",
        "ship",
        "credential_grant",
        "grant_admin",
        "recall_on",
        "production_push",
        "unknown",
    ],
)
def test_autonomous_actions_are_safe_allowlisted(action):
    result = kernel_with().validate_move(
        move("move-a", "safe-a", "cursor", "ws-a", action=action)
    )
    assert "action_requires_external_governed_authority" in result.reasons


def test_missing_durable_coordination_fails_closed():
    k = kernel_with(store=False)
    assert k.validate_move(move("move-a", "safe-a", "cursor", "ws-a")).legal
    assert (
        k.acquire_lease(
            move("move-a", "safe-a", "cursor", "ws-a"), ttl=timedelta(minutes=1)
        )
        is None
    )


def test_durable_lease_survives_restart_and_stale_fence_cannot_mutate():
    path = f"/tmp/grandmaster-restart-{uuid.uuid4()}.sqlite3"
    first = GrandmasterKernel(
        ready_board(), clock=FixedClock(NOW), coordination=SqliteCoordinationStore(path)
    )
    candidate = move("move-a", "safe-a", "cursor", "ws-a")
    old = first.acquire_lease(candidate, ttl=timedelta(minutes=1))
    assert old is not None
    restarted = GrandmasterKernel(
        ready_board(), clock=FixedClock(NOW), coordination=SqliteCoordinationStore(path)
    )
    assert restarted.acquire_lease(candidate, ttl=timedelta(minutes=1)) == old
    takeover_at = NOW + timedelta(minutes=2)
    restarted.expire_leases(now=takeover_at)
    fresh = restarted.acquire_lease(
        candidate, ttl=timedelta(minutes=1), now=takeover_at
    )
    assert fresh is not None and fresh.fencing_token > old.fencing_token
    assert (
        restarted.heartbeat_lease(
            "ws-a",
            agent_key="cursor",
            execution_id="move-a",
            fencing_token=old.fencing_token,
            ttl=timedelta(minutes=1),
            now=takeover_at,
        )
        is None
    )


def test_two_processes_have_one_durable_workspace_winner():
    path = f"/tmp/grandmaster-process-{uuid.uuid4()}.sqlite3"
    SqliteCoordinationStore(path)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    output = context.Queue()
    processes = [
        context.Process(
            target=_process_lease_attempt, args=(path, suffix, start, output)
        )
        for suffix in ("a", "b")
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert sorted(output.get(timeout=2) for _ in processes) == [False, True]


def test_untrusted_or_mismatched_examiner_verdict_is_rejected():
    b = ready_board()
    k = kernel_with(b)
    with pytest.raises(ValueError, match="trusted"):
        k.record_examiner_verdict(
            "exam-1",
            "FREEZE_SHA",
            verdict_evidence(b.examinations["exam-1"], "FREEZE_SHA", source="github"),
        )
    bad = verdict_evidence(b.examinations["exam-1"], "FREEZE_SHA")
    bad = replace(bad, payload={**bad.payload, "candidate_sha": SHA_B})
    with pytest.raises(ValueError, match="not bound"):
        k.record_examiner_verdict("exam-1", "FREEZE_SHA", bad)


def test_dependency_state_comes_from_authoritative_task_not_edge_boolean():
    b = ready_board()
    b.tasks["safe-a"] = task("safe-a", deps=("safe-b",))
    b.dependencies.append(Dependency("safe-a", "safe-b", True, evidence("task_ledger")))
    blocked = kernel_with(b).validate_move(move("move-a", "safe-a", "cursor", "ws-a"))
    assert "dependency_not_ready_or_unknown" in blocked.reasons
    b.tasks["safe-b"] = replace(b.tasks["safe-b"], status=BoardStatus.CERTIFIED)
    b.dependencies[-1] = replace(b.dependencies[-1], satisfied=False)
    assert (
        kernel_with(b).validate_move(move("move-a", "safe-a", "cursor", "ws-a")).legal
    )


def test_dependency_cycle_is_rejected():
    b = ready_board()
    b.tasks["safe-a"] = task("safe-a", deps=("safe-b",))
    b.tasks["safe-b"] = task("safe-b", deps=("safe-a",))
    assert (
        "dependency_cycle"
        in kernel_with(b)
        .validate_move(move("move-a", "safe-a", "cursor", "ws-a"))
        .reasons
    )
    with pytest.raises(ValueError, match="cycle"):
        kernel_with().refresh([b], now=NOW)


def test_report_ingestion_is_idempotent_and_conflicts_fail_closed():
    k = kernel_with()
    report = [
        {
            "agent_key": "cursor",
            "task_key": "safe-a",
            "execution_id": "move-a",
            "result_type": "BUILDER_REPORT",
            "status": "COMPLETE_UNVERIFIED",
        }
    ]
    first = k.ingest_reports("same-message", report)
    second = k.ingest_reports("same-message", report)
    assert first == second and len(k.board.reports) == 1
    conflicting = [{**report[0], "status": "FAILED"}]
    with pytest.raises(ValueError, match="conflicting"):
        k.ingest_reports("same-message", conflicting)


def test_concurrent_report_replay_is_locked_and_singleton():
    k = kernel_with()
    report = [
        {
            "agent_key": "cursor",
            "task_key": "safe-a",
            "execution_id": "move-a",
            "result_type": "BUILDER_REPORT",
            "status": "RUNNING",
        }
    ]
    barrier = threading.Barrier(8)
    threads = [
        threading.Thread(
            target=lambda: (barrier.wait(), k.ingest_reports("race", report))
        )
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(k.board.reports) == 1
