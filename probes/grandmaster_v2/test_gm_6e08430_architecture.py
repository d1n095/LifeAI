"""Examiner-only architecture/adversarial probes for 6e08430 (SqliteCoordinationStore). Asserts SECURE properties."""
from __future__ import annotations

import multiprocessing as mp
import os
import sqlite3
import tempfile
from dataclasses import replace
from datetime import timedelta

import pytest

import adapter
from app.mainai_grandmaster.coordination import SqliteCoordinationStore, normalize_physical_path
from app.mainai_grandmaster.types import BoardStatus, Workspace, WorkspaceMode
from conftest import FIXED_NOW, SHA_BASE, SHA_EXAM, base_board, ev, execution, mv, workspace

TTL = timedelta(minutes=30)


def require(cond, msg):
    if not cond:
        pytest.fail(msg)


# ---------------------------------------------------------------- E nested / overlapping physical paths
def test_E_nested_workspace_inside_leased_workspace_is_rejected(board, clock):
    board.workspaces["nested"] = workspace("nested", path="/worktrees/ws1/backend")
    k = adapter.make_kernel(board, clock=clock)
    a = k.acquire_lease(mv("cursor-t1", "t1", "cursor", "ws1"), ttl=TTL, now=clock())
    b = k.acquire_lease(mv("codex-t2", "t2", "codex", "nested"), ttl=TTL, now=clock())
    require(a is not None, "fixture error")
    require(b is None, "P1 OPEN (E): a workspace NESTED inside another agent's leased worktree was leased (exact-path equality only)")


def test_E_parent_of_examiner_worktree_is_rejected(board, clock):
    board.workspaces["parent"] = workspace("parent", path="/worktrees")
    d = adapter.make_kernel(board, clock=clock).validate_move(mv("cursor-t1", "t1", "cursor", "parent"), now=clock())
    require(not d.legal, "P1 OPEN (E): a MUTABLE workspace that CONTAINS the read-only examiner worktree is ASSIGNABLE")


def test_E_case_variant_on_case_insensitive_fs(clock, tmp_path):
    real = tmp_path / "Exam"
    real.mkdir()
    alias = str(tmp_path / "exam")
    same_dir = os.path.exists(alias) and os.path.samefile(alias, real)
    b = base_board()
    b.workspaces["exam"] = replace(b.workspaces["exam"], path=str(real))
    b.workspaces["alias"] = Workspace("alias", alias, WorkspaceMode.MUTABLE, None, None, None, SHA_BASE, ev("filesystem"))
    d = adapter.make_kernel(b, clock=clock).validate_move(mv("cursor-t1", "t1", "cursor", "alias"), now=clock())
    print(f"\nINFO host fs case-insensitive={same_dir} normalize={normalize_physical_path(alias)!r} vs {normalize_physical_path(str(real))!r}")
    require(not (same_dir and d.legal), "P1 OPEN (E): case-variant path of the SAME physical examiner directory is ASSIGNABLE on this host")


def test_E_symlink_alias_is_rejected(clock, tmp_path):
    real = tmp_path / "exam"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    b = base_board()
    b.workspaces["exam"] = replace(b.workspaces["exam"], path=str(real))
    b.workspaces["alias"] = Workspace("alias", str(link), WorkspaceMode.MUTABLE, None, None, None, SHA_BASE, ev("filesystem"))
    d = adapter.make_kernel(b, clock=clock).validate_move(mv("cursor-t1", "t1", "cursor", "alias"), now=clock())
    require(not d.legal, "P1 OPEN (E): symlink alias of examiner worktree ASSIGNABLE")


# ---------------------------------------------------------------- F branch exclusivity in the durable authority
def test_F_two_processes_cannot_lease_same_branch(clock):
    k1, k2 = adapter.make_kernels_sharing_durable_lease_store(base_board, clock=clock)
    a = k1.acquire_lease(mv("cursor-t1", "t1", "cursor", "ws1", branch="shared/branch"), ttl=TTL, now=clock())
    b = k2.acquire_lease(mv("codex-t2", "t2", "codex", "ws2", branch="shared/branch"), ttl=TTL, now=clock())
    require(a is not None, "fixture error")
    require(b is None, "P1 OPEN (F): two kernels sharing the durable store leased the SAME branch to two agents in two workspaces")


def test_F_single_kernel_same_branch(board, clock):
    k = adapter.make_kernel(board, clock=clock)
    a = k.acquire_lease(mv("cursor-t1", "t1", "cursor", "ws1", branch="shared/branch"), ttl=TTL, now=clock())
    b = k.acquire_lease(mv("codex-t2", "t2", "codex", "ws2", branch="shared/branch"), ttl=TTL, now=clock())
    require(a is not None, "fixture error")
    require(b is None, "P1 OPEN (F): one kernel leased the same branch to two agents (lease is not recorded as a branch execution)")


