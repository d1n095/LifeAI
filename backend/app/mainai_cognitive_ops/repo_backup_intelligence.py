"""Repo / Backup Intelligence. See docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md.

LOCAL COMMIT != REMOTE COMMIT. LOCAL BRANCH != PUSHED BRANCH. COMMITTED != BACKED UP. VERIFIED
LOCALLY != AVAILABLE REMOTELY. REMOTE EXISTS != REMOTE IS CURRENT. DEFAULT BRANCH != CURRENT
DEVELOPMENT TRUTH.

Real, read-only git introspection via `subprocess` -- fixed argv lists only, `shell=False`
always, no string interpolation into the command, so this can never become command injection
regardless of what a caller passes as `repo_path`. Uses ONLY already-fetched local
remote-tracking refs (never performs a live network fetch itself) -- this means "remote SHA"
here reflects the state as of the last fetch, which is exactly the REMOTE EXISTS != REMOTE IS
CURRENT distinction this module is required to make explicit, not hide.

PUSH/BACKUP != MERGE. MERGE != DEPLOY. REMOTE WRITE != DEPLOY AUTHORITY. This module is
read-only by construction -- it contains no function that mutates git state, and the
structural-purity test for this package asserts that."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

from app.mainai_cognitive_ops.types import RemoteSyncState


class GitIntrospectionError(RuntimeError):
    """Raised when a read-only git command itself fails (e.g. not a git repo, ref does not
    exist) -- never silently swallowed into a fabricated "clean" state."""


def _run_git(repo_path: str, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, *args], capture_output=True, text=True, timeout=10, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise GitIntrospectionError(f"git {' '.join(args)} failed in {repo_path}: {exc}") from exc
    # rstrip only the trailing newline -- `.strip()` would eat the leading status-code column
    # of `git status --porcelain`'s first line, corrupting `get_dirty_files()`'s own parsing.
    return result.stdout.rstrip("\n")


def _run_git_optional(repo_path: str, *args: str) -> str | None:
    try:
        return _run_git(repo_path, *args)
    except GitIntrospectionError:
        return None


def get_current_branch(repo_path: str) -> str:
    return _run_git(repo_path, "branch", "--show-current")


def get_local_head(repo_path: str) -> str:
    return _run_git(repo_path, "rev-parse", "HEAD")


def get_dirty_files(repo_path: str) -> tuple[str, ...]:
    out = _run_git(repo_path, "status", "--porcelain")
    return tuple(line[3:] for line in out.splitlines() if line)


def get_remote_tracking_sha(repo_path: str, *, upstream: str | None = None) -> str | None:
    """Reads the local remote-tracking ref (e.g. `origin/<branch>`) as of the last fetch --
    never triggers a network fetch. Returns None if there is no upstream configured/known."""

    ref = upstream or _run_git_optional(repo_path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if not ref:
        return None
    return _run_git_optional(repo_path, "rev-parse", ref)


def get_ahead_behind(repo_path: str, *, upstream: str) -> tuple[int | None, int | None]:
    out = _run_git_optional(repo_path, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
    if out is None:
        return None, None
    parts = out.split()
    if len(parts) != 2:
        return None, None
    ahead, behind = int(parts[0]), int(parts[1])
    return ahead, behind


def snapshot_remote_sync_state(repo_path: str) -> RemoteSyncState:
    branch = get_current_branch(repo_path)
    local_sha = get_local_head(repo_path)
    dirty = get_dirty_files(repo_path)
    upstream = _run_git_optional(repo_path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    remote_sha = get_remote_tracking_sha(repo_path, upstream=upstream) if upstream else None
    ahead, behind = get_ahead_behind(repo_path, upstream=upstream) if upstream else (None, None)
    return RemoteSyncState(
        branch=branch, local_sha=local_sha, remote_sha=remote_sha, ahead=ahead, behind=behind,
        dirty_files=dirty, as_of=datetime.now(timezone.utc), extra={"upstream": upstream},
    )


@dataclass(frozen=True)
class BackupRiskAssessment:
    material: bool
    message: str


def assess_backup_risk(state: RemoteSyncState) -> BackupRiskAssessment:
    """Only material when there is something a founder would actually want to know: unpushed
    commits, dirty working tree, or no known upstream at all. Clean-and-in-sync produces a
    non-material assessment so a caller does not spam the founder every time nothing changed."""

    if state.remote_sha is None:
        return BackupRiskAssessment(material=True, message=f"branch {state.branch} has no known upstream -- local SHA {state.local_sha[:12]} is not verified backed up anywhere remote")

    if state.ahead and state.ahead > 0:
        return BackupRiskAssessment(
            material=True,
            message=f"{state.ahead} commit(s) on {state.branch} exist only locally (local {state.local_sha[:12]} vs remote {state.remote_sha[:12] if state.remote_sha else '?'}) -- COMMITTED != BACKED UP",
        )

    if state.dirty_files:
        return BackupRiskAssessment(material=True, message=f"{len(state.dirty_files)} uncommitted change(s) on {state.branch} -- not committed, so not backed up either")

    return BackupRiskAssessment(material=False, message=f"{state.branch} is clean and matches its remote tracking ref as of last fetch -- no action needed")
