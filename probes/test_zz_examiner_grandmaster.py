"""Examiner-only adversarial probes for the grandmaster kernel 851fdc7 (disposable, never committed)."""
from dataclasses import replace
from datetime import timedelta

import app.mainai_grandmaster.kernel as kmod
from app.mainai_grandmaster.kernel import GrandmasterKernel
from app.mainai_grandmaster.planner import GrandmasterPlanner
from app.mainai_grandmaster.types import (
    BoardStatus, CandidateSHA, Dependency, Examination, Workspace, WorkspaceMode,
)
from tests.backend.mainai.test_grandmaster_orchestration_kernel import (
    NOW, SHA_A, SHA_B, evidence, execution, move, ready_board, task, workspace,
)

kmod.utc_now = lambda: NOW  # pin the clock for methods that do not take `now`
TTL = timedelta(minutes=30)


def report(name, holds, detail=""):
    print(f"\n{'HOLDS' if holds else 'GAP  '} {name} {detail}")


def _k(board=None):
    return GrandmasterKernel(board or ready_board())


def test_00_board_shape():
    b = ready_board()
    print("\nBOARD workspaces:", {k: (w.path, w.mode.value, w.owner_agent) for k, w in b.workspaces.items()},
          "examinations:", {k: (e.status.value, e.workspace_key, e.candidate_sha[:6]) for k, e in b.examinations.items()},
          "candidates:", {k[:6]: (c.branch, c.status.value, c.frozen) for k, c in b.candidates.items()})


def test_same_agent_two_leases():
    b = ready_board()
    b.tasks["safe-c"] = task("safe-c")
    b.executions["move-c"] = execution("move-c", "safe-c", "cursor")
    b.workspaces["ws-c"] = workspace("ws-c")
    k = _k(b)
    l1 = k.acquire_lease(move("move-a", "safe-a", "cursor", "ws-a"), ttl=TTL, now=NOW)
    l2 = k.acquire_lease(move("move-c", "safe-c", "cursor", "ws-c"), ttl=TTL, now=NOW)
    report("one agent cannot hold two concurrent leases/jobs", not (l1 and l2), f"(lease1={bool(l1)}, lease2={bool(l2)} for agent cursor)")


def test_same_task_two_agents():
    b = ready_board()
    b.executions["move-a2"] = execution("move-a2", "safe-a", "codex")
    b.workspaces["ws-a2"] = workspace("ws-a2")
    k = _k(b)
    l1 = k.acquire_lease(move("move-a", "safe-a", "cursor", "ws-a"), ttl=TTL, now=NOW)
    l2 = k.acquire_lease(move("move-a2", "safe-a", "codex", "ws-a2"), ttl=TTL, now=NOW)
    report("one task cannot be leased to two agents (duplicate job)", not (l1 and l2), f"(cursor={bool(l1)}, codex={bool(l2)} both on task safe-a)")
    planner = GrandmasterPlanner(_k(b))
    chosen = [p for p in planner.plan([move("move-a", "safe-a", "cursor", "ws-a"), move("move-a2", "safe-a", "codex", "ws-a2")]) if p.legal]
    report("planner batch does not choose two moves for the same task", len(chosen) <= 1, f"(chosen={[(p.move.agent_key, p.move.task_key) for p in chosen]})")


def test_workspace_path_alias_of_examiner():
    b = ready_board()
    exam_ws = b.workspaces["exam-ws"]
    b.workspaces["alias-ws"] = Workspace("alias-ws", exam_ws.path, WorkspaceMode.MUTABLE, None, None, None, SHA_A, evidence("filesystem"))
    k = _k(b)
    d = k.validate_move(move("move-a", "safe-a", "cursor", "alias-ws"), now=NOW)
    report("builder cannot reach examiner worktree via a second key with the same path", not d.legal, f"(path={exam_ws.path}; legal={d.legal} reasons={d.reasons})")


def test_examiner_workspace_before_exam_starts():
    b = ready_board()
    ex = next(iter(b.examinations.values()))
    b.examinations[ex.examination_id] = replace(ex, status=BoardStatus.RESERVED)
    b.workspaces[ex.workspace_key] = replace(b.workspaces[ex.workspace_key], mode=WorkspaceMode.MUTABLE, owner_agent=None, execution_id=None, branch=None)
    k = _k(b)
    d = k.validate_move(move("move-a", "safe-a", "cursor", ex.workspace_key), now=NOW)
    report("reserved (not yet started) examination still protects its workspace", not d.legal, f"(legal={d.legal} reasons={d.reasons})")


def _examined(b):
    ex = next(iter(b.examinations.values()))
    return ex, b.candidates[ex.candidate_sha]


def test_after_freeze_branch_mutation():
    b = ready_board()
    ex, cand = _examined(b)
    k = _k(b)
    k.record_examiner_verdict(ex.examination_id, "FREEZE_SHA", evidence("examiner_registry"))
    b2 = k.board
    b2.workspaces["ws-f"] = workspace("ws-f")
    d = k.validate_move(move("move-a", "safe-a", "cursor", "ws-f", branch=cand.branch, base=cand.sha, child=cand.sha), now=NOW)
    report("after FREEZE, builder cannot take the frozen candidate's own branch as mutable", not d.legal,
           f"(branch={cand.branch}; legal={d.legal} reasons={d.reasons})")


