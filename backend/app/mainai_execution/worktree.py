"""Minimal per-task/per-attempt worktree isolation — the founder-approved V0.2 prerequisite
(migration 0033's own module docstring has the full architecture rationale). Two facts drove
this module's shape:

1. V0.1's `_handle_repo_edit()` (execution_job.py) writes directly onto the single shared
   on-disk backend checkout the worker process itself runs from — no isolation, and a real
   concurrency hazard if two attempts ever touch the same files. This module is what
   INTRODUCES a real, separate `git` checkout per (task, job/attempt), created from a
   VERIFIED base SHA (`github_client.get_ref()`, never trusted from a stale local ref) on a
   deterministic task-scoped branch — never the shared checkout, never mainline/protected.

2. The backend/worker container filesystem is NOT persistent (only `lifeai_uploads` is a
   named volume — see docker-compose.vps.yml). A worktree directory can therefore NEVER be
   trusted by path or DB row alone: an unrelated stale directory could coincidentally exist
   at a reused path after a container replacement. Every function below that touches a
   worktree's file CONTENT (commit, status, push) requires `verify_worktree_ownership()` to
   have passed first — it reads back `.mainai_worktree_owner.json`, written inside the
   worktree at creation time, and compares it field-for-field against the DB row's
   `marker_token`. Missing, unreadable, or mismatched: treated as "nothing here belongs to
   this task", never assumed either way. This is what lets recovery degrade safely whether
   the container survived (marker intact, salvage proceeds) or was replaced (marker gone,
   correctly falls back to durable checkpoint + remote GitHub state).

Real local git commit→push is genuine here (not the GitHub Git-Data-API-only model
`_finalize_repo_edit()` uses) specifically so LOCAL_UNCOMMITTED_WORK and
LOCAL_COMMITTED_NOT_PUSHED are real, independently verifiable states — a local commit is a
real object addressable via `git cat-file -t <sha>` even with zero network, distinct from a
push having happened. `push_worktree_branch()` is always a plain, non-force push: origin
either fast-forwards cleanly or git itself rejects it — a genuine divergence surfaces as an
error for the recovery classifier to see (CONFLICTED_STATE), never a silent overwrite.

Zero new lease/fencing/heartbeat mechanism: nothing here grants write authority. Callers are
expected to hold a valid `mainai_jobs` lease (see app/jobs/mainai_job_lease.py) for the whole
duration these functions run; `lease_generation` is recorded on the worktree row purely as a
durable ownership fact for later recovery inspection, not enforced here as a live check."""

import json
import os
import re
import secrets
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations.github_client import GitHubClient
from app.mainai_execution.publication_errors import (
    ApprovedBaseMovedError,
    GitHubApiBoundaryError,
    PublicationTimeoutError,
    PushRejectedError,
    map_publication_boundary_error,
)
from app.models.mainai_job import MainAIJob
from app.models.mainai_execution import MainAITask
from app.models.mainai_recovery import (
    ACTIVATION_INTEGRATION_BRANCH,
    PROTECTED_BRANCH_PREFIXES,
    PROTECTED_BRANCHES,
    MainAITaskWorktree,
    MainAITaskWorktreeStatus,
    is_protected_branch,
)

WORKTREE_ROOT = Path("/tmp/mainai-worktrees")
MARKER_FILENAME = ".mainai_worktree_owner.json"
BASE_BRANCH = "claude/det-kommer-mer-879lcm"
COMMIT_AUTHOR = ("MainAI Executor", "mainai-executor@lifeai.local")
_GIT_TIMEOUT_SECONDS = 60


_SHA40 = re.compile(r"^[0-9a-f]{40}$")