# ---------------------------------------------------------------- real OS processes racing the store
def _proc(path, idx, q):
    import sys
    sys.path[:0] = [os.path.dirname(__file__)]
    from conftest import FIXED_NOW, mv as _mv
    from app.mainai_grandmaster.coordination import SqliteCoordinationStore as S
    s = S(path)
    m = [_mv("cursor-t1", "t1", "cursor", "ws1"), _mv("codex-t2", "t2", "codex", "ws9")][idx]
    q.put(s.acquire(m, "/worktrees/race", timedelta(minutes=30), FIXED_NOW) is not None)


def test_store_real_process_race_same_path():
    wins = []
    for _ in range(10):
        path = os.path.join(tempfile.mkdtemp(prefix="gm-race-"), "c.sqlite3")
        SqliteCoordinationStore(path)
        ctx = mp.get_context("spawn")
        q = ctx.Queue()
        ps = [ctx.Process(target=_proc, args=(path, i, q)) for i in range(2)]
        [p.start() for p in ps]
        [p.join(60) for p in ps]
        wins.append(sum(q.get() for _ in range(2)))
    require(all(w == 1 for w in wins), f"P1 OPEN: real OS processes both leased one physical path: per-trial winners {wins}")


# ---------------------------------------------------------------- store-level fencing / theft
def test_store_workspace_key_reuse_cannot_steal_active_lease(tmp_path):
    s = SqliteCoordinationStore(str(tmp_path / "c.sqlite3"))
    a = s.acquire(mv("cursor-t1", "t1", "cursor", "ws1"), "/worktrees/A", TTL, FIXED_NOW)
    b = s.acquire(mv("codex-t2", "t2", "codex", "ws1"), "/worktrees/B", TTL, FIXED_NOW)  # same key, other path
    require(a is not None, "fixture error")
    require(b is None, f"P2 OPEN: same workspace_key with a different path overwrote cursor's ACTIVE lease (now {b and (b.owner_agent, b.workspace_path, b.fencing_token)})")


def test_store_fencing_token_not_reset_when_row_survives(tmp_path):
    s = SqliteCoordinationStore(str(tmp_path / "c.sqlite3"))
    toks = []
    for i, ag in enumerate(("cursor", "codex", "cursor")):
        now = FIXED_NOW + timedelta(hours=i)
        s.expire(now)
        l = s.acquire(mv(f"{ag}-t{i+1}", f"t{i+1}", ag, "ws1"), "/worktrees/ws1", TTL, now)
        toks.append(l.fencing_token)
    require(toks == sorted(set(toks)), f"P1-8 OPEN: tokens not strictly increasing {toks}")


def test_store_file_is_not_world_writable(tmp_path):
    p = tmp_path / "c.sqlite3"
    SqliteCoordinationStore(str(p))
    mode = oct(os.stat(p).st_mode & 0o777)
    print(f"\nINFO store file mode {mode}; any local process with write access is a lease authority (no auth/integrity)")


def test_store_raw_writer_can_forge_lease(tmp_path):
    """Any local writer of the file can rewrite authority: there is no integrity check on rows."""
    p = str(tmp_path / "c.sqlite3")
    s = SqliteCoordinationStore(p)
    s.acquire(mv("cursor-t1", "t1", "cursor", "ws1"), "/worktrees/ws1", TTL, FIXED_NOW)
    with sqlite3.connect(p) as c:
        c.execute("UPDATE grandmaster_leases SET owner_agent='codex', execution_id='codex-t1', fencing_token=fencing_token-1")
    l = s.active(FIXED_NOW)[0]
    print(f"\nINFO raw file writer rewrote owner -> {l.owner_agent}, token -> {l.fencing_token} (accepted)")


# ---------------------------------------------------------------- D fail-closed when the store is absent
def test_D_no_store_validate_and_planner_fail_closed(board, clock):
    k = adapter.make_kernel(board, clock=clock, store=None)
    require(k.acquire_lease(mv("cursor-t1", "t1", "cursor", "ws1"), ttl=TTL, now=clock()) is None, "P1 OPEN (D): lease granted without a store")
    d = k.validate_move(mv("cursor-t1", "t1", "cursor", "ws1"), now=clock())
    require(not d.legal, f"P2 OPEN (D): without the durable store validate_move/planner still report ASSIGNABLE ({d.outcome}); "
                         "only acquire_lease fails closed, so any consumer dispatching on the plan bypasses all lease checks")


