"""Reusable adversarial regression suite for the Grandmaster orchestration kernel.

Covers the nine P1 findings from the 851fdc7 examination plus dependency cycles, idempotent report
ingestion and concurrent board writers. Every test asserts the SECURE property; on a vulnerable
SHA it FAILS with a "P1-n OPEN" / "P2 OPEN" message. Nothing here is skipped or xfailed.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import timedelta

import pytest

import adapter
from adapter import PropertyNotExposed, make_kernel, make_planner
from app.mainai_grandmaster.types import BoardStatus, CandidateSHA, Dependency, EvidencePointer, Workspace, WorkspaceMode
from conftest import FIXED_NOW, SHA_BASE, SHA_CHILD, SHA_EXAM, base_board, ev, execution, mv, task, workspace

TTL = timedelta(minutes=30)


def V(k, move, clock):
    return k.validate_move(move, now=clock())


def L(k, move, clock):
    return k.acquire_lease(move, ttl=TTL, now=clock())


def require(cond, msg):
    if not cond:
        pytest.fail(msg)


# ---------------------------------------------------------------- P1-1 deterministic clock
@pytest.mark.parametrize("wall_offset_days", [-3650, +3650])
def test_p1_1_kernel_default_paths_use_injected_clock(board, clock, monkeypatch, wall_offset_days):
    """Evidence is fresh relative to the injected clock; the wall clock is moved +/- 10 years."""
    import app.mainai_grandmaster.kernel as kmod
    import app.mainai_grandmaster.types as tmod
    try:
        k = make_kernel(board, clock=clock)
    except PropertyNotExposed as exc:
        pytest.fail(str(exc))
    skew = FIXED_NOW + timedelta(days=wall_offset_days)
    monkeypatch.setattr(kmod, "utc_now", lambda: skew, raising=False)
    monkeypatch.setattr(tmod, "utc_now", lambda: skew)
    d = k.validate_move(mv("cursor-t1", "t1", "cursor", "ws1"))  # NO explicit now (registered execution id)
    require(d.legal, f"P1-1 OPEN: default validate_move ignored injected clock (wall skew {wall_offset_days}d) -> {d.outcome} {d.reasons}")
    require(k.discover_sha(branch="feature/x") == SHA_EXAM, "P1-1 OPEN: discover_sha() reads wall clock, not injected clock")


def test_p1_1_suite_fixtures_do_not_expire_with_real_time():
    """Meta-check on THIS suite: fixtures are anchored to FIXED_NOW (2031) and never to datetime.now()."""
    b = base_board()
    for coll in (b.agents, b.tasks, b.executions, b.workspaces):
        for item in coll.values():
            require(item.evidence.observed_at == FIXED_NOW, "suite fixture not anchored to the injected clock")


# ---------------------------------------------------------------- P1-2 one active execution per agent
def test_p1_2_one_agent_cannot_hold_two_leases(board, clock):
    k = make_kernel(board)
    a = L(k, mv("cursor-t1", "t1", "cursor", "ws1"), clock)
    b = L(k, mv("cursor-t2", "t2", "cursor", "ws2"), clock)
    require(a is not None, "fixture error: first lease should be granted")
    require(b is None, "P1-2 OPEN: agent `cursor` received a second concurrent lease (t2/ws2) while holding t1/ws1")


def test_p1_2_validate_reflects_agent_lease(board, clock):
    k = make_kernel(board)
    L(k, mv("cursor-t1", "t1", "cursor", "ws1"), clock)
    d = V(k, mv("cursor-t2", "t2", "cursor", "ws2"), clock)
    require(not d.legal, f"P1-2 OPEN: validate_move still ASSIGNABLE for an agent that holds an active lease ({d.reasons})")


# ---------------------------------------------------------------- P1-3 one active lease per task
def test_p1_3_one_task_cannot_be_leased_twice(board, clock):
    k = make_kernel(board)
    a = L(k, mv("cursor-t1", "t1", "cursor", "ws1"), clock)
    b = L(k, mv("codex-t1", "t1", "codex", "ws2"), clock)
    require(a is not None, "fixture error: first lease should be granted")
    require(b is None, "P1-3 OPEN: task t1 leased to both cursor and codex (duplicate job)")


def test_p1_3_planner_batch_never_picks_two_moves_for_one_task(board, clock, monkeypatch):
    import app.mainai_grandmaster.kernel as kmod
    monkeypatch.setattr(kmod, "utc_now", clock, raising=False)  # isolate from P1-1: planner calls validate_move without `now`
    chosen = [x for x in make_planner(make_kernel(board)).plan(
        [mv("cursor-t1", "t1", "cursor", "ws1"), mv("codex-t1", "t1", "codex", "ws2")]) if x.legal]
    tasks = [x.move.task_key for x in chosen]
    require(len(chosen) >= 1, "fixture error: at least one move should be assignable")
    require(len(tasks) == len(set(tasks)), f"P1-3 OPEN: planner batch chose the same task twice: {[(x.move.agent_key, x.move.task_key) for x in chosen]}")


# ---------------------------------------------------------------- P1-4 canonical workspace path
@pytest.mark.parametrize("alias", ["/worktrees/exam", "/worktrees/exam/", "/worktrees//exam", "/worktrees/./exam",
                                   "/worktrees/ws1/../exam", "/WORKTREES/exam"])
def test_p1_4_alias_of_examiner_worktree_is_rejected(board, clock, alias):
    board.workspaces["alias"] = Workspace("alias", alias, WorkspaceMode.MUTABLE, None, None, None, SHA_BASE, ev("filesystem"))
    d = V(make_kernel(board), mv("cursor-t1", "t1", "cursor", "alias"), clock)
    if alias == "/WORKTREES/exam":
        # case-sensitive filesystems treat this as a different path; record only, do not fail
        print(f"\nINFO case-variant alias {alias!r} -> legal={d.legal}")
        return
    require(not d.legal, f"P1-4 OPEN: builder workspace alias {alias!r} of the examiner worktree is ASSIGNABLE")


def test_p1_4_two_mutable_keys_same_path_cannot_both_lease(board, clock):
    board.workspaces["dup"] = workspace("dup", path="/worktrees/ws1")
    k = make_kernel(board)
    a = L(k, mv("cursor-t1", "t1", "cursor", "ws1"), clock)
    b = L(k, mv("codex-t2", "t2", "codex", "dup"), clock)
    require(not (a and b), "P1-4 OPEN: two workspace keys pointing at the same path were leased to two agents")


# ---------------------------------------------------------------- P1-5 reserved exams + frozen branches
def test_p1_5_fixture_guard_branch_ownership_does_not_mask(board, clock):
    """Meta: with codex owning feature/x and NO examination at all, codex may build there (so a P1-5/P1-6
    rejection really comes from the examination/freeze rule, not from branch ownership)."""
    board.examinations.clear()
    board.candidates.clear()
    board.branches["feature/x"] = replace(board.branches["feature/x"], owner_agent="codex")
    d = V(make_kernel(board), mv("codex-t1", "t1", "codex", "ws1", branch="feature/x"), clock)
    require(d.legal, f"fixture error: baseline codex-on-own-branch should be legal ({d.reasons})")


def test_p1_5_reserved_examination_protects_workspace(board, clock):
    board.examinations["exam-1"] = replace(board.examinations["exam-1"], status=BoardStatus.RESERVED)
    board.workspaces["exam"] = replace(board.workspaces["exam"], mode=WorkspaceMode.MUTABLE, owner_agent=None, execution_id=None, branch=None)
    d = V(make_kernel(board), mv("cursor-t1", "t1", "cursor", "exam"), clock)
    require(not d.legal, "P1-5 OPEN: RESERVED (not yet started) examination does not protect its workspace")


@pytest.mark.parametrize("status", [BoardStatus.RESERVED, BoardStatus.UNDER_EXAMINATION, BoardStatus.CERTIFIED])
def test_p1_5_examined_or_frozen_branch_is_never_a_mutable_builder_branch(board, clock, status):
    board.examinations["exam-1"] = replace(board.examinations["exam-1"], status=status)
    board.candidates[SHA_EXAM] = replace(board.candidates[SHA_EXAM], frozen=status == BoardStatus.CERTIFIED,
                                         status=status)
    board.branches["feature/x"] = replace(board.branches["feature/x"], owner_agent="codex")  # builder owns its branch
    d = V(make_kernel(board), mv("codex-t1", "t1", "codex", "ws1", branch="feature/x", base=SHA_EXAM, child=SHA_EXAM), clock)
    require(not d.legal, f"P1-5 OPEN: examined/frozen candidate branch feature/x usable as MUTABLE builder branch (exam {status.value})")


# ---------------------------------------------------------------- P1-6 RETURN_TO_BUILDER child enforcement
def _returned_kernel(board):
    board.branches["feature/x"] = replace(board.branches["feature/x"], owner_agent="codex")  # builder owns its branch
    k = make_kernel(board)
    k.record_examiner_verdict("exam-1", "RETURN_TO_BUILDER", adapter.registry_evidence(k, "exam-1", "RETURN_TO_BUILDER"))
    k.board.workspaces["exam"] = replace(k.board.workspaces["exam"], mode=WorkspaceMode.MUTABLE, owner_agent=None, execution_id=None, branch=None)
    return k


@pytest.mark.parametrize("label,ws,branch", [("same branch + examiner ws", "exam", "feature/x"),
                                              ("same branch, other ws", "ws1", "feature/x"),
                                              ("new branch, examiner ws", "exam", "codex/x-child")])
def test_p1_6_return_to_builder_requires_new_child_at_the_gate(board, clock, label, ws, branch):
    k = _returned_kernel(board)
    m = mv("codex-t1", "t1", "codex", ws, branch=branch, base=SHA_EXAM, child=SHA_EXAM)
    d = V(k, m, clock)
    lease = L(k, m, clock)
    require(not d.legal and lease is None, f"P1-6 OPEN: after RETURN_TO_BUILDER, gate allowed {label} (legal={d.legal}, lease={bool(lease)})")


def test_p1_6_control_new_child_branch_and_workspace_is_allowed(board, clock):
    k = _returned_kernel(board)
    d = V(k, mv("codex-t1", "t1", "codex", "ws3", branch="codex/x-child", base=SHA_EXAM, child=SHA_EXAM), clock)
    require(d.legal, f"P1-6 control: a genuine new child branch+workspace must remain assignable ({d.reasons})")


# ---------------------------------------------------------------- P1-7 explicit action allowlist
FORBIDDEN = ["merge", "Merge", "MERGE", " merge", "merge_pr", "merge-to-main", "squash_merge", "deploy", "deploy_prod",
             "release", "promote", "activate", "activate_recall", "enable_recall", "grant_credentials", "push_to_main",
             "force_push", "spend", "purchase", "override_security", "mutate_founder_policy", "", "unknown_action"]


@pytest.mark.parametrize("action", FORBIDDEN)
def test_p1_7_non_allowlisted_actions_are_never_assignable(board, clock, action):
    d = V(make_kernel(board), mv("cursor-t1", "t1", "cursor", "ws1", action=action), clock)
    require(not d.legal, f"P1-7 OPEN: action {action!r} classified ASSIGNABLE (denylist instead of allowlist)")


def test_p1_7_control_build_is_assignable(board, clock):
    d = V(make_kernel(board), mv("cursor-t1", "t1", "cursor", "ws1", action="build"), clock)
    require(d.legal, f"P1-7 control: plain build must stay assignable ({d.reasons})")


# ---------------------------------------------------------------- P1-8 durable leases + fencing
def test_p1_8_two_processes_cannot_both_lease_one_workspace(clock):
    try:
        k1, k2 = adapter.make_kernels_sharing_durable_lease_store(base_board, clock=clock)
    except PropertyNotExposed as exc:
        pytest.fail(str(exc))
    a = L(k1, mv("cursor-t1", "t1", "cursor", "ws1"), clock)
    b = L(k2, mv("codex-t2", "t2", "codex", "ws1"), clock)
    require(not (a and b), "P1-8 OPEN: two processes leased the same workspace")


def test_p1_8_active_lease_survives_restart(clock):
    try:
        k1, k_restarted = adapter.make_kernels_sharing_durable_lease_store(base_board, clock=clock)
    except PropertyNotExposed as exc:
        pytest.fail(str(exc))
    L(k1, mv("cursor-t1", "t1", "cursor", "ws1"), clock)
    k_restarted.refresh([base_board()], now=clock())
    require(L(k_restarted, mv("codex-t2", "t2", "codex", "ws1"), clock) is None, "P1-8 OPEN: restart forgot an active lease")


def test_p1_8_fencing_token_strictly_increases_on_takeover(board, clock):
    k = make_kernel(board)
    first = L(k, mv("cursor-t1", "t1", "cursor", "ws1"), clock)
    clock.advance(minutes=31)
    k.expire_leases(now=clock())
    second = L(k, mv("codex-t2", "t2", "codex", "ws1"), clock)
    require(first is not None and second is not None, "fixture error: takeover after expiry should succeed")
    try:
        t1, t2 = adapter.fencing_token(first), adapter.fencing_token(second)
    except PropertyNotExposed as exc:
        pytest.fail(str(exc))
    require(t2 > t1, f"P1-8 OPEN: fencing token did not increase on takeover ({t1!r} -> {t2!r})")
    for tok in (t1, t2):  # stale owner presents its own old token, then the (guessable) new token
        require(k.heartbeat_lease("ws1", agent_key="cursor", execution_id="cursor-t1", fencing_token=tok, ttl=TTL, now=clock()) is None,
                f"P1-8 OPEN: stale owner could heartbeat after takeover (token {tok})")
        require(not k.release_lease("ws1", agent_key="cursor", execution_id="cursor-t1", fencing_token=tok, now=clock()),
                f"P1-8 OPEN: stale owner could release the new owner's lease (token {tok})")
    require(k.heartbeat_lease("ws1", agent_key="codex", execution_id="codex-t2", fencing_token=t2, ttl=TTL, now=clock()) is not None,
            "fixture error: current owner must be able to heartbeat")


# ---------------------------------------------------------------- P1-9 examiner identity + verdict provenance
def test_p1_9_builder_who_used_examiner_workspace_cannot_certify(board):
    board.executions["claude-built"] = execution("claude-built", "build-x", "claude", BoardStatus.COMPLETE_UNVERIFIED, "exam", "feature/x", SHA_EXAM)
    k = make_kernel(board)
    with pytest.raises(ValueError, match="builder cannot certify"):
        k.record_examiner_verdict("exam-1", "FREEZE_SHA", adapter.registry_evidence(k, "exam-1", "FREEZE_SHA"))
        pytest.fail("P1-9 OPEN: builder whose execution used the examiner workspace certified its own candidate")


@pytest.mark.parametrize("source", ["founder_message", "agent_self_report", "chat", "mainai_generated_text", "github", "filesystem"])
def test_p1_9_freeze_requires_trusted_verdict_provenance(board, source):
    k = make_kernel(board)
    try:
        k.record_examiner_verdict("exam-1", "FREEZE_SHA", adapter.registry_evidence(k, "exam-1", "FREEZE_SHA", source=source))
        accepted = True
    except ValueError:
        accepted = False
    require(not accepted, f"P1-9 OPEN: FREEZE verdict accepted from untrusted source {source!r}")


def test_p1_9_control_registry_verdict_from_independent_examiner_accepted(board):
    k = make_kernel(board)
    k.record_examiner_verdict("exam-1", "FREEZE_SHA", adapter.registry_evidence(k, "exam-1", "FREEZE_SHA"))
    require(k.board.candidates[SHA_EXAM].status == BoardStatus.CERTIFIED, "P1-9 control: legitimate verdict must record")


# ---------------------------------------------------------------- additional: dependencies
def test_dep_failed_dependency_is_not_satisfied(board, clock):
    board.tasks["dep"] = task("dep", BoardStatus.FAILED)
    board.tasks["t1"] = replace(board.tasks["t1"], dependencies=frozenset({"dep"}))
    board.dependencies.append(Dependency("t1", "dep", True, ev("task_ledger")))
    require(not V(make_kernel(board), mv("cursor-t1", "t1", "cursor", "ws1"), clock).legal,
            "P2 OPEN: dependency on a FAILED task treated as satisfied (edge flag trusted over task state)")


@pytest.mark.parametrize("cycle", [("t1", "t2"), ("t1", "t2", "t3")])
def test_dep_cycles_are_detected(board, clock, cycle):
    for i, a in enumerate(cycle):
        b = cycle[(i + 1) % len(cycle)]
        board.tasks[a] = replace(board.tasks[a], dependencies=frozenset({b}))
        board.dependencies.append(Dependency(a, b, True, ev("task_ledger")))
    d = V(make_kernel(board), mv("cursor-t1", "t1", "cursor", "ws1"), clock)
    require(not d.legal, f"P2 OPEN: dependency cycle {cycle} not detected (legal, reasons={d.reasons})")


def test_dep_self_dependency_rejected(board, clock):
    board.tasks["t1"] = replace(board.tasks["t1"], dependencies=frozenset({"t1"}))
    board.dependencies.append(Dependency("t1", "t1", True, ev("task_ledger")))
    require(not V(make_kernel(board), mv("cursor-t1", "t1", "cursor", "ws1"), clock).legal, "P2 OPEN: self-dependency accepted")


# ---------------------------------------------------------------- additional: report ingestion
R = {"agent_key": "cursor", "task_key": "t1", "execution_id": "cursor-t1", "result_type": "EXECUTION_EVENT", "status": "RUNNING"}


def test_reports_reingest_is_idempotent(board):
    k = make_kernel(board)
    k.ingest_reports("m1", [R, dict(R, agent_key="codex", execution_id="codex-t1")])
    k.ingest_reports("m1", [R, dict(R, agent_key="codex", execution_id="codex-t1")])
    ids = [e.event_id for e in k.board.reports]
    require(len(ids) == len(set(ids)) == 2, f"P2 OPEN: re-ingesting the same message duplicated events: {ids}")


def test_reports_never_mutate_authoritative_state(board, clock):
    k = make_kernel(board)
    k.ingest_reports("m2", [dict(R, status="RUNNING"), dict(R, agent_key="claude", execution_id="exec-exam", task_key="exam-x", status="IDLE")])
    require(k.board.agents["claude"].status == BoardStatus.RUNNING and k.board.executions["cursor-t1"].status == BoardStatus.IDLE,
            "REGRESSION: a self-report changed authoritative state")


def test_reports_distinct_per_report_in_one_message(board):
    k = make_kernel(board)
    out = k.ingest_reports("m3", [R, dict(R, result_type="BUILDER_REPORT", status="COMPLETE_UNVERIFIED")])
    require(len({e.event_id for e in out}) == 2, "REGRESSION: reports in one message collapsed")


# ---------------------------------------------------------------- additional: concurrent writers
def test_concurrent_writers_keep_invariants(clock):
    """Threads race lease acquisition (same workspace / same agent / same task), report ingestion and refresh."""
    k = make_kernel(base_board())
    barrier = threading.Barrier(8)
    errors, leases = [], []

    def lease(move):
        try:
            barrier.wait()
            leases.append((move.agent_key, move.task_key, move.workspace_key, L(k, move, clock)))
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    def ingest(i):
        try:
            barrier.wait()
            for j in range(50):
                k.ingest_reports(f"c{i}-{j}", [R])
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    def refresh():
        try:
            barrier.wait()
            for _ in range(20):
                k.refresh([base_board()], now=clock())
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    moves = [mv("cursor-t1", "t1", "cursor", "ws1"), mv("codex-t1", "t1", "codex", "ws2"),
             mv("cursor-t2", "t2", "cursor", "ws3"), mv("codex-t3", "t3", "codex", "ws1")]
    ts = [threading.Thread(target=lease, args=(m,)) for m in moves]
    ts += [threading.Thread(target=ingest, args=(i,)) for i in range(2)] + [threading.Thread(target=refresh) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    require(not errors, f"concurrent writers raised: {errors[:3]}")
    granted = [(a, t, w) for a, t, w, l in leases if l is not None]
    agents = [a for a, _, _ in granted]
    tasks = [t for _, t, _ in granted]
    wss = [w for _, _, w in granted]
    require(len(wss) == len(set(wss)), f"P1 OPEN: concurrent writers leased one workspace twice: {granted}")
    require(len(agents) == len(set(agents)), f"P1-2 OPEN (concurrent): one agent got two leases: {granted}")
    require(len(tasks) == len(set(tasks)), f"P1-3 OPEN (concurrent): one task leased twice: {granted}")


def test_reports_survive_a_board_refresh(board, clock):
    k = make_kernel(board)
    k.ingest_reports("keep-me", [R, dict(R, agent_key="codex", execution_id="codex-t1")])
    k.refresh([base_board()], now=clock())
    kept = [e.event_id for e in k.board.reports if e.message_id == "keep-me"]
    require(len(kept) == 2, f"P2 OPEN: refresh() silently discarded previously ingested report events (kept {kept})")
