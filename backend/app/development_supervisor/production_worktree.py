"""A real, isolated LOCAL git worktree for the production Supervisor entry point -- distinct
from `app/mainai_execution/worktree.py`'s job-scoped, GitHub-push-capable worktree model, and
deliberately so.

`SupervisorScope.repository_identity` is a single fixed string for an entire `run_supervisor()`
call -- checked for EXACT equality against every dispatched task's own `OperatorContext.
repository_root.resolve()` (see `bind_execution_context()`'s escape check). `run_supervisor()`
can dispatch several DIFFERENT tasks within one bounded call (`SupervisorBounds.max_jobs`), so
a per-JOB worktree directory (job ids are only generated inside `dispatch_ready_task()`, not
known ahead of the call) cannot satisfy that fixed, pre-declared identity. A per-GOAL
directory can: it is fully deterministic from `goal.id` alone, computable before
`run_supervisor()` is even called, and correctly shared -- and correctly reused, including
across separate ticks -- by every task attempted under that one authorized goal.

LOCAL ONLY, on purpose: this module never fetches from or pushes to a remote. It creates a
real `git worktree add` off the worker process's own on-disk checkout (never writes directly
onto that shared checkout -- the exact hazard `app/mainai_execution/worktree.py`'s own module
docstring describes) at that checkout's current local HEAD. Remote write remains a separate,
NOT-YET-authorized capability: `OperatorContext.remote_write_authorized` defaults False and is
never set True by this production entry, and `push_branch` (the one REMOTE_WRITE capability in
`DEVELOPMENT_CAPABILITIES`) is never included in the capability set this module hands to the
Operator regardless of what an envelope authorizes -- expanding into real remote pushes is
explicitly staged as a SEPARATE, later founder act, not something this "smallest coherent
implementation" grants itself.

No DB row, no ownership marker, no persistent-volume durability story: unlike the job-scoped
model, a lost local checkout here is always cleanly re-creatable (nothing in it is durable
truth -- durable truth is `mainai_checkpoints`/task status, entirely unchanged), so there is
nothing to verify ownership of and nothing recovery needs to inspect after a crash."""

import subprocess
from pathlib import Path

WORKTREE_ROOT = Path("/tmp/mainai-supervisor-goal-worktrees")
_GIT_TIMEOUT_SECONDS = 60


class ProductionWorktreeError(RuntimeError):
    pass


def _run_git(args: list[str], *, cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=_GIT_TIMEOUT_SECONDS, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise ProductionWorktreeError(f"git {' '.join(args)} timed out") from exc
    if result.returncode != 0:
        raise ProductionWorktreeError(f"git {' '.join(args)} failed ({result.returncode}): {result.stderr[:1000]}")
    return result.stdout.strip()


def goal_branch_name(goal_id) -> str:
    return f"claude/mainai-supervisor-goal-{goal_id}"


def goal_worktree_path(goal_id) -> Path:
    return WORKTREE_ROOT / str(goal_id)


def worker_source_repo_root() -> Path:
    """This file lives at <repo>/backend/app/development_supervisor/production_worktree.py --
    three parents up is the repo root, exactly the same convention
    app/mainai_execution/execution_job.py's own `_repo_root()` already establishes."""
    return Path(__file__).resolve().parents[3]


def ensure_goal_worktree_sync(*, goal_id, source_repo_root: Path) -> tuple[Path, str, str]:
    """Returns (repository_root, expected_base_sha, expected_branch) -- creating a fresh local
    `git worktree add` off `source_repo_root`'s current HEAD the first time this goal is
    worked on, or reusing (and reporting the CURRENT local HEAD of) the existing one on every
    later call for the same goal, including across separate worker ticks. Never raises for "no
    worktree yet" -- that is the normal first-call case, always handled by creating one."""
    path = goal_worktree_path(goal_id)
    branch = goal_branch_name(goal_id)

    if path.is_dir() and (path / ".git").exists():
        head = _run_git(["rev-parse", "HEAD"], cwd=path)
        return path, head, branch

    path.parent.mkdir(parents=True, exist_ok=True)
    # A prior container instance's admin metadata can outlive the (non-persistent) worktree
    # directory itself -- prune first so `worktree add` never fails on a dangling registration.
    _run_git(["worktree", "prune"], cwd=source_repo_root)

    base_sha = _run_git(["rev-parse", "HEAD"], cwd=source_repo_root)
    existing_branch = _run_git(["branch", "--list", branch], cwd=source_repo_root)
    if existing_branch.strip():
        _run_git(["worktree", "add", str(path), branch], cwd=source_repo_root)
    else:
        _run_git(["worktree", "add", "-b", branch, str(path), base_sha], cwd=source_repo_root)
    return path, base_sha, branch
