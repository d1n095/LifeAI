#!/usr/bin/env python3
"""Tamper resistance for the security controls themselves.

A security gate only means something if the thing being gated cannot quietly edit the gate. Every
control in this repository is a file — the workflows, the Semgrep rule set, the gitleaks config, the
capability declaration, these scripts — and a file is editable by anything that can open a pull
request. `security/POLICY_LOCK.json` pins the SHA-256 of each one. `verify` recomputes them and
fails closed on:

  * a **changed** control (hash mismatch),
  * a **missing** control (listed, gone),
  * an **uncovered** control (a file matches a protected pattern but is not in the lock at all).

That last case is the one that matters most: without it, an agent could add a new workflow that
disables a check and the lock would still "verify", because the lock only ever described files that
already existed. Adding a control is therefore an explicit, reviewed act (`--update`, in a commit a
human sees), never a side effect.

`security/POLICY_LOCK.json` itself is deliberately NOT hashed here (a file cannot contain its own
digest). It is protected by review: it is listed in `.github/CODEOWNERS` and changing it is exactly
the event this script exists to make visible in a diff.

Exit codes: 0 verified, 1 a control changed/missing/uncovered, 2 the lock itself is unreadable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

LOCK_RELATIVE_PATH = "security/POLICY_LOCK.json"
ALGORITHM = "sha256"

# Every file that can weaken a security control. Globbed rather than listed so a NEW workflow or
# rule file is caught as "uncovered" instead of being invisible.
PROTECTED_GLOBS: tuple[str, ...] = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    ".github/CODEOWNERS",
    ".semgrep/*.yaml",
    ".semgrep/*.yml",
    ".gitleaks.toml",
    "security/agent_capabilities.json",
    "backend/scripts/security/*.py",
    "backend/tests/security/test_security_controls.py",
)

_SKIP_DIR_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def protected_files(repo_root: Path) -> list[str]:
    """Every protected control currently present, as sorted repo-relative POSIX paths."""
    found: set[str] = set()
    for pattern in PROTECTED_GLOBS:
        for path in repo_root.glob(pattern):
            if not path.is_file():
                continue
            if any(part in _SKIP_DIR_NAMES for part in path.parts):
                continue
            found.add(path.relative_to(repo_root).as_posix())
    return sorted(found)


def build_lock(repo_root: Path) -> dict:
    files = {rel: sha256_file(repo_root / rel) for rel in protected_files(repo_root)}
    if not files:
        raise RuntimeError("refusing to write an empty policy lock -- no protected control was found")
    return {
        "version": 1,
        "algorithm": ALGORITHM,
        "description": (
            "SHA-256 of every security control. Verified by backend/scripts/security/verify_policy_lock.py "
            "in the Security workflow. Regenerate with --update, in a reviewed commit."
        ),
        "files": files,
    }


def verify_no_suppressed_failures(repo_root: Path) -> list[str]:
    """A workflow may not tell GitHub Actions to ignore a failing step.

    `continue-on-error: true` is the real suppression mechanism, and this check is deliberately
    narrow: cleanup idioms like `kill "$PID" || true` are legitimate and are NOT flagged, because
    a rule that fires on ordinary shell hygiene gets deleted instead of followed.
    """
    problems: list[str] = []
    workflows_dir = repo_root / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return problems
    for path in sorted(workflows_dir.glob("*.y*ml")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0].strip()
            if code.lower().replace(" ", "") == "continue-on-error:true":
                problems.append(
                    f"{path.relative_to(repo_root).as_posix()}:{lineno}: "
                    "'continue-on-error: true' suppresses a failing check"
                )
    return problems


def verify_lock(repo_root: Path, lock: dict) -> list[str]:
    problems: list[str] = []
    if lock.get("algorithm") != ALGORITHM:
        problems.append(f"unsupported algorithm {lock.get('algorithm')!r} (expected {ALGORITHM!r})")
    locked: dict = lock.get("files") or {}
    if not locked:
        problems.append("the lock lists no files -- it would verify any repository")

    for rel, expected in sorted(locked.items()):
        path = repo_root / rel
        if not path.is_file():
            problems.append(f"missing control: {rel}")
            continue
        actual = sha256_file(path)
        if actual != expected:
            problems.append(f"changed control: {rel} (expected {expected[:12]}…, found {actual[:12]}…)")

    for rel in protected_files(repo_root):
        if rel not in locked:
            problems.append(f"uncovered control: {rel} matches a protected pattern but is not in the lock")

    problems.extend(verify_no_suppressed_failures(repo_root))
    return problems


def load_lock(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"error: cannot read {path}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", default=".", help="repository root (default: .)")
    parser.add_argument("--lock", default=None, help=f"lock file (default: <root>/{LOCK_RELATIVE_PATH})")
    parser.add_argument(
        "--update",
        action="store_true",
        help="rewrite the lock from the current tree (a reviewed act -- never run from CI)",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    lock_path = Path(args.lock) if args.lock else repo_root / LOCK_RELATIVE_PATH

    if args.update:
        lock = build_lock(repo_root)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"policy lock written: {lock_path} ({len(lock['files'])} controls)")
        return 0

    try:
        lock = load_lock(lock_path)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2

    problems = verify_lock(repo_root, lock)
    if problems:
        print("policy lock verification FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nIf the change is intended, regenerate with "
            "`python backend/scripts/security/verify_policy_lock.py --update` and include both the "
            "control change and the regenerated lock in the SAME reviewed commit.",
            file=sys.stderr,
        )
        return 1

    print(f"policy lock verified: {len(lock['files'])} controls unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