def activation_base() -> tuple[str, str]:
    """Activation-mode base: (integration branch, Founder-approved base SHA). Fail closed.

    Requires, with no fallback of any kind:
    - `GITHUB_WORKTREE_BASE_BRANCH` explicitly set, non-empty, and NOT the legacy default;
    - the branch to be exactly the Founder-supervised integration branch
      (`ACTIVATION_INTEGRATION_BRANCH`), which is protected (never main/master/legacy/other
      `mainai/` branches);
    - `GITHUB_PR_BASE_BRANCH` explicitly set and equal to the worktree base;
    - `MAINAI_ACTIVATION_APPROVED_BASE_SHA` a 40-hex lowercase commit SHA.
    """
    settings = get_settings()
    worktree_base = (settings.github_worktree_base_branch or "").strip()
    pr_base = (settings.github_pr_base_branch or "").strip()
    approved = (settings.mainai_activation_approved_base_sha or "").strip()
    if not worktree_base:
        raise ActivationBaseConfigurationError("activation mode requires GITHUB_WORKTREE_BASE_BRANCH; it is empty.")
    if worktree_base == BASE_BRANCH:
        raise ActivationBaseConfigurationError(
            f"activation mode refuses the legacy default base {BASE_BRANCH!r}; configure the integration branch explicitly."
        )
    if (
        worktree_base != ACTIVATION_INTEGRATION_BRANCH
        or not worktree_base.startswith(PROTECTED_BRANCH_PREFIXES)
        or not is_protected_branch(worktree_base)
    ):
        raise ActivationBaseConfigurationError(
            f"activation base {worktree_base!r} is not the protected integration branch {ACTIVATION_INTEGRATION_BRANCH!r}."
        )
    if not pr_base:
        raise ActivationBaseConfigurationError("activation mode requires GITHUB_PR_BASE_BRANCH; it is empty.")
    if pr_base != worktree_base:
        raise ActivationBaseConfigurationError(
            f"activation PR base {pr_base!r} differs from worktree base {worktree_base!r}."
        )
    if not _SHA40.match(approved):
        raise ActivationBaseConfigurationError(
            "activation mode requires MAINAI_ACTIVATION_APPROVED_BASE_SHA (40 lowercase hex characters)."
        )
    return worktree_base, approved


def execution_worktree_base_branch() -> str:
    """Configured worktree fetch base. Outside activation mode it falls back to the historical
    default so existing tests that seed `BASE_BRANCH` keep matching production-default settings.
    In activation mode there is no fallback: see `activation_base()`."""
    if get_settings().mainai_activation_mode:
        return activation_base()[0]
    configured = (get_settings().github_worktree_base_branch or "").strip()
    return configured or BASE_BRANCH


def execution_pr_base_branch() -> str:
    """Configured PR target. Empty setting means the worktree base (legacy mode only)."""
    if get_settings().mainai_activation_mode:
        return activation_base()[0]
    configured = (get_settings().github_pr_base_branch or "").strip()
    return configured or execution_worktree_base_branch()


def protected_execution_branches() -> frozenset[str]:
    return frozenset({*PROTECTED_BRANCHES, BASE_BRANCH, execution_worktree_base_branch(), execution_pr_base_branch()})


class WorktreeError(RuntimeError):
    """Base for all worktree-layer failures. Callers must treat any of these as "recovery
    cannot proceed automatically" — never as "treat it as if nothing happened"."""


class WorktreeTimeoutError(WorktreeError):
    """A git subprocess exceeded its timeout. Local operations may retry; at the push/grant
    boundary this is mapped to a typed fail-closed publication timeout."""


class ActivationBaseConfigurationError(WorktreeError, ApprovedBaseMovedError):
    """Activation-mode base branch / approved base SHA is missing, stale, or has moved.
    A hard NO-GO: nothing falls back to another branch or to a moving tip."""


class WorktreeOwnershipError(WorktreeError):
    """The on-disk marker is missing, unreadable, or does not match the DB row. Per the
    founder's explicit instruction: fail closed -> manual_review_required, never guess."""


