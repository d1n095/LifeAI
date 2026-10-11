"""Self-contained fixtures for the Grandmaster verification suite.

Deliberately independent of the builder's own test helpers (which may change between SHAs) and of
the backend DB conftest (the kernel is pure Python). All evidence is built relative to an INJECTED
clock that is far from the real wall clock, so a kernel that silently reads `datetime.now()` fails.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.mainai_grandmaster.types import (
    Agent, Board, BoardStatus, Branch, CandidateSHA, EvidencePointer, Examination, Execution,
    ProposedMove, Task, Workspace, WorkspaceMode,
)

# Two years away from the real clock in BOTH directions is exercised by test_p1_clock.
FIXED_NOW = datetime(2031, 3, 14, 9, 0, tzinfo=timezone.utc)
SHA_EXAM = "e" * 40
SHA_BASE = "b" * 40
SHA_CHILD = "c" * 40


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **kw) -> None:
        self.at = self.at + timedelta(**kw)


def ev(source="execution_ledger", *, at=None, authoritative=True, mutable=True, ttl=timedelta(hours=1)):
    at = at or FIXED_NOW
    return EvidencePointer(source=source, source_ref=f"{source}:ref", observed_at=at, authoritative=authoritative,
                           mutable=mutable, expires_at=(at + ttl) if mutable else None)


def agent(key, status=BoardStatus.IDLE, roles=("builder",), caps=("repo_edit",)):
    return Agent(key, frozenset(roles), frozenset(caps), status, ev())


def task(key, status=BoardStatus.IDLE, role="builder", deps=()):
    return Task(key, key, role, frozenset({"repo_edit"}), status, frozenset(deps), frozenset(), None, ev("task_ledger"))


def execution(eid, task_key, agent_key, status=BoardStatus.IDLE, workspace=None, branch=None, sha=None):
    return Execution(eid, task_key, agent_key, status, workspace, branch, sha, ev())


def workspace(key, path=None, mode=WorkspaceMode.MUTABLE, owner=None, execution_id=None, branch=None):
    return Workspace(key, path or f"/worktrees/{key}", mode, owner, execution_id, branch, SHA_BASE, ev("filesystem"))


def mv(move_id, task_key, agent_key, workspace_key, *, branch=None, base=SHA_BASE, child=None, action="build"):
    return ProposedMove(move_id, task_key, agent_key, move_id, workspace_key, branch or f"{agent_key}/{task_key}/{move_id}",
                        base, action=action, child_of_sha=child)


def base_board() -> Board:
    """Claude examining SHA_EXAM on branch feature/x in read-only /worktrees/exam; Cursor/Codex idle;
    four independent idle tasks with one pre-registered (not yet started) execution per agent/task pair."""
    b = Board(refreshed_at=FIXED_NOW)
    b.agents = {
        "claude": agent("claude", BoardStatus.RUNNING, roles=("examiner", "builder")),
        "cursor": agent("cursor"),
        "codex": agent("codex"),
    }
    b.tasks = {k: task(k) for k in ("t1", "t2", "t3", "t4")}
    b.tasks["exam-x"] = task("exam-x", BoardStatus.UNDER_EXAMINATION, role="examiner")
    # Examiners work in a DETACHED read-only checkout of the SHA (no branch) — so builder-move rejections must come
    # from the examination/freeze rules, not from an incidental branch collision with the examiner's execution.
    b.executions = {"exec-exam": execution("exec-exam", "exam-x", "claude", BoardStatus.RUNNING, "exam", None, SHA_EXAM)}
    for t in ("t1", "t2", "t3", "t4"):
        for a in ("cursor", "codex"):
            b.executions[f"{a}-{t}"] = execution(f"{a}-{t}", t, a)
    b.workspaces = {"exam": workspace("exam", "/worktrees/exam", WorkspaceMode.READ_ONLY, "claude", "exec-exam", None)}
    for i in range(1, 7):
        b.workspaces[f"ws{i}"] = workspace(f"ws{i}")
    b.branches = {"feature/x": Branch("feature/x", SHA_EXAM, SHA_EXAM, "claude", ev("github"))}
    b.candidates = {SHA_EXAM: CandidateSHA(SHA_EXAM, "build-x", "feature/x", BoardStatus.UNDER_EXAMINATION, False, ev("verification_registry"))}
    b.tasks["build-x"] = task("build-x", BoardStatus.COMPLETE_UNVERIFIED)
    b.executions["exec-build-x"] = execution("exec-build-x", "build-x", "codex", BoardStatus.COMPLETE_UNVERIFIED, "ws-build-x", "feature/x", SHA_EXAM)
    b.examinations = {"exam-1": Examination("exam-1", SHA_EXAM, "claude", "exam", BoardStatus.UNDER_EXAMINATION, None, ev("verification_registry"))}
    return b


@pytest.fixture
def clock():
    return Clock(FIXED_NOW)


@pytest.fixture
def board():
    return base_board()
