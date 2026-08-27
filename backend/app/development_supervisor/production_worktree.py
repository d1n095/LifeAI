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
truth -- durable truth is `mainai_checkpoints`/task status, entirely unchanged).

WRITE AUTHORIZATION for this shared directory is still real, but it is NOT
`MainAITaskWorktree` / `.mainai_worktree_owner.json`. Those are PER-JOB recovery identities;
stamping them onto a PER-GOAL path creates an invalid lifecycle when task B overwrites task
A's marker. Operator instead verifies the active `supervisor_goal_leases` claim plus this
module's canonical `goal_worktree_path` / `goal_branch_name` formulas (see
`OperatorContext.supervisor_goal_id` / lease fields). Recovery inspectors continue to ignore
this directory class unless a genuine per-job recovery worktree exists elsewhere."""

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


def reset_goal_worktree_to_clean_head(repository_root: Path) -> None:
    """Discards any uncommitted changes (staged, unstaged, and untracked) in a goal's shared
    worktree, restoring it to exactly its last real commit.

    WHY THIS EXISTS: this worktree is deliberately reused across every task attempted under
    one authorized goal, potentially across many separate worker ticks (see this module's own
    docstring on why it is keyed by goal_id, not job_id) -- but a task that only got partway
    through its own plan before deferring, failing, or the whole tick raising (e.g. a
    `patch_file`/`create_file` step succeeded but the plan's own later `commit_scoped_changes`
    step never ran) leaves those changes sitting UNCOMMITTED in this SHARED directory. Every
    `app.development_operator.service.write_file()` call already fails closed on an unexpected
    `before_sha256` mismatch, so a later, unrelated task can never have its own write silently
    corrupted by that leftover mess -- but without this function, it WOULD be incorrectly
    blocked/failed by it indefinitely, since nothing else ever cleans the shared worktree
    between distinct task attempts. That is an availability defect (one task's partial failure
    permanently wedges every later task under the same goal), not a security one, but a real
    one: found via targeted adversarial self-review of PR #148's own new worktree-reuse design
    after it merged.

    CALLER CONTRACT -- this MUST be called only when a genuinely NEW task attempt is
    beginning (a job just freshly claimed from `queued`), NEVER when the SAME task's own
    still-valid claim is being resumed across two ticks: a resume's entire point is to
    continue exactly where that task's own prior attempt left off, uncommitted changes
    included. See `app/development_supervisor/production_entry.py`'s own `prepare_context()`
    -- it calls this only in the fresh-claim branch, never the resume branch.

    Never touches COMMITTED history: `git reset --hard HEAD` moves the working tree and index
    back to the current branch tip only (never rewrites `HEAD` itself), so every earlier
    task's own SUCCESSFULLY COMMITTED work remains exactly as committed. `git clean -fd`
    additionally removes untracked files/directories a `create_file` step may have left behind
    without ever being staged -- never touches `.git/` itself."""
    _run_git(["reset", "--hard", "HEAD"], cwd=repository_root)
    _run_git(["clean", "-fd"], cwd=repository_root)