class WorktreePushRejected(WorktreeError, PushRejectedError):
    """git itself rejected a non-force push. The remote did not accept this publication.
    Typed origin=`push`; recovery may still inspect refs for CONFLICTED_STATE."""


def task_branch_name(task_id: uuid.UUID) -> str:
    """The single canonical task-scoped branch name formula. `_finalize_repo_edit()`
    (execution_job.py) uses this exact same `claude/mainai-task-<task_id>` shape independently
    today (V0.1 predates this module) — kept identical on purpose so a task's worktree branch
    and its eventual GitHub PR branch are always the same branch, never two different ones."""
    return f"claude/mainai-task-{task_id}"


_GIT_CONFIG_INJECTION_KEYS = frozenset(
    {
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
    }
)


def strip_git_config_injection(env: dict[str, str]) -> dict[str, str]:
    """Remove Git config-injection vectors (GIT_CONFIG_COUNT / KEY / VALUE / PARAMETERS).

    Does not implement a second credential transport. Token-in-URL (CRED-1) stays FAIL.
    """
    cleaned = dict(env)
    for key in _GIT_CONFIG_INJECTION_KEYS:
        cleaned.pop(key, None)
    for key in list(cleaned):
        if key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"):
            cleaned.pop(key, None)
    return cleaned


def sanitized_git_subprocess_env() -> dict[str, str]:
    """Process env for git subprocesses, with config-injection keys removed."""
    env = {k: str(v) for k, v in os.environ.items() if v is not None}
    return strip_git_config_injection(env)