def test_after_return_same_branch_workspace():
    b = ready_board()
    ex, cand = _examined(b)
    k = _k(b)
    k.record_examiner_verdict(ex.examination_id, "RETURN_TO_BUILDER", evidence("examiner_registry"))
    k.board.workspaces[ex.workspace_key] = replace(k.board.workspaces[ex.workspace_key], mode=WorkspaceMode.MUTABLE, owner_agent=None, execution_id=None, branch=None)
    d = k.validate_move(move("move-a", "safe-a", "cursor", ex.workspace_key, branch=cand.branch, base=cand.sha), now=NOW)
    report("after RETURN_TO_BUILDER, validate_move itself requires a new child/workspace (not only next_builder_child)", not d.legal,
           f"(reused examined branch {cand.branch} + examiner workspace; legal={d.legal} reasons={d.reasons})")


def test_forbidden_action_variants():
    k = _k()
    leaks = []
    for act in ("Merge", "merge_pr", "merge-to-main", "deploy_prod", "release", "activate", "enable_recall", "grant_credentials", "push_to_main"):
        d = k.validate_move(move("move-a", "safe-a", "cursor", "ws-a", action=act), now=NOW)
        if d.legal:
            leaks.append(act)
    report("authority-bearing action variants are not ASSIGNABLE (allowlist, not exact-string denylist)", not leaks, f"(legal: {leaks})")


def test_report_ingestion():
    k = _k()
    before = dict(k.board.agents)
    k.ingest_reports("m1", [{"agent_key": "cursor", "task_key": "safe-a", "execution_id": "move-a", "result_type": "EXECUTION_EVENT", "status": "RUNNING"},
                            {"agent_key": "codex", "task_key": "safe-b", "execution_id": "move-b", "result_type": "BUILDER_REPORT", "status": "COMPLETE_UNVERIFIED"}])
    report("self-reports never change authoritative agent/execution state", k.board.agents == before and k.board.executions["move-a"].status == BoardStatus.IDLE)
    report("two reports in one message stay two distinct events", len({e.event_id for e in k.board.reports}) == 2)
    k.ingest_reports("m1", [{"agent_key": "cursor", "task_key": "safe-a", "execution_id": "move-a", "result_type": "EXECUTION_EVENT", "status": "RUNNING"}])
    ids = [e.event_id for e in k.board.reports]
    report("re-ingesting the same message is idempotent (no duplicate event ids)", len(ids) == len(set(ids)), f"(event ids={ids})")


def test_dependency_truth_and_cycles():
    b = ready_board()
    b.tasks["dep-x"] = task("dep-x", status=BoardStatus.FAILED)
    b.tasks["safe-a"] = replace(b.tasks["safe-a"], dependencies=frozenset({"dep-x"}))
    b.dependencies.append(Dependency("safe-a", "dep-x", True, evidence("task_ledger")))
    d = _k(b).validate_move(move("move-a", "safe-a", "cursor", "ws-a"), now=NOW)
    report("dependency on a FAILED task is not satisfied just because the edge says satisfied=True", not d.legal, f"(legal={d.legal})")
    b = ready_board()
    b.tasks["safe-a"] = replace(b.tasks["safe-a"], dependencies=frozenset({"safe-b"}))
    b.tasks["safe-b"] = replace(b.tasks["safe-b"], dependencies=frozenset({"safe-a"}))
    b.dependencies += [Dependency("safe-a", "safe-b", True, evidence("task_ledger")), Dependency("safe-b", "safe-a", True, evidence("task_ledger"))]
    d1 = _k(b).validate_move(move("move-a", "safe-a", "cursor", "ws-a"), now=NOW)
    report("dependency cycle (a<->b) detected and refused", not d1.legal, f"(legal={d1.legal} reasons={d1.reasons})")


def test_verdict_authority():
    b = ready_board()
    ex, cand = _examined(b)
    b.executions["builder-in-exam-ws"] = execution("builder-in-exam-ws", cand.task_key, ex.examiner_agent, workspace=ex.workspace_key)
    k = _k(b)
    try:
        k.record_examiner_verdict(ex.examination_id, "FREEZE_SHA", evidence("examiner_registry"))
        ok = True
    except ValueError:
        ok = False
    report("builder that built in the examiner's workspace cannot certify its own candidate", not ok, f"(certified={ok})")
    k2 = _k()
    ex2, _ = _examined(k2.board)
    try:
        k2.record_examiner_verdict(ex2.examination_id, "FREEZE_SHA", evidence("founder_message"))
        ok2 = True
    except ValueError:
        ok2 = False
    report("FREEZE verdict cannot be recorded from a chat/founder-message source flagged authoritative", not ok2, f"(accepted={ok2})")


def test_two_kernel_instances_no_shared_lease():
    b = ready_board()
    k1, k2 = GrandmasterKernel(b.copy()), GrandmasterKernel(b.copy())
    l1 = k1.acquire_lease(move("move-a", "safe-a", "cursor", "ws-a"), ttl=TTL, now=NOW)
    l2 = k2.acquire_lease(move("move-b", "safe-b", "codex", "ws-a"), ttl=TTL, now=NOW)
    report("two kernel instances (two workers/processes) cannot both lease the same workspace", not (l1 and l2),
           f"(k1={bool(l1)}, k2={bool(l2)}; leases are in-process memory only)")


def test_lease_survives_restart():
    b = ready_board()
    k1 = GrandmasterKernel(b)
    k1.acquire_lease(move("move-a", "safe-a", "cursor", "ws-a"), ttl=TTL, now=NOW)
    k_restarted = GrandmasterKernel(ready_board())
    k_restarted.refresh([ready_board()], now=NOW)
    l = k_restarted.acquire_lease(move("move-b", "safe-b", "codex", "ws-a"), ttl=TTL, now=NOW)
    report("an active lease survives a kernel restart", l is None, f"(after restart, second agent acquired ws-a: {bool(l)})")
