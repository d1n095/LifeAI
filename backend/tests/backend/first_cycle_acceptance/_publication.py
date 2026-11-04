"""First-cycle publication oracle. Independent of production path-set checks.

Required for GO:
- exactly one commit on the approved base;
- every changed object in that commit (and therefore in the full history) is a regular file;
- every path is inside the authorized set (including deletions);
- working tree, index and HEAD agree (no untracked/staged discrepancy);
- no symlink, gitlink/submodule, or other non-regular mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ._git import (
    GITLINK_MODE,
    REGULAR_FILE_MODES,
    SYMLINK_MODE,
    commit_parent,
    commits_exclusive,
    diff_tree_raw,
    ls_tree_mode,
    porcelain,
)


@dataclass
class PublicationVerdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    commit_count: int = 0
    commits: list[str] = field(default_factory=list)
    objects: list[dict] = field(default_factory=list)

    def fail(self, reason: str) -> None:
        self.ok = False
        self.reasons.append(reason)


def evaluate_publication(
    *,
    cwd,
    base_sha: str,
    head: str = "HEAD",
    authorized_paths: list[str],
) -> PublicationVerdict:
    verdict = PublicationVerdict(ok=True)
    authorized = set(authorized_paths)
    commits = commits_exclusive(cwd, base_sha, head)
    verdict.commits = commits
    verdict.commit_count = len(commits)
    if len(commits) != 1:
        verdict.fail(
            f"first cycle requires exactly one authorized commit on the approved base; got {len(commits)}: {commits}"
        )

    dirty = porcelain(cwd)
    if dirty.strip():
        verdict.fail(f"working tree/index is not clean at publication:\n{dirty}")

    seen_paths: set[str] = set()
    for sha in commits:
        parent = commit_parent(cwd, sha)
        for old_mode, new_mode, status, path in diff_tree_raw(cwd, parent, sha):
            record = {
                "commit": sha,
                "path": path,
                "old_mode": old_mode,
                "new_mode": new_mode,
                "status": status,
            }
            verdict.objects.append(record)
            seen_paths.add(path)
            if path not in authorized:
                verdict.fail(
                    f"out-of-scope path {path!r} appears in commit {sha[:12]} (status={status}); "
                    "a clean final diff must not conceal intermediate or deleted unauthorized paths"
                )
            mode = new_mode if new_mode != "000000" else old_mode
            if new_mode == SYMLINK_MODE or old_mode == SYMLINK_MODE or mode == SYMLINK_MODE:
                verdict.fail(f"symlink is not a regular file: {path!r} in {sha[:12]}")
            if new_mode == GITLINK_MODE or old_mode == GITLINK_MODE:
                verdict.fail(f"git submodule/gitlink is forbidden: {path!r} in {sha[:12]}")
            if new_mode not in {"000000", *REGULAR_FILE_MODES} and new_mode != "040000":
                verdict.fail(f"non-regular object mode {new_mode} for {path!r} in {sha[:12]}")
            if new_mode in REGULAR_FILE_MODES:
                continue
            if new_mode == "000000" and old_mode in REGULAR_FILE_MODES:
                continue
            if new_mode not in {"000000", *REGULAR_FILE_MODES}:
                verdict.fail(f"changed object {path!r} is not a regular file (mode={new_mode})")

        for path in authorized:
            mode = ls_tree_mode(cwd, sha, path)
            if mode == SYMLINK_MODE:
                verdict.fail(f"authorized path {path!r} is a symlink at {sha[:12]}")
            if mode == GITLINK_MODE:
                verdict.fail(f"authorized path {path!r} is a gitlink at {sha[:12]}")

    return verdict