def _run_git(args: list[str], *, cwd: Path, timeout: int = _GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    """All git subprocess calls funnel through here. Mirrors the exact
    subprocess.TimeoutExpired handling lesson verify.py/_run_pytest() already established
    (V0.1 hardening pass): a killed subprocess's .stdout/.stderr are not reliably `str` even
    with text=True, and an uncaught TimeoutExpired must never propagate as a bare crash."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=sanitized_git_subprocess_env(),
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        raise WorktreeTimeoutError(
            f"git {' '.join(args)} timed out after {timeout}s: stdout={stdout[:500]!r} stderr={stderr[:500]!r}"
        ) from exc
    if result.returncode != 0:
        raise WorktreeError(f"git {' '.join(args)} failed ({result.returncode}): {result.stderr[:1000]}")
    return result


def _authed_remote_url(repo: str, token: str) -> str:
    return f"https://x-access-token:{token}@github.com/{repo}.git"


def _marker_path(path: Path) -> Path:
    return path / MARKER_FILENAME


def verify_worktree_ownership(worktree: MainAITaskWorktree) -> bool:
    """The sole trust boundary between "a directory exists at worktree.path" and "this task's
    code is allowed to treat that directory's contents as its own work". Never raises -- any
    failure to read/parse/match is a plain False, exactly like a genuine mismatch."""
    marker_file = _marker_path(Path(worktree.path))
    try:
        raw = marker_file.read_text(encoding="utf-8")
        marker = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return False
    return (
        marker.get("task_id") == str(worktree.task_id)
        and marker.get("job_id") == str(worktree.job_id)
        and marker.get("marker_token") == worktree.marker_token
    )


def _require_ownership(worktree: MainAITaskWorktree) -> None:
    if not verify_worktree_ownership(worktree):
        raise WorktreeOwnershipError(
            f"Worktree ownership marker missing or mismatched for task={worktree.task_id} job={worktree.job_id} "
            f"path={worktree.path} — refusing to trust local content."
        )


def create_task_worktree_sync(
    db: Session, *, task: MainAITask, job: MainAIJob, lease_generation: int, executor_id: str, base_sha: str
) -> MainAITaskWorktree:
    """The synchronous core of `create_task_worktree()` below, split out so a caller that
    already has a verified `base_sha` in hand (fetched once, upfront, via its own `await
    GitHubClient().get_ref(BASE_BRANCH)`) can create a worktree from a plain, non-async
    context. This is exactly the situation `app/development_supervisor/production_entry.py`'s
    real `prepare_context` closure is in: `WorkBinding.prepare_context` is a plain
    `Callable[[MainAITask, MainAIJob], object]`, called synchronously (never awaited) from
    inside `run_supervisor()` — so the one network call this whole function needs must happen
    OUTSIDE it, once per Supervisor tick, not once per task.

    Still fails closed exactly like the async wrapper: GitHub write must be configured/
    enabled, and the branch name is re-verified never protected/mainline before any
    filesystem side effect."""
    existing = db.query(MainAITaskWorktree).filter(MainAITaskWorktree.job_id == job.id).one_or_none()
    if existing is not None:
        return existing

    settings = get_settings()
    client = GitHubClient()
    if not settings.github_write_enabled or not client.is_configured():
        raise WorktreeError(
            "GITHUB_WRITE_ENABLED är av eller GITHUB_TOKEN/GITHUB_REPO saknas — kan inte skapa en verklig, "
            "push-bar worktree. MainAI stannar i förslagsläge (se app/mainai_execution/execution_job.py)."
        )

    if settings.mainai_activation_mode:
        _branch, approved_sha = activation_base()
        if base_sha != approved_sha:
            raise ActivationBaseConfigurationError(
                f"worktree base_sha {base_sha!r} is not the Founder-approved base SHA {approved_sha!r}."
            )

    branch = task_branch_name(task.id)
    if branch in protected_execution_branches() or is_protected_branch(branch):
        # Structurally unreachable given task_branch_name()'s fixed prefix, but checked
        # explicitly anyway -- the DB CHECK constraint is the real backstop, this is defense
        # in depth before any filesystem or network side effect happens at all.
        raise WorktreeError(f"Refusing to use protected/mainline branch '{branch}' as a task worktree's working branch.")

    path = WORKTREE_ROOT / str(job.id)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)

    marker_token = secrets.token_hex(16)
    try:
        _run_git(["init", "-q"], cwd=path)
        _run_git(["config", "user.email", COMMIT_AUTHOR[1]], cwd=path)
        _run_git(["config", "user.name", COMMIT_AUTHOR[0]], cwd=path)
        _run_git(["remote", "add", "origin", _authed_remote_url(settings.github_repo, settings.github_token)], cwd=path)
        _run_git(["fetch", "--depth", "1", "origin", base_sha], cwd=path, timeout=120)
        _run_git(["checkout", "-b", branch, "FETCH_HEAD"], cwd=path)

        # The marker file itself must never look like "uncommitted work" to
        # commit_worktree_changes()/worktree_git_status() -- local-only exclude (never
        # committed, never pushed), not .gitignore (which would itself be a tracked-file
        # change landing in every task's commits).
        (path / ".git" / "info" / "exclude").write_text(f"{MARKER_FILENAME}\n", encoding="utf-8")

        marker = {
            "task_id": str(task.id),
            "job_id": str(job.id),
            "marker_token": marker_token,
            "lease_generation": lease_generation,
            "created_at": datetime.utcnow().isoformat(),
        }
        _marker_path(path).write_text(json.dumps(marker), encoding="utf-8")
    except Exception:
        shutil.rmtree(path, ignore_errors=True)
        raise

    worktree = MainAITaskWorktree(
        task_id=task.id,
        owner_id=task.owner_id,
        job_id=job.id,
        lease_generation=lease_generation,
        executor_id=executor_id,
        repo=settings.github_repo,
        base_sha=base_sha,
        branch=branch,
        path=str(path),
        marker_token=marker_token,
        status=MainAITaskWorktreeStatus.active,
        created_at=datetime.utcnow(),
    )
    db.add(worktree)
    db.flush()
    return worktree


async def create_task_worktree(
    db: Session, *, task: MainAITask, job: MainAIJob, lease_generation: int, executor_id: str
) -> MainAITaskWorktree:
    """Thin async wrapper: resolves `base_sha` via the one real network call
    (`GitHubClient.get_ref()`), then delegates everything else to the synchronous
    `create_task_worktree_sync()` above. Behavior is unchanged from before the sync/async
    split — existing callers (app/mainai_execution/execution_job.py) are unaffected."""
    settings = get_settings()
    client = GitHubClient()
    if not settings.github_write_enabled or not client.is_configured():
        raise WorktreeError(
            "GITHUB_WRITE_ENABLED är av eller GITHUB_TOKEN/GITHUB_REPO saknas — kan inte skapa en verklig, "
            "push-bar worktree. MainAI stannar i förslagsläge (se app/mainai_execution/execution_job.py)."
        )
    if settings.mainai_activation_mode:
        # Bind to the Founder-approved SHA, never to whatever the branch tip is now. A moved
        # tip is a NO-GO (the approval no longer describes what would be built on).
        base_branch, approved_sha = activation_base()
        try:
            tip = await client.get_ref(base_branch)
        except Exception as exc:
            raise map_publication_boundary_error(exc) from exc
        if tip != approved_sha:
            raise ActivationBaseConfigurationError(
                f"integration branch {base_branch!r} tip {tip!r} is not the approved base SHA {approved_sha!r}; refusing to build."
            )
        base_sha = approved_sha
    else:
        try:
            base_sha = await client.get_ref(execution_worktree_base_branch())
        except Exception as exc:
            raise map_publication_boundary_error(exc) from exc
    return create_task_worktree_sync(
        db, task=task, job=job, lease_generation=lease_generation, executor_id=executor_id, base_sha=base_sha
    )


def git_changed_paths_no_renames(*, cwd: Path, base_sha: str, head: str = "HEAD") -> list[str]:
    """Changed paths between `base_sha` and `head`, with rename collapsing disabled.

    Equivalent to `git diff --name-only --no-renames`. A rename of an out-of-scope
    file onto an authorized target therefore exposes the deleted source path
    (TA-10), not only the destination.
    """
    if not base_sha:
        raise WorktreeError("refusing to compute changed files without a base_sha.")
    result = _run_git(["diff", "--name-only", "--no-renames", "-z", f"{base_sha}..{head}"], cwd=cwd)
    return [entry for entry in result.stdout.split("\0") if entry]


def path_exists_in_git_revision(*, cwd: Path, revision: str, path: str) -> bool | None:
    """True if `path` exists as a blob at `revision`. False if git answers "missing".

    Returns None when git cannot be queried (not a repo, timeout, unexpected error)
    so callers can fail closed rather than treat an unknown tree as empty.
    """
    if not revision or not path:
        return None
    try:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{revision}:{path}"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            env=sanitized_git_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode == 0:
        return True
    stderr = (result.stderr or "").lower()
    if "not a git repository" in stderr or "not a git repo" in stderr:
        return None
    if result.returncode in {1, 128}:
        return False
    return None


def path_exists_at_base(worktree: MainAITaskWorktree, path: str) -> bool:
    """Whether `path` exists at the worktree's verified base SHA (not the current tree)."""
    _require_ownership(worktree)
    if not worktree.base_sha:
        raise WorktreeError("worktree has no base_sha — refusing to test path existence.")
    exists = path_exists_in_git_revision(cwd=Path(worktree.path), revision=worktree.base_sha, path=path)
    if exists is None:
        raise WorktreeError(
            f"cannot determine whether {path!r} exists at base_sha {worktree.base_sha} — refusing to lift edit authority."
        )
    return exists


def worktree_changed_files(worktree: MainAITaskWorktree) -> list[str]:
    """Repo-root relative paths that differ between the verified base SHA and HEAD.

    Used immediately before remote publication so the actual Git changed-file set
    can be compared against structured editable targets. Rename collapsing is
    disabled so a deleted/renamed source path cannot hide behind a destination
    that happens to be authorized. The ownership marker is excluded via
    `.git/info/exclude` and never appears here.
    """
    _require_ownership(worktree)
    if not worktree.base_sha:
        raise WorktreeError("worktree has no base_sha — refusing to compute changed files.")
    return git_changed_paths_no_renames(cwd=Path(worktree.path), base_sha=worktree.base_sha)


def worktree_git_status(worktree: MainAITaskWorktree) -> dict:
    """Read-only snapshot used by the recovery inspector — never mutates anything. Returns
    ownership_verified=False (with every other field left at its safe default) rather than
    raising, so a caller doing a broad inspection sweep over many worktrees doesn't need a
    try/except per row."""
    if not verify_worktree_ownership(worktree):
        return {"ownership_verified": False, "has_uncommitted_changes": False, "local_head_sha": None}

    path = Path(worktree.path)
    status_result = _run_git(["status", "--porcelain"], cwd=path)
    head_result = _run_git(["rev-parse", "HEAD"], cwd=path)
    return {
        "ownership_verified": True,
        "has_uncommitted_changes": bool(status_result.stdout.strip()),
        "local_head_sha": head_result.stdout.strip(),
    }


def fetch_remote_branch(worktree: MainAITaskWorktree) -> str | None:
    """Fetches the CURRENT remote tip of the worktree's own branch into this local checkout's
    object database (read-only in effect -- never touches the local branch ref or working
    tree) so is_ancestor() below has something real to compare against. Necessary because
    create_task_worktree() makes a shallow, single-commit checkout (`--depth 1`) that never
    contains anyone else's commits by default. Returns None if the branch does not exist on
    the remote at all (nothing to compare against -- not an error)."""
    _require_ownership(worktree)
    path = Path(worktree.path)
    result = subprocess.run(
        ["git", "-C", str(path), "fetch", "origin", worktree.branch],
        cwd=str(path),
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        env=sanitized_git_subprocess_env(),
    )
    if result.returncode != 0:
        return None
    return _run_git(["rev-parse", "FETCH_HEAD"], cwd=path).stdout.strip()


def is_ancestor(worktree: MainAITaskWorktree, ancestor_sha: str) -> bool | None:
    """True if `ancestor_sha` is a real ancestor of (or equal to) the worktree's local HEAD --
    i.e. the local branch is a normal fast-forward continuation of that commit, not a genuine
    divergence. Used by the recovery classifier to tell "hasn't pushed yet" (remote tip is an
    ancestor of local HEAD) apart from a real conflict (it is not) without guessing from a bare
    SHA mismatch. Returns None (never a bare crash) if `ancestor_sha` is not even present in
    this local checkout's object database -- a truncated fetch or a genuinely unrelated commit,
    itself signal the classifier should treat as unsafe rather than as "not an ancestor"."""
    _require_ownership(worktree)
    path = Path(worktree.path)
    git_env = sanitized_git_subprocess_env()
    cat_file = subprocess.run(
        ["git", "-C", str(path), "cat-file", "-e", ancestor_sha],
        capture_output=True,
        text=True,
        env=git_env,
    )
    if cat_file.returncode != 0:
        return None
    result = subprocess.run(
        ["git", "-C", str(path), "merge-base", "--is-ancestor", ancestor_sha, "HEAD"],
        capture_output=True,
        text=True,
        env=git_env,
    )
    return result.returncode == 0


def commit_worktree_changes(db: Session, worktree: MainAITaskWorktree, *, message: str) -> str | None:
    """Stages and commits everything currently in the worktree. Returns the new commit SHA, or
    None if there was nothing to commit (an empty commit is never created — "nothing changed"
    is itself meaningful signal for the recovery classifier, not an error)."""
    _require_ownership(worktree)
    path = Path(worktree.path)

    _run_git(["add", "-A"], cwd=path)
    # After `add -A` the worktree is clean and anything pending shows up staged, so a plain
    # `status --porcelain` (no `--cached` -- not a real git status flag) is enough to tell
    # "nothing changed" from "something changed".
    status_result = _run_git(["status", "--porcelain"], cwd=path)
    if not status_result.stdout.strip():
        return None

    _run_git(["commit", "-q", "-m", message], cwd=path)
    sha = _run_git(["rev-parse", "HEAD"], cwd=path).stdout.strip()

    worktree.current_commit = sha
    db.add(worktree)
    db.flush()
    return sha


async def push_worktree_branch(worktree: MainAITaskWorktree) -> str:
    """Plain, non-force push of the worktree's branch to origin. Never `--force` /
    `--force-with-lease` under any circumstance (the founder's explicit requirement) -- a
    non-fast-forward rejection is surfaced as WorktreePushRejected, not silently retried with
    force, since a real divergence is exactly the case that must stop and ask, not overwrite."""
    _require_ownership(worktree)
    path = Path(worktree.path)

    try:
        _run_git(["push", "origin", f"{worktree.branch}:{worktree.branch}"], cwd=path, timeout=120)
    except WorktreeTimeoutError as exc:
        raise PublicationTimeoutError(type(exc).__name__) from exc
    except WorktreeError as exc:
        # Any failed push is a publication-boundary failure. Do not classify from git stderr.
        raise WorktreePushRejected(type(exc).__name__) from exc

    local_sha = _run_git(["rev-parse", "HEAD"], cwd=path).stdout.strip()
    client = GitHubClient()
    try:
        remote_sha = await client.get_ref(worktree.branch)
    except Exception as exc:
        raise map_publication_boundary_error(exc) from exc
    if remote_sha != local_sha:
        raise GitHubApiBoundaryError("PushSuccessRemoteMismatch")
    return remote_sha


def rebind_worktree_to_job(db: Session, worktree: MainAITaskWorktree, *, new_job_id: uuid.UUID, new_lease_generation: int) -> MainAITaskWorktree:
    """Durable ownership transfer for a salvage/takeover: the worktree's DB row and its
    on-disk marker are BOTH rewritten to point at the new attempt's job_id, with a freshly
    minted marker_token (never reused -- a stale marker_token surviving a rebind would defeat
    the whole point of binding trust to a specific attempt). Requires the CURRENT ownership to
    already verify -- rebinding something that doesn't provably belong to the OLD job in the
    first place would just be forging a NEW false claim of ownership, not a genuine transfer."""
    _require_ownership(worktree)
    path = Path(worktree.path)

    new_marker_token = secrets.token_hex(16)
    marker = {
        "task_id": str(worktree.task_id),
        "job_id": str(new_job_id),
        "marker_token": new_marker_token,
        "lease_generation": new_lease_generation,
        "created_at": datetime.utcnow().isoformat(),
    }
    _marker_path(path).write_text(json.dumps(marker), encoding="utf-8")

    worktree.job_id = new_job_id
    worktree.lease_generation = new_lease_generation
    worktree.marker_token = new_marker_token
    db.add(worktree)
    db.flush()
    return worktree


def release_worktree(db: Session, worktree: MainAITaskWorktree, *, status: MainAITaskWorktreeStatus) -> None:
    """Ends a worktree's active lifecycle. The local directory is deliberately removed
    (ephemeral by design, matching the container's own non-persistent filesystem) -- the
    DURABLE record of what actually happened lives in the DB row plus any pushed git history,
    never in the continued existence of this directory."""
    if status == MainAITaskWorktreeStatus.active:
        raise WorktreeError("release_worktree() cannot set status back to 'active'.")
    worktree.status = status
    worktree.released_at = datetime.utcnow()
    db.add(worktree)
    db.flush()
    shutil.rmtree(worktree.path, ignore_errors=True)