def test_D_unreadable_store_fails_closed(board, clock, tmp_path):
    p = tmp_path / "c.sqlite3"
    s = SqliteCoordinationStore(str(p))
    k = adapter.make_kernel(board, clock=clock, store=s)
    os.remove(p)
    p.mkdir()  # path now a directory: store unusable
    try:
        d = k.validate_move(mv("cursor-t1", "t1", "cursor", "ws1"), now=clock())
        lease = k.acquire_lease(mv("cursor-t1", "t1", "cursor", "ws1"), ttl=TTL, now=clock())
        outcome = f"validate={d.outcome} lease={lease is not None}"
        ok = not d.legal and lease is None
    except Exception as exc:  # noqa: BLE001  (raising == fail closed)
        outcome, ok = f"raised {type(exc).__name__}", True
    require(ok, f"P1 OPEN (D): broken store did not fail closed: {outcome}")


# ---------------------------------------------------------------- frozen/verdict state durability
def _freeze(k):
    k.record_examiner_verdict("exam-1", "FREEZE_SHA", adapter.registry_evidence(k, "exam-1", "FREEZE_SHA"))


def test_frozen_protection_survives_refresh(board, clock):
    k = adapter.make_kernel(board, clock=clock)
    _freeze(k)
    require(k.board.candidates[SHA_EXAM].frozen, "fixture error")
    k.refresh([base_board()], now=clock())
    d = k.validate_move(mv("codex-t1", "t1", "codex", "ws1", branch="feature/x"), now=clock())
    require(not d.legal and "frozen_candidate_branch_is_immutable" in d.reasons,
            f"P1 OPEN: FREEZE verdict is process memory: after refresh the frozen branch is no longer protected as frozen ({d.reasons})")


def test_verdict_provenance_is_checked_against_registry_not_a_label(board, clock):
    """A caller-constructed pointer labelled 'verification_registry' with matching payload is accepted with no lookup."""
    k = adapter.make_kernel(board, clock=clock)
    forged = adapter.registry_evidence(k, "exam-1", "FREEZE_SHA")  # never written to any registry
    try:
        _ = k.record_examiner_verdict("exam-1", "FREEZE_SHA", forged)
        accepted = True
    except ValueError:
        accepted = False
    require(not accepted, "P1 OPEN (9): FREEZE accepted from a self-constructed EvidencePointer labelled 'verification_registry' "
                          "(no lookup in mainai_verification_records / no signature)")


@pytest.mark.parametrize("variant", ["Codex", "CODEX", "codex "])
def test_examiner_identity_variants_of_builder_rejected(board, clock, variant):
    board.examinations["exam-1"] = replace(board.examinations["exam-1"], examiner_agent=variant)
    k = adapter.make_kernel(board, clock=clock)
    try:
        _freeze(k)
        accepted = True
    except ValueError:
        accepted = False
    require(not accepted, f"P1 OPEN (9): builder 'codex' certified its own SHA as examiner {variant!r}")


def test_self_certifier_in_exam_workspace_certification_valid(board, clock):
    board.executions["claude-built"] = execution("claude-built", "build-x", "claude", BoardStatus.COMPLETE_UNVERIFIED, "exam", "feature/x", SHA_EXAM)
    k = adapter.make_kernel(board, clock=clock)
    try:
        _freeze(k)
        recorded = True
    except ValueError:
        recorded = False
    print(f"\nINFO record_examiner_verdict accepted={recorded} frozen={k.board.candidates[SHA_EXAM].frozen} "
          f"status={k.board.candidates[SHA_EXAM].status} certification_valid={k.certification_valid(SHA_EXAM, 'claude')}")
    require(not recorded, "P1 OPEN (9): self-builder froze/CERTIFIED its own candidate on the board (certification_valid disagrees)")


# ---------------------------------------------------------------- C two authorities
def test_C_sqlite_store_is_unaware_of_postgres_coordination():
    import inspect
    import app.mainai_grandmaster.coordination as c
    import app.mainai_grandmaster.kernel as k
    src = inspect.getsource(c) + inspect.getsource(k)
    refs = [x for x in ("agent_scope_leases", "agent_coordination", "mainai_task_worktrees", "agent_work_assignments", "mainai_verification_registry") if x in src]
    require(refs, "P1 OPEN (C): Grandmaster authority never consults the PostgreSQL coordination store "
                  "(agent_scope_leases / mainai_task_worktrees / registry); both can grant the same worktree/branch")
