"""Proposal-mode isolation — an unverified AI edit never lands in the live checkout.

Proposal mode (`GITHUB_WRITE_ENABLED` off) used to write the code agent's content straight into
the single checkout the backend process itself executes from, purely so `verify_task()` could run
against real content. PI-1d made that failure *recoverable* (the pre-write content is captured and
restored on any non-accepting path — still verified, still kept here as defense in depth), but
recoverability is not isolation: between the write and the rollback the live tree is modified, and
a process killed inside that window leaves an unaccepted proposal's content in the code the next
startup executes.

This module removes the window instead of shrinking it. Every proposal attempt gets its own
checkout, built from the shared checkout's own HEAD, with:

  * **no remote at all.** The bootstrap is a fresh bare repository (`git init --bare`) that is
    populated by a *local path* fetch from the shared checkout. Nothing is ever cloned from the
    GitHub URL, so `remote.origin.url` — and the `x-access-token:<PAT>@github.com` that
    `worktree.py`'s local-mode remote would carry — cannot be inherited. `assert_no_credentialed_remote()`
    re-reads the resulting config and fails closed if any remote URL with a scheme or an `@`
    appears, rather than assuming the fetch direction was honored.

  * **the exact commit the deployment runs**, not a branch name: `HEAD` is resolved once, pinned
    to `refs/mainai/base`, and verified after the fetch. If the shared checkout moves in between,
    the bootstrap fails closed instead of silently verifying a different tree.

  * **no write path back to the shared checkout.** The proposal's files are written inside the
    isolated tree only; verification runs there.

Because the worktree is created by `git worktree add` from that local bare repository, the attempt
also inherits none of the shared checkout's config, hooks, stash or remotes — only its commits.

Failure policy: `ProposalWorktreeError` is raised, never swallowed here. The caller
(`execution_job._propose_repo_edit`) logs it loudly and falls back to the shared checkout, where
PI-1d's rollback still protects the tree. Isolation is the default; the fallback exists so a
missing `git` binary degrades to "as safe as before" rather than "MainAI cannot propose anything".
"""

import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

PROPOSAL_ROOT = Path("/tmp/mainai-proposal-worktrees")
BASE_REF = "refs/mainai/base"
_ATTEMPT_FILENAME = "attempt.json"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_GIT_TIMEOUT_SECONDS = 120
# An attempt id names a directory. Today it is always a job/task UUID, but the value is passed
# through as a path component, so it is validated as one here rather than trusted because every
# current caller happens to be internal: "../../" in an id would place a proposal's write root
# outside PROPOSAL_ROOT, which is exactly the boundary this module exists to hold.
_ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ProposalWorktreeError(RuntimeError):
    """Any failure to build a trustworthy isolated checkout. Callers must treat this as "no
    isolation available", never as "the shared checkout is unchanged"."""


def _git(args: list[str], *, cwd: Path, timeout: int = _GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProposalWorktreeError(f"git {' '.join(args)} could not run: {exc}") from exc
    if result.returncode != 0:
        raise ProposalWorktreeError(f"git {' '.join(args)} failed ({result.returncode}): {result.stderr[:800]}")
    return result


def remote_urls(repo: Path) -> list[str]:
    """Every configured remote URL (local config only). Returns [] when there are none, which is
    the expected state for a bootstrap; a non-zero exit other than "no matches" is an error."""
    result = subprocess.run(
        ["git", "-C", str(repo), "config", "--local", "--get-regexp", r"^remote\..*\.url$"],
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):  # 1 == no matches
        raise ProposalWorktreeError(f"could not read remotes of {repo}: {result.stderr[:400]}")
    urls = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].strip():
            urls.append(parts[1].strip())
    return urls


def assert_no_credentialed_remote(repo: Path) -> None:
    """Fail closed on any remote that could carry credentials or reach a network.

    A proposal checkout needs no remote whatsoever — it is never pushed, never fetched from.
    `ghp_...`, `x-access-token:...@`, `https://user:pass@host` and a bare `git@github.com:` form all
    contain either `://` or `@`, so both are rejected; a plain filesystem path (which the bootstrap
    itself never configures either) is the only shape that could ever pass.
    """
    for url in remote_urls(repo):
        if "://" in url or "@" in url:
            raise ProposalWorktreeError(
                f"refusing to use {repo}: it carries a credentialed/network remote ({url.split('@')[-1]!r}). "
                "A proposal checkout must never inherit a Git remote."
            )


def _resolve_shared_head(shared_checkout: Path) -> str:
    head = _git(["rev-parse", "HEAD"], cwd=shared_checkout).stdout.strip()
    if not _SHA_RE.match(head):
        raise ProposalWorktreeError(f"shared checkout {shared_checkout} has no usable HEAD ({head!r}).")
    return head


