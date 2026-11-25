"""Local git and captured pull-request evidence for the system map.

Two deliberately separate sources:

- `scan_local_git()` reads the checkout it is pointed at (`git for-each-ref`, `rev-parse`,
  `status --porcelain`). That is verifiable, offline evidence of what exists locally.
- `load_pull_requests()` reads a JSON file captured from the GitHub API by a human or an
  authenticated workflow. The atlas never calls the network itself and never invents a PR
  state.

A branch that exists locally is not proof of a remote branch, a merge, or a deployment.
Callers must keep those states separate; `GitEvidence` only reports what it observed.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class GitRef:
    name: str
    sha: str
    is_current: bool


@dataclass(frozen=True)
class PullRequestRef:
    number: int
    state: str
    draft: bool
    head_ref: str
    head_sha: str
    base_ref: str
    updated_at: str | None
    url: str | None


@dataclass(frozen=True)
class GitEvidence:
    current_branch: str | None
    current_sha: str | None
    dirty_tracked_tree: bool | None
    local_branches: tuple[GitRef, ...]
    pull_requests: tuple[PullRequestRef, ...]
    captured_at: str
    source: str

    def to_dict(self) -> dict:
        return {
            "current_branch": self.current_branch,
            "current_sha": self.current_sha,
            "dirty_tracked_tree": self.dirty_tracked_tree,
            "local_branches": [
                {"name": b.name, "sha": b.sha, "is_current": b.is_current}
                for b in self.local_branches
            ],
            "pull_requests": [
                {
                    "number": pr.number,
                    "state": pr.state,
                    "draft": pr.draft,
                    "head_ref": pr.head_ref,
                    "head_sha": pr.head_sha,
                    "base_ref": pr.base_ref,
                    "updated_at": pr.updated_at,
                    "url": pr.url,
                }
                for pr in self.pull_requests
            ],
            "captured_at": self.captured_at,
            "source": self.source,
            "execution_authorized": False,
        }


def _git(repo_root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def scan_local_git(repo_root: Path, *, pull_requests_path: Path | None = None) -> GitEvidence:
    repo_root = Path(repo_root).resolve()
    branch = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    sha = _git(repo_root, "rev-parse", "HEAD")
    status = _git(repo_root, "status", "--porcelain", "--untracked-files=no")
    refs_text = _git(repo_root, "for-each-ref", "--format=%(refname:short)\t%(objectname)", "refs/heads/")

    current_branch = branch.strip() if branch else None
    branches: list[GitRef] = []
    for line in (refs_text or "").splitlines():
        if "\t" not in line:
            continue
        name, ref_sha = line.split("\t", 1)
        branches.append(
            GitRef(name=name, sha=ref_sha.strip(), is_current=name == current_branch)
        )

    return GitEvidence(
        current_branch=current_branch,
        current_sha=sha.strip() if sha else None,
        dirty_tracked_tree=bool(status.strip()) if status is not None else None,
        local_branches=tuple(sorted(branches, key=lambda b: b.name)),
        pull_requests=load_pull_requests(pull_requests_path),
        captured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source="local_git" + ("+captured_pull_requests" if pull_requests_path else ""),
    )


def load_pull_requests(path: Path | None) -> tuple[PullRequestRef, ...]:
    """Read a captured `[{number, state, draft, head: {ref, sha}, base: {ref}, ...}]` file."""
    if path is None:
        return ()
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()

    items = payload.get("pull_requests", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return ()

    refs: list[PullRequestRef] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("number"), int):
            continue
        head = item.get("head") if isinstance(item.get("head"), dict) else {}
        base = item.get("base") if isinstance(item.get("base"), dict) else {}
        refs.append(
            PullRequestRef(
                number=item["number"],
                state=str(item.get("state", "unknown")),
                draft=bool(item.get("draft", False)),
                head_ref=str(head.get("ref") or item.get("head_ref") or ""),
                head_sha=str(head.get("sha") or item.get("head_sha") or ""),
                base_ref=str(base.get("ref") or item.get("base_ref") or ""),
                updated_at=item.get("updated_at"),
                url=item.get("html_url") or item.get("url"),
            )
        )
    return tuple(sorted(refs, key=lambda pr: pr.number))
