#!/usr/bin/env python3
"""Build and verify an immutable, reproducible staging release manifest.

The manifest records the integration base, every imported commit, its delivery base,
dependencies, actual changed files, verification status, required checks, blockers and
artifact hashes. It refuses to overwrite an existing manifest and rejects provenance that
cannot be tied back to real Git objects.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.integration.common import (  # noqa: E402
    STATUSES,
    PipelineError,
    canonical_json,
    check_no_secret_strings,
    commit_metadata,
    git,
    is_ancestor,
    merge_base,
    read_json,
    resolve_commit,
    sha256_file,
    status_exit,
    write_json,
)


def _artifact_path(repo: Path, input_dir: Path, value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate.resolve()
    for base in (repo, input_dir):
        resolved = (base / candidate).resolve()
        if resolved.exists():
            return resolved
    return (repo / candidate).resolve()


def _actual_changes(repo: Path, base: str, commit: str) -> list[str]:
    result = git(repo, "diff", "--name-only", "--no-renames", base, commit, check=True)
    return sorted(line for line in result.stdout.splitlines() if line)


def _topological_order(names: set[str], dependencies: dict[str, list[str]]) -> list[str]:
    remaining = {name: set(dependencies.get(name, [])) for name in names}
    order: list[str] = []
    while remaining:
        ready = sorted(name for name, deps in remaining.items() if not deps)
        if not ready:
            raise PipelineError("delivery dependency graph contains a cycle", "FAIL")
        for name in ready:
            order.append(name)
            remaining.pop(name)
        for deps in remaining.values():
            deps.difference_update(ready)
    return order


def build_manifest(
    *,
    repo: Path | str,
    input_path: Path | str,
    out_path: Path | str,
) -> dict[str, Any]:
    repo_path = Path(repo).resolve()
    input_file = Path(input_path).resolve()
    source = read_json(input_file)
    input_dir = input_file.parent
    if not isinstance(source, dict):
        raise PipelineError("manifest input must be a JSON object", "FAIL")

    base_value = source.get("base")
    if not base_value:
        raise PipelineError("manifest input is missing base", "FAIL")
    base_sha = resolve_commit(repo_path, str(base_value))
    base_meta = commit_metadata(repo_path, base_sha)

    raw_deliveries = source.get("deliveries")
    if not isinstance(raw_deliveries, list) or not raw_deliveries:
        raise PipelineError("manifest input must contain at least one delivery", "FAIL")

    deliveries: list[dict[str, Any]] = []
    names: set[str] = set()
    commits: set[str] = set()
    dependency_map: dict[str, list[str]] = {}
    artifacts: dict[str, dict[str, str]] = {}

    for raw in raw_deliveries:
        if not isinstance(raw, dict):
            raise PipelineError("each delivery must be an object", "FAIL")
        name = str(raw.get("name") or "").strip()
        if not name or name in names:
            raise PipelineError(f"delivery name is missing or duplicated: {name!r}", "FAIL")
        names.add(name)
        commit = resolve_commit(repo_path, str(raw.get("commit") or ""))
        if commit in commits:
            raise PipelineError(f"commit {commit} is delivered more than once", "FAIL")
        for other in commits:
            if is_ancestor(repo_path, other, commit) or is_ancestor(repo_path, commit, other):
                raise PipelineError(
                    f"delivery {name} overlaps an already included commit ({other}); refusing duplicate delivery",
                    "FAIL",
                )
        commits.add(commit)
        delivery_base = resolve_commit(repo_path, str(raw.get("base") or ""))
        if not is_ancestor(repo_path, delivery_base, commit):
            raise PipelineError(f"{name}: delivery base is not an ancestor of its commit", "FAIL")
        meta = commit_metadata(repo_path, commit)

        actual_changes = _actual_changes(repo_path, delivery_base, commit)
        declared_changes = raw.get("applied_changes", actual_changes)
        if not isinstance(declared_changes, list) or not all(isinstance(item, str) for item in declared_changes):
            raise PipelineError(f"{name}: applied_changes must be a list of paths", "FAIL")
        untraceable = sorted(set(declared_changes) - set(actual_changes))
        if untraceable:
            raise PipelineError(
                f"{name}: applied_changes contains paths not in the real delivery diff: {untraceable}",
                "FAIL",
            )

        bundle_value = raw.get("bundle")
        if not bundle_value:
            raise PipelineError(f"{name}: bundle path is required", "FAIL")
        bundle_path = _artifact_path(repo_path, input_dir, str(bundle_value))
        if not bundle_path.is_file():
            raise PipelineError(f"{name}: bundle not found: {bundle_path}", "BLOCKED")
        bundle_hash = sha256_file(bundle_path)
        declared_hash = raw.get("bundle_sha256")
        if declared_hash and str(declared_hash).lower() != bundle_hash:
            raise PipelineError(f"{name}: bundle SHA-256 does not match the artifact", "FAIL")
        artifacts[str(bundle_path)] = {"path": str(bundle_path), "sha256": bundle_hash}

        dependencies = raw.get("dependencies", [])
        if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
            raise PipelineError(f"{name}: dependencies must be a list of delivery names", "FAIL")
        dependency_map[name] = sorted(set(dependencies))

        verification = raw.get("verification")
        if not isinstance(verification, dict) or verification.get("status") not in STATUSES:
            raise PipelineError(f"{name}: verification.status must be PASS, FAIL or BLOCKED", "FAIL")
        blockers = raw.get("blockers", [])
        if not isinstance(blockers, list) or not all(isinstance(item, str) for item in blockers):
            raise PipelineError(f"{name}: blockers must be a list of strings", "FAIL")

        deliveries.append(
            {
                "name": name,
                "commit": commit,
                "subject": meta["subject"],
                "base": delivery_base,
                "base_relation": "descendant" if is_ancestor(repo_path, base_sha, commit) else "divergent",
                "merge_base_with_integration": merge_base(repo_path, base_sha, commit),
                "bundle": str(bundle_path),
                "bundle_sha256": bundle_hash,
                "dependencies": dependency_map[name],
                "applied_changes": sorted(set(declared_changes)),
                "actual_changed_files": actual_changes,
                "verification": verification,
                "blockers": blockers,
            }
        )

    for name, deps in dependency_map.items():
        for dep in deps:
            if dep == name:
                raise PipelineError(f"{name}: a delivery cannot depend on itself", "FAIL")
            if dep not in names:
                raise PipelineError(f"{name}: unknown dependency {dep!r}", "FAIL")
            dep_delivery = next(item for item in deliveries if item["name"] == dep)
            own_delivery = next(item for item in deliveries if item["name"] == name)
            if not (
                is_ancestor(repo_path, dep_delivery["commit"], own_delivery["base"])
                or is_ancestor(repo_path, dep_delivery["commit"], own_delivery["commit"])
            ):
                raise PipelineError(
                    f"{name}: dependency {dep} is not an ancestor of its base or commit", "FAIL"
                )

    order = _topological_order(names, dependency_map)

    raw_artifacts = source.get("artifacts", [])
    if not isinstance(raw_artifacts, list):
        raise PipelineError("artifacts must be a list", "FAIL")
    for item in raw_artifacts:
        if not isinstance(item, dict) or not item.get("path") or not item.get("sha256"):
            raise PipelineError("each artifact needs path and sha256", "FAIL")
        artifact_path = _artifact_path(repo_path, input_dir, str(item["path"]))
        if not artifact_path.is_file():
            raise PipelineError(f"artifact not found: {artifact_path}", "BLOCKED")
        actual = sha256_file(artifact_path)
        expected = str(item["sha256"]).lower()
        if actual != expected:
            raise PipelineError(f"artifact SHA-256 mismatch: {artifact_path}", "FAIL")
        artifacts[str(artifact_path)] = {"path": str(artifact_path), "sha256": actual}

    required_checks = source.get("required_checks", [])
    if not isinstance(required_checks, list) or not all(isinstance(item, str) for item in required_checks):
        raise PipelineError("required_checks must be a list of strings", "FAIL")
    known_blockers = source.get("known_blockers", [])
    if not isinstance(known_blockers, list) or not all(isinstance(item, str) for item in known_blockers):
        raise PipelineError("known_blockers must be a list of strings", "FAIL")

    payload: dict[str, Any] = {
        "manifest_version": 1,
        "kind": "lifeai-staging-release",
        "base": {"sha": base_sha, "subject": base_meta["subject"]},
        "deliveries": sorted(deliveries, key=lambda item: item["name"]),
        "dependency_order": order,
        "required_checks": sorted(set(required_checks)),
        "known_blockers": known_blockers,
        "artifacts": sorted(artifacts.values(), key=lambda item: item["path"]),
        "staging_ready": all(item["verification"]["status"] == "PASS" for item in deliveries)
        and not known_blockers,
        "input_sha256": sha256_file(input_file),
        "provenance": {
            "source": str(input_file),
            "imported_commits": sorted(commits),
            "base_commit": base_sha,
        },
    }
    check_no_secret_strings(canonical_json(payload), label="release manifest")
    payload["manifest_sha256"] = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    write_json(out_path, payload, immutable=True)
    return {"status": "PASS", "manifest": str(Path(out_path).resolve()), "manifest_sha256": payload["manifest_sha256"], "staging_ready": payload["staging_ready"]}


def verify_manifest(*, repo: Path | str, manifest_path: Path | str) -> dict[str, Any]:
    repo_path = Path(repo).resolve()
    manifest_file = Path(manifest_path).resolve()
    payload = read_json(manifest_file)
    if not isinstance(payload, dict):
        raise PipelineError("manifest must be an object", "FAIL")
    recorded = payload.get("manifest_sha256")
    if not isinstance(recorded, str) or not re.fullmatch(r"[0-9a-f]{64}", recorded):
        raise PipelineError("manifest is missing a valid manifest_sha256", "FAIL")
    unsigned = dict(payload)
    unsigned.pop("manifest_sha256", None)
    actual = hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()
    if actual != recorded:
        raise PipelineError("manifest self-hash does not match its contents", "FAIL")
    check_no_secret_strings(canonical_json(payload), label="release manifest")

    base_sha = resolve_commit(repo_path, str((payload.get("base") or {}).get("sha") or ""))
    deliveries = payload.get("deliveries")
    if not isinstance(deliveries, list) or not deliveries:
        raise PipelineError("manifest has no deliveries", "FAIL")
    names: set[str] = set()
    commits: set[str] = set()
    for item in deliveries:
        if not isinstance(item, dict):
            raise PipelineError("manifest delivery is not an object", "FAIL")
        name = str(item.get("name") or "")
        if not name or name in names:
            raise PipelineError("manifest delivery names are missing or duplicated", "FAIL")
        names.add(name)
        commit = resolve_commit(repo_path, str(item.get("commit") or ""))
        delivery_base = resolve_commit(repo_path, str(item.get("base") or ""))
        if commit in commits:
            raise PipelineError(f"duplicate commit in manifest: {commit}", "FAIL")
        commits.add(commit)
        if not is_ancestor(repo_path, delivery_base, commit):
            raise PipelineError(f"{name}: base is not an ancestor of commit", "FAIL")
        actual_changes = set(_actual_changes(repo_path, delivery_base, commit))
        if not set(item.get("applied_changes", [])) <= actual_changes:
            raise PipelineError(f"{name}: applied_changes is not traceable to the Git diff", "FAIL")
        bundle = Path(str(item.get("bundle") or ""))
        if not bundle.is_file() or sha256_file(bundle) != item.get("bundle_sha256"):
            raise PipelineError(f"{name}: bundle hash does not match the artifact", "FAIL")
    for artifact in payload.get("artifacts", []):
        path = Path(str(artifact.get("path") or ""))
        if not path.is_file() or sha256_file(path) != artifact.get("sha256"):
            raise PipelineError(f"artifact hash mismatch: {path}", "FAIL")
    if not is_ancestor(repo_path, base_sha, base_sha):
        raise PipelineError("integration base is not valid", "FAIL")
    return {
        "status": "PASS",
        "manifest": str(manifest_file),
        "manifest_sha256": recorded,
        "staging_ready": bool(payload.get("staging_ready")),
        "deliveries": len(deliveries),
        "imported_commits": sorted(commits),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--input", required=True)
    build.add_argument("--out", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            result = build_manifest(repo=args.repo, input_path=args.input, out_path=args.out)
        else:
            result = verify_manifest(repo=args.repo, manifest_path=args.manifest)
    except PipelineError as exc:
        result = {"status": exc.status, "error": str(exc), "detail": exc.detail}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return status_exit(result["status"])


if __name__ == "__main__":
    raise SystemExit(main())
