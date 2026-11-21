#!/usr/bin/env python3
"""Secure import of an agent-delivered Git bundle.

This command is intentionally a read-only verifier plus one namespaced import ref. It
never updates a branch, never force-pushes, and never executes a script from the bundle.
Optional worktrees are detached and can be removed without affecting the original commit.

Example:
    python scripts/integration/bundle_import.py \
      --repo . \
      --bundle delivery/v7-testlab/lifeai-v7-testlab.bundle \
      --manifest delivery/v7-testlab/SHA256SUMS \
      --expected-commit 8917f0ca4f5a64e57a8eda623d57763145bd9cde \
      --expected-base 56190209cdfb462f2127f4bc04f91fc2b9b5efe3 \
      --target-ref origin/integration/founder-alpha-20261121 \
      --worktree /tmp/lifeai-v8-import-v7 \
      --json-out delivery/v8-integration/evidence/import-v7.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.integration.common import (  # noqa: E402
    COMMIT_RE,
    DEFAULT_TARGET_REF,
    PipelineError,
    commit_metadata,
    git,
    git_ok,
    git_output,
    is_ancestor,
    scan_tree,
    sha256_file,
    status_exit,
    verify_manifest,
    worktree_add,
    worktree_remove,
    worst_status,
    write_json,
)


def _list_heads(repo: Path, bundle: Path) -> list[tuple[str, str]]:
    result = git(repo, "bundle", "list-heads", str(bundle))
    if result.returncode != 0:
        raise PipelineError(f"cannot list bundle heads: {result.stderr.strip()}", "FAIL")
    heads: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        match = re.match(r"^([0-9a-f]{40})\s+(\S+)$", line.strip())
        if match:
            heads.append((match.group(1), match.group(2)))
    if not heads:
        raise PipelineError("bundle contains no heads", "FAIL")
    return heads


def _select_head(
    heads: list[tuple[str, str]], *, expected_commit: str | None, head_ref: str | None
) -> tuple[str, str]:
    if head_ref:
        matches = [item for item in heads if item[1] == head_ref]
        if len(matches) != 1:
            raise PipelineError(f"bundle does not contain exactly one {head_ref!r} head", "FAIL")
        return matches[0]
    if expected_commit:
        matches = [item for item in heads if item[0] == expected_commit]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise PipelineError("expected commit appears under multiple bundle heads", "FAIL")
    if len(heads) == 1:
        return heads[0]
    raise PipelineError(
        "bundle has multiple heads; pass --expected-commit or --head-ref to select one", "FAIL"
    )


def _classify_bundle_failure(text: str) -> str:
    lowered = text.lower()
    if "prerequisite" in lowered or "missing" in lowered or "could not find" in lowered:
        return "BLOCKED"
    return "FAIL"


def import_bundle(
    *,
    repo: Path | str,
    bundle: Path | str,
    manifest: Path | str,
    expected_commit: str | None,
    expected_base: str,
    target_ref: str | None = DEFAULT_TARGET_REF,
    head_ref: str | None = None,
    worktree: Path | str | None = None,
    import_prefix: str = "refs/imports/lifeai",
) -> dict[str, Any]:
    repo_path = Path(repo).resolve()
    bundle_path = Path(bundle).resolve()
    if not bundle_path.is_file():
        raise PipelineError(f"bundle not found: {bundle_path}", "FAIL")
    if not expected_base or not re.fullmatch(r"[0-9a-f]{40}", expected_base):
        raise PipelineError("--expected-base must be a full 40-character commit SHA", "FAIL")
    if expected_commit and not COMMIT_RE.fullmatch(expected_commit):
        raise PipelineError("--expected-commit must be a full 40-character commit SHA", "FAIL")

    manifest_result = verify_manifest(manifest, required_path=bundle_path)
    bundle_digest = manifest_result["required_sha256"]
    if bundle_digest != sha256_file(bundle_path):
        raise PipelineError("bundle changed after manifest verification", "FAIL")

    checks: dict[str, dict[str, Any]] = {
        "manifest": {"status": "PASS", "manifest_sha256": manifest_result["manifest_sha256"]},
    }
    verify_result = git(repo_path, "bundle", "verify", str(bundle_path))
    if verify_result.returncode != 0:
        output = f"{verify_result.stdout}\n{verify_result.stderr}"
        raise PipelineError(
            "bundle verification failed (corrupt or missing prerequisite)",
            _classify_bundle_failure(output),
            detail=output[-4000:],
        )
    checks["bundle"] = {"status": "PASS", "detail": verify_result.stderr.strip() or verify_result.stdout.strip()}

    heads = _list_heads(repo_path, bundle_path)
    selected_sha, selected_ref = _select_head(
        heads, expected_commit=expected_commit, head_ref=head_ref
    )
    if expected_commit and selected_sha != expected_commit:
        raise PipelineError(
            f"bundle head {selected_sha} does not match expected commit {expected_commit}", "FAIL"
        )
    checks["head"] = {"status": "PASS", "sha": selected_sha, "ref": selected_ref, "heads": len(heads)}

    import_ref = f"{import_prefix.rstrip('/')}/{selected_sha}"
    existing_ref = git(repo_path, "rev-parse", "--verify", import_ref)
    created_ref = False
    created_worktree: Path | None = None
    try:
        if existing_ref.returncode == 0:
            existing_sha = existing_ref.stdout.strip()
            if existing_sha != selected_sha:
                raise PipelineError(
                    f"import ref {import_ref} already points at a different commit", "FAIL"
                )
            already_imported = True
        else:
            fetch = git(
                repo_path,
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                str(bundle_path),
                f"{selected_ref}:{import_ref}",
                timeout=180,
            )
            if fetch.returncode != 0:
                output = f"{fetch.stdout}\n{fetch.stderr}"
                raise PipelineError(
                    "could not fetch bundle head; prerequisite commit may be missing",
                    _classify_bundle_failure(output),
                    detail=output[-4000:],
                )
            created_ref = True
            already_imported = False
        checks["import_ref"] = {
            "status": "PASS",
            "ref": import_ref,
            "already_imported": already_imported,
        }

        # The expected base must be present and a real ancestor. This is checked after
        # fetching, because a complete bundle may itself carry the prerequisite object.
        if not git_ok(repo_path, "cat-file", "-e", f"{expected_base}^{{commit}}"):
            raise PipelineError(
                f"expected base commit {expected_base} is missing from the repository", "BLOCKED"
            )
        if not is_ancestor(repo_path, expected_base, selected_sha):
            raise PipelineError(
                f"bundle head {selected_sha} is not a descendant of expected base {expected_base}", "FAIL"
            )
        checks["ancestry"] = {
            "status": "PASS",
            "base": expected_base,
            "commit_count": int(git_output(repo_path, "rev-list", "--count", f"{expected_base}..{selected_sha}")),
        }

        target_sha: str | None = None
        already_integrated = False
        equivalent_commits: list[str] = []
        target_status = "BLOCKED"
        target_detail = "target ref not provided"
        if target_ref:
            target_result = git(repo_path, "rev-parse", "--verify", f"{target_ref}^{{commit}}")
            if target_result.returncode != 0:
                target_detail = f"target ref missing: {target_ref}"
            else:
                target_sha = target_result.stdout.strip()
                target_status = "PASS"
                target_detail = target_ref
                already_integrated = is_ancestor(repo_path, selected_sha, target_sha)
                cherry = git(repo_path, "cherry", target_sha, selected_sha, expected_base)
                if cherry.returncode == 0:
                    equivalent_commits = [
                        line.split(maxsplit=1)[1]
                        for line in cherry.stdout.splitlines()
                        if line.startswith("- ") and len(line.split(maxsplit=1)) == 2
                    ]
        checks["target"] = {
            "status": target_status,
            "ref": target_ref,
            "target_sha": target_sha,
            "already_integrated": already_integrated,
            "equivalent_patch_commits": equivalent_commits,
            "detail": target_detail,
        }

        security = scan_tree(repo_path, selected_sha, base=expected_base)
        checks["security"] = security
        if security["status"] != "PASS":
            raise PipelineError(
                "bundle contains unsafe paths, symlinks, credential files, or high-confidence secrets",
                "FAIL",
                detail=json.dumps(
                    {
                        "unsafe_paths": security["unsafe_paths"],
                        "symlinks": security["symlinks"],
                        "submodules": security["submodules"],
                        "credential_files": security["credential_files"],
                        "findings": security["findings"],
                        "message_findings": security["message_findings"],
                    },
                    sort_keys=True,
                ),
            )

        metadata = commit_metadata(repo_path, selected_sha)
        checks["provenance"] = {
            "status": "PASS",
            "sha": selected_sha,
            "parents": metadata["parents"],
            "author": f"{metadata['author_name']} <{metadata['author_email']}>",
            "committer": f"{metadata['committer_name']} <{metadata['committer_email']}>",
            "subject": metadata["subject"],
        }

        if worktree:
            created_worktree = worktree_add(repo_path, worktree, selected_sha)
            head = git_output(created_worktree, "rev-parse", "HEAD")
            if head != selected_sha:
                raise PipelineError("worktree HEAD does not match imported commit", "FAIL")
            checks["worktree"] = {"status": "PASS", "path": str(created_worktree), "head": head}
        else:
            checks["worktree"] = {"status": "PASS", "path": None, "head": selected_sha}

        status = worst_status(check["status"] for check in checks.values())
        blockers = []
        if checks["target"]["status"] != "PASS":
            blockers.append(f"target ref unavailable: {target_ref}")
        duplicate_delivery = already_imported
        return {
            "status": status,
            "bundle": str(bundle_path),
            "bundle_sha256": bundle_digest,
            "manifest": str(Path(manifest).resolve()),
            "manifest_sha256": manifest_result["manifest_sha256"],
            "commit": selected_sha,
            "expected_commit": expected_commit,
            "base": expected_base,
            "head_ref": selected_ref,
            "import_ref": import_ref,
            "already_imported": already_imported,
            "duplicate_delivery": duplicate_delivery,
            "already_integrated": already_integrated,
            "equivalent_patch_commits": equivalent_commits,
            "worktree": str(created_worktree) if created_worktree else None,
            "checks": checks,
            "blockers": blockers,
        }
    except Exception:
        if created_worktree is not None:
            try:
                worktree_remove(repo_path, created_worktree)
            except Exception:
                pass
        if created_ref:
            git(repo_path, "update-ref", "-d", import_ref, selected_sha)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--expected-commit")
    parser.add_argument("--expected-base", required=True)
    parser.add_argument("--target-ref", default=DEFAULT_TARGET_REF)
    parser.add_argument("--head-ref")
    parser.add_argument("--worktree")
    parser.add_argument("--import-prefix", default="refs/imports/lifeai")
    parser.add_argument("--json-out")
    args = parser.parse_args(argv)
    try:
        result = import_bundle(
            repo=args.repo,
            bundle=args.bundle,
            manifest=args.manifest,
            expected_commit=args.expected_commit,
            expected_base=args.expected_base,
            target_ref=args.target_ref or None,
            head_ref=args.head_ref,
            worktree=args.worktree,
            import_prefix=args.import_prefix,
        )
    except PipelineError as exc:
        result = {
            "status": exc.status,
            "error": str(exc),
            "detail": exc.detail,
            "bundle": str(Path(args.bundle).resolve()),
            "manifest": str(Path(args.manifest).resolve()),
            "expected_commit": args.expected_commit,
            "expected_base": args.expected_base,
        }
    if args.json_out:
        write_json(args.json_out, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return status_exit(result["status"])


if __name__ == "__main__":
    raise SystemExit(main())
