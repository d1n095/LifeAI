#!/usr/bin/env python3
"""Build provenance: prove the artifact that runs is the source that was reviewed.

A green test run says nothing about the bytes on disk at execution time. This script measures them.
`create` writes a manifest of every source file under a root (path -> SHA-256, plus one tree digest
over the sorted list). `verify` recomputes and fails closed on any added, changed or removed file.

That closes the specific gap the Developer Alpha mission names: "build provenance and verification
that reviewed source matches the executed artifact". The manifest is produced from the reviewed
commit (in CI, after checkout) and can be verified against the deployed tree before (or after)
startup; a mismatch is a hard failure, never a warning.

Honest scope, stated rather than implied: a digest manifest is *integrity*, not *authenticity*. It
detects unintended and unaudited modification of the measured tree; it does not by itself prove who
produced it (that needs a signed attestation from a trusted identity) and it says nothing about
whether the reviewed source is free of malicious logic. Combine with review, the capability
restrictions and the isolated execution boundary — never instead of them.

Exit codes: 0 verified, 1 mismatch, 2 unreadable input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ALGORITHM = "sha256"
_SKIP_DIR_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".next"}
_SKIP_SUFFIXES = {".pyc", ".pyo"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _included(path: Path, root: Path) -> bool:
    rel_parts = path.relative_to(root).parts
    if any(part in _SKIP_DIR_NAMES for part in rel_parts):
        return False
    return path.suffix not in _SKIP_SUFFIXES


def tree_sha256(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for rel in sorted(files):
        digest.update(f"{rel}\0{files[rel]}\n".encode("utf-8"))
    return digest.hexdigest()


def measure(root: Path) -> dict[str, str]:
    files = {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and _included(path, root)
    }
    if not files:
        raise ValueError(f"no files found under {root}")
    return files


def _git_sha(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def create_manifest(root: Path, *, label: str | None = None) -> dict:
    files = measure(root)
    return {
        "version": 1,
        "algorithm": ALGORITHM,
        "label": label,
        "root": root.name,
        "git_commit": _git_sha(root),
        "file_count": len(files),
        "tree_sha256": tree_sha256(files),
        "files": files,
    }


def verify_manifest(root: Path, manifest: dict) -> list[str]:
    problems: list[str] = []
    if manifest.get("algorithm") != ALGORITHM:
        problems.append(f"unsupported algorithm {manifest.get('algorithm')!r}")
    expected: dict = manifest.get("files") or {}
    if not expected:
        problems.append("manifest lists no files -- it would verify anything")
        return problems

    try:
        actual = measure(root)
    except ValueError as exc:
        return [str(exc)]

    for rel, digest in sorted(expected.items()):
        if rel not in actual:
            problems.append(f"missing from artifact: {rel}")
        elif actual[rel] != digest:
            problems.append(f"changed in artifact: {rel}")
    for rel in sorted(set(actual) - set(expected)):
        problems.append(f"present in artifact but not in the reviewed manifest: {rel}")

    if not problems and tree_sha256(actual) != manifest.get("tree_sha256"):
        problems.append("tree digest mismatch even though per-file digests matched (manifest is inconsistent)")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="measure a tree and write a provenance manifest")
    create.add_argument("--root", required=True)
    create.add_argument("--out", required=True)
    create.add_argument("--label", default=None)

    verify = sub.add_parser("verify", help="re-measure a tree and compare it to a manifest")
    verify.add_argument("--root", required=True)
    verify.add_argument("--manifest", required=True)

    args = parser.parse_args(argv)

    if args.command == "create":
        try:
            manifest = create_manifest(Path(args.root), label=args.label)
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(
            f"provenance written: {args.out} "
            f"({manifest['file_count']} files, tree {manifest['tree_sha256'][:16]}…, commit {manifest['git_commit']})"
        )
        return 0

    try:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read {args.manifest}: {exc}", file=sys.stderr)
        return 2

    problems = verify_manifest(Path(args.root), manifest)
    if problems:
        print("provenance verification FAILED:", file=sys.stderr)
        for problem in problems[:50]:
            print(f"  - {problem}", file=sys.stderr)
        if len(problems) > 50:
            print(f"  ... and {len(problems) - 50} more", file=sys.stderr)
        return 1

    print(f"provenance verified: {manifest.get('file_count')} files match the reviewed manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
