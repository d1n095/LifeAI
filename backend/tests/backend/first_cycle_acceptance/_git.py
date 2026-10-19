"""Local git helpers. Network-free. Isolated worktrees and a bare remote with a grant hook."""

from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

AUTHOR = {
    "GIT_AUTHOR_NAME": "first-cycle-harness",
    "GIT_AUTHOR_EMAIL": "harness@lifeai.test",
    "GIT_COMMITTER_NAME": "first-cycle-harness",
    "GIT_COMMITTER_EMAIL": "harness@lifeai.test",
}

REGULAR_FILE_MODES = frozenset({"100644", "100755"})
SYMLINK_MODE = "120000"
GITLINK_MODE = "160000"


def git(*args: str, cwd: Path | None = None, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    merged = {**os.environ, **AUTHOR, **(env or {})}
    # Never inherit live credentials into harness git.
    for key in (
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GIT_ASKPASS",
        "GIT_USERNAME",
        "GIT_PASSWORD",
        "GIT_TERMINAL_PROMPT",
    ):
        merged.pop(key, None)
    merged["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        env=merged,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({result.returncode}): {(result.stderr or result.stdout)[:1200]}"
        )
    return result


def rev_parse(cwd: Path, ref: str = "HEAD") -> str:
    return git("rev-parse", ref, cwd=cwd).stdout.strip()


@dataclass
class IsolatedRepo:
    bare: Path
    work: Path
    base_branch: str
    base_sha: str
    grant_box: Path

    def work_sha(self) -> str:
        return rev_parse(self.work)

    def remote_sha(self, ref: str) -> str | None:
        result = git("rev-parse", f"refs/heads/{ref}", cwd=self.bare, check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip()


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def install_exact_sha_grant_hook(bare: Path, grant_box: Path) -> None:
    """pre-receive: refuse unless a one-use grant file names the incoming SHA.

    This is the harness stand-in for remote grant enforcement. Production must later
    implement the same refusal without this hook being copied into production code.
    """
    grant_box.mkdir(parents=True, exist_ok=True)
    hook = bare / "hooks" / "pre-receive"
    _write_executable(
        hook,
        f"""#!/bin/sh
grant_file="{grant_box / "live_push_grant"}"
if [ ! -f "$grant_file" ]; then
  echo "first-cycle-harness: ungranted push refused" >&2
  exit 1
fi
expected=$(cat "$grant_file")
rm -f "$grant_file"
while read old new ref; do
  if [ "$new" != "$expected" ]; then
    echo "first-cycle-harness: incoming $new is not the granted SHA $expected" >&2
    exit 1
  fi
done
exit 0
""",
    )


def seed_isolated_repo(tmp_path: Path, *, base_branch: str = "integration/founder-alpha-test-base") -> IsolatedRepo:
    bare = tmp_path / "github.git"
    work = tmp_path / "work"
    grant_box = tmp_path / "grant-box"
    git("init", "--bare", "-q", str(bare))
    git("clone", "-q", str(bare), str(work))
    (work / "backend" / "app").mkdir(parents=True)
    (work / "backend" / "tests").mkdir(parents=True)
    (work / "backend" / "app" / "greeting.py").write_text("def greet(name):\n    return f'hello {name}'\n")
    (work / "backend" / "tests" / "test_greeting_doc.py").write_text("def test_placeholder():\n    assert True\n")
    (work / "README.md").write_text("seed\n")
    git("config", "user.email", AUTHOR["GIT_AUTHOR_EMAIL"], cwd=work)
    git("config", "user.name", AUTHOR["GIT_AUTHOR_NAME"], cwd=work)
    git("checkout", "-q", "-b", base_branch, cwd=work)
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "seed", cwd=work)
    git("push", "-q", "origin", base_branch, cwd=work)
    install_exact_sha_grant_hook(bare, grant_box)
    base_sha = rev_parse(work)
    return IsolatedRepo(bare=bare, work=work, base_branch=base_branch, base_sha=base_sha, grant_box=grant_box)


def porcelain(cwd: Path) -> str:
    return git("status", "--porcelain", cwd=cwd).stdout


def diff_tree_raw(cwd: Path, parent: str, commit: str) -> list[tuple[str, str, str, str]]:
    """Return (old_mode, new_mode, status, path) for every changed object."""
    out = git("diff-tree", "-r", "--no-renames", "--raw", "-z", parent, commit, cwd=cwd).stdout
    entries: list[tuple[str, str, str, str]] = []
    parts = [p for p in out.split("\0") if p]
    i = 0
    while i < len(parts):
        raw = parts[i]
        if not raw.startswith(":"):
            i += 1
            continue
        meta = raw[1:].split()
        if len(meta) < 5:
            i += 1
            continue
        old_mode, new_mode, _old_sha, _new_sha, status = meta[:5]
        path = parts[i + 1] if i + 1 < len(parts) else ""
        entries.append((old_mode, new_mode, status, path))
        i += 2
    return entries


def commits_exclusive(cwd: Path, base: str, head: str = "HEAD") -> list[str]:
    out = git("rev-list", "--reverse", f"{base}..{head}", cwd=cwd).stdout.strip()
    return [line for line in out.splitlines() if line]


def commit_parent(cwd: Path, sha: str) -> str:
    return git("rev-parse", f"{sha}^", cwd=cwd).stdout.strip()


def ls_tree_mode(cwd: Path, sha: str, path: str) -> str | None:
    result = git("ls-tree", "-z", sha, "--", path, cwd=cwd, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    # "<mode> <type> <sha>\t<path>"
    first = result.stdout.split("\0", 1)[0]
    return first.split(" ", 1)[0]


def add_commit(cwd: Path, message: str) -> str:
    git("add", "-A", cwd=cwd)
    git("commit", "-q", "-m", message, cwd=cwd)
    return rev_parse(cwd)


def checkout_branch(cwd: Path, name: str, start: str | None = None) -> None:
    if start:
        git("checkout", "-q", "-B", name, start, cwd=cwd)
    else:
        git("checkout", "-q", "-B", name, cwd=cwd)