def bootstrap_local_base(shared_checkout: Path, *, dest: Path) -> str:
    """Create a bare, remote-less repository at `dest` holding the shared checkout's HEAD.

    The fetch source is the *filesystem path* of the shared checkout, so no URL — and therefore no
    credential — is ever read, written or persisted. Returns the pinned base SHA.
    """
    shared = Path(shared_checkout).resolve()
    if not (shared / ".git").exists():
        raise ProposalWorktreeError(f"{shared} is not a git checkout — cannot bootstrap a proposal base from it.")
    base_sha = _resolve_shared_head(shared)

    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.mkdir(parents=True)
    dest.chmod(0o700)
    try:
        # A fresh bare repository: no remotes, no hooks, no inherited config.
        _git(["init", "-q", "--bare", "."], cwd=dest)
        _git(
            [
                "-c",
                "protocol.file.allow=always",
                "fetch",
                "--no-tags",
                "--depth",
                "1",
                str(shared),
                f"HEAD:{BASE_REF}",
            ],
            cwd=dest,
        )
        fetched = _git(["rev-parse", BASE_REF], cwd=dest).stdout.strip()
        if fetched != base_sha:
            # The shared checkout moved while we were bootstrapping: our pinned identity no
            # longer describes what we would verify. Fail closed rather than guess.
            raise ProposalWorktreeError(
                f"shared checkout HEAD moved during bootstrap ({base_sha} -> {fetched}); refusing to verify against an unpinned tree."
            )
        assert_no_credentialed_remote(dest)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return base_sha


def _attempt_root(attempt_id) -> Path:
    """Resolve the per-attempt directory, refusing anything that is not a plain safe name.

    Two independent checks, because either alone is one mistake away from useless: the token must
    match `_ATTEMPT_ID_RE` (no separators, no `..`, no leading dot, bounded length), and the
    resolved path must still be a direct child of PROPOSAL_ROOT — which also catches a symlinked
    PROPOSAL_ROOT or a platform where the join behaves unexpectedly.
    """
    token = str(attempt_id)
    if not _ATTEMPT_ID_RE.match(token) or ".." in token:
        raise ProposalWorktreeError(
            f"refusing attempt id {token[:80]!r}: an attempt id must be a plain directory name"
        )
    root = (PROPOSAL_ROOT / token).resolve()
    if root.parent != PROPOSAL_ROOT.resolve():
        raise ProposalWorktreeError(
            f"refusing attempt id {token[:80]!r}: it resolves outside {PROPOSAL_ROOT}"
        )
    return root


def create_isolated_proposal_checkout(*, shared_checkout: Path, attempt_id) -> "IsolatedProposalCheckout":
    """Build the per-attempt checkout. `attempt_id` (a job id, or the task id when no job exists)
    scopes the directory so two attempts never share a tree."""
    root = _attempt_root(attempt_id)
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    root.chmod(0o700)

    base_repo = root / ".base.git"
    try:
        base_sha = bootstrap_local_base(shared_checkout, dest=base_repo)
        tree = root / "tree"
        _git(["worktree", "add", "--detach", str(tree), BASE_REF], cwd=base_repo)
        assert_no_credentialed_remote(tree)
        (root / _ATTEMPT_FILENAME).write_text(
            json.dumps(
                {
                    "attempt_id": str(attempt_id),
                    "base_sha": base_sha,
                    "created_at": datetime.utcnow().isoformat(),
                    # Deliberately no remote, no token, no user data: this file is audit
                    # metadata for "which commit did we verify against", nothing more.
                }
            ),
            encoding="utf-8",
        )
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return IsolatedProposalCheckout(root=root, tree=tree, base_sha=base_sha)


class IsolatedProposalCheckout:
    """Plain value object — no DB row, no lease, no authority. It only describes a directory."""

    __slots__ = ("root", "tree", "base_sha")

    def __init__(self, *, root: Path, tree: Path, base_sha: str) -> None:
        self.root = root
        self.tree = tree
        self.base_sha = base_sha


def remove_isolated_proposal_checkout(checkout: IsolatedProposalCheckout) -> None:
    """Best-effort removal. A leftover directory under `/tmp` is harmless (it is not a durable
    record and is never trusted on existence alone); an exception here must not mask the caller's
    real outcome."""
    try:
        if Path(checkout.root).exists():
            shutil.rmtree(checkout.root, ignore_errors=True)
    except OSError:  # pragma: no cover - defensive
        pass


def remove_isolated_proposal_checkout_for_attempt(attempt_id) -> None:
    """Same best-effort removal as `remove_isolated_proposal_checkout()`, but reachable from a
    caller that only has the durable `attempt_id` (the job id a repo_edit task's proposal
    checkout was created under — see `execution_job._propose_repo_edit()`'s own
    `attempt_id=job.id` call) and not a live `IsolatedProposalCheckout` object — e.g.
    `final_report.py`'s goal-finalization cleanup, which runs long after the job that created
    the checkout, and the task, have both finished. Reconciliation finding (PI-1d merge): this
    mechanism's own success path never cleaned up a completed proposal's checkout at all before
    this helper existed — only the write-failure path inside `_propose_repo_edit()` did.
    Deliberately a no-op (never raises) for an attempt_id that was never a real checkout, or
    whose directory name fails `_attempt_root()`'s own validation — cleanup must never fail a
    goal's finalization over a directory that may never have existed."""
    try:
        root = _attempt_root(attempt_id)
    except ProposalWorktreeError:
        return
    try:
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
    except OSError:  # pragma: no cover - defensive
        pass
