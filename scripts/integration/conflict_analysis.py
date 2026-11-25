#!/usr/bin/env python3
"""Read-only conflict analysis for an imported delivery against the integration branch.

No merge is performed and no conflict is resolved. The output distinguishes textual Git
conflicts, migration-graph collisions and security-sensitive overlap that needs a human.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.integration.common import (  # noqa: E402
    DEFAULT_TARGET_REF,
    PipelineError,
    git,
    is_ancestor,
    merge_base,
    resolve_commit,
    status_exit,
    write_json,
)

MIGRATION_DIR = "backend/alembic/versions/"
SECURITY_SENSITIVE = re.compile(
    r"(^|/)(?:auth|security|rls|egress_policy|providers|alembic|migrations?|"
    r"\.github/workflows|scripts/vps|docker-compose|Dockerfile|prompt_fencing|"
    r"chat\.py|config\.py)(/|\.|$)",
    re.IGNORECASE,
)


def _diff_statuses(repo: Path, base: str, head: str) -> dict[str, str]:
    result = git(repo, "diff", "--name-status", "--no-renames", base, head, check=True)
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            raise PipelineError(f"unexpected git diff record: {line!r}", "FAIL")
        values[parts[1]] = parts[0][:1]
    return values


def _commits(repo: Path, base: str, head: str) -> list[dict[str, str]]:
    result = git(repo, "log", "--reverse", "--format=%H%x00%s%x00", f"{base}..{head}", check=True)
    parts = result.stdout.split("\x00")
    commits: list[dict[str, str]] = []
    for index in range(0, len(parts) - 1, 2):
        sha = parts[index].strip()
        subject = parts[index + 1].strip()
        if re.fullmatch(r"[0-9a-f]{40}", sha):
            commits.append({"sha": sha, "subject": subject})
    return commits


def _merge_tree_conflicts(repo: Path, target: str, candidate: str) -> tuple[str, list[str]]:
    result = git(
        repo,
        "merge-tree",
        "--write-tree",
        "--name-only",
        "--no-messages",
        target,
        candidate,
    )
    if result.returncode not in (0, 1):
        raise PipelineError(
            "git merge-tree failed; cannot determine actual conflicts",
            "BLOCKED",
            detail=result.stderr.strip(),
        )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise PipelineError("git merge-tree returned no tree", "FAIL")
    # First line is the resulting tree OID; with --no-messages the rest are paths.
    paths = [line for line in lines[1:] if not line.startswith(("Auto-merging ", "CONFLICT "))]
    return lines[0], sorted(set(paths))


def _migration_files(repo: Path, rev: str) -> list[str]:
    result = git(repo, "ls-tree", "-r", "--name-only", rev, "--", MIGRATION_DIR)
    if result.returncode != 0:
        raise PipelineError(f"could not list migrations at {rev}", "FAIL")
    return sorted(
        path
        for path in result.stdout.splitlines()
        if path.endswith(".py") and Path(path).name != "__init__.py"
    )


def _parse_migration(repo: Path, rev: str, path: str) -> dict[str, Any]:
    result = git(repo, "show", f"{rev}:{path}")
    if result.returncode != 0:
        return {"path": path, "error": "missing blob"}
    try:
        tree = ast.parse(result.stdout)
    except SyntaxError as exc:
        return {"path": path, "error": f"syntax error: {exc.msg} (line {exc.lineno})"}
    values: dict[str, Any] = {}
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
            try:
                values[target.id] = ast.literal_eval(value)  # type: ignore[arg-type]
            except (ValueError, TypeError):
                values[target.id] = None
    if not isinstance(values.get("revision"), str):
        return {"path": path, "error": "missing string revision"}
    down = values.get("down_revision")
    if down is None:
        downs: list[str] = []
    elif isinstance(down, str):
        downs = [down]
    elif isinstance(down, (tuple, list)) and all(isinstance(item, str) for item in down):
        downs = list(down)
    else:
        return {"path": path, "error": "down_revision is not None, a string, or a string tuple"}
    return {"path": path, "revision": values["revision"], "down_revision": downs}


def _migration_graph(repo: Path, rev: str) -> dict[str, Any]:
    files = _migration_files(repo, rev)
    parsed: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    for path in files:
        item = _parse_migration(repo, rev, path)
        if "error" in item:
            errors.append({"path": path, "error": item["error"]})
            continue
        parsed[path] = item
    revisions: dict[str, list[str]] = {}
    for path, item in parsed.items():
        revisions.setdefault(item["revision"], []).append(path)
    duplicates = {
        revision: paths for revision, paths in revisions.items() if len(paths) > 1
    }
    down_values = {down for item in parsed.values() for down in item["down_revision"]}
    heads = sorted(set(revisions) - down_values)
    missing = sorted(
        down
        for item in parsed.values()
        for down in item["down_revision"]
        if down not in revisions
    )
    return {
        "files": files,
        "parsed": parsed,
        "revisions": revisions,
        "duplicate_revisions": duplicates,
        "heads": heads,
        "missing_parents": missing,
        "errors": errors,
    }


def _migration_analysis(
    repo: Path,
    candidate: str,
    target: str,
    candidate_diff: dict[str, str],
    target_diff: dict[str, str],
) -> dict[str, Any]:
    candidate_graph = _migration_graph(repo, candidate)
    target_graph = _migration_graph(repo, target)
    union_revisions = set(candidate_graph["revisions"]) | set(target_graph["revisions"])
    candidate_paths = set(candidate_graph["files"])
    target_paths = set(target_graph["files"])
    changed_candidate = {path for path in candidate_diff if path.startswith(MIGRATION_DIR)}
    changed_target = {path for path in target_diff if path.startswith(MIGRATION_DIR)}

    revision_collisions: list[dict[str, Any]] = []
    for revision in sorted(set(candidate_graph["revisions"]) & set(target_graph["revisions"])):
        c_paths = candidate_graph["revisions"][revision]
        t_paths = target_graph["revisions"][revision]
        if set(c_paths) != set(t_paths):
            revision_collisions.append(
                {"revision": revision, "candidate_paths": c_paths, "target_paths": t_paths}
            )

    same_path_collisions: list[dict[str, Any]] = []
    for path in sorted(candidate_paths & target_paths):
        c_blob = git(repo, "rev-parse", f"{candidate}:{path}").stdout.strip()
        t_blob = git(repo, "rev-parse", f"{target}:{path}").stdout.strip()
        if c_blob and t_blob and c_blob != t_blob:
            same_path_collisions.append(
                {"path": path, "candidate_blob": c_blob, "target_blob": t_blob}
            )

    candidate_missing = sorted(
        down for down in candidate_graph["missing_parents"] if down not in union_revisions
    )
    target_missing = sorted(
        down for down in target_graph["missing_parents"] if down not in union_revisions
    )
    union_heads = sorted(
        (set(candidate_graph["heads"]) | set(target_graph["heads"]))
        - {down for graph in (candidate_graph, target_graph) for item in graph["parsed"].values() for down in item["down_revision"]}
    )
    parse_errors = [
        {"side": "candidate", **item} for item in candidate_graph["errors"]
    ] + [{"side": "target", **item} for item in target_graph["errors"]]

    failed = bool(
        revision_collisions
        or same_path_collisions
        or candidate_missing
        or target_missing
        or parse_errors
        or len(union_heads) != 1
    )
    return {
        "status": "FAIL" if failed else "PASS",
        "candidate_changed_files": sorted(changed_candidate),
        "target_changed_files": sorted(changed_target),
        "candidate_heads": candidate_graph["heads"],
        "target_heads": target_graph["heads"],
        "union_heads": union_heads,
        "multiple_heads": len(union_heads) != 1,
        "revision_collisions": revision_collisions,
        "same_path_collisions": same_path_collisions,
        "candidate_missing_parents": candidate_missing,
        "target_missing_parents": target_missing,
        "parse_errors": parse_errors,
    }


def analyze(
    *,
    repo: Path | str,
    candidate: str,
    target: str = DEFAULT_TARGET_REF,
    base: str | None = None,
) -> dict[str, Any]:
    repo_path = Path(repo).resolve()
    candidate_sha = resolve_commit(repo_path, candidate)
    target_sha = resolve_commit(repo_path, target)
    base_sha = resolve_commit(repo_path, base) if base else merge_base(repo_path, target_sha, candidate_sha)
    if not base_sha:
        raise PipelineError("candidate and target have no merge base", "BLOCKED")

    already_integrated = is_ancestor(repo_path, candidate_sha, target_sha)
    candidate_commits = _commits(repo_path, base_sha, candidate_sha)
    target_commits = _commits(repo_path, base_sha, target_sha)
    candidate_diff = _diff_statuses(repo_path, base_sha, candidate_sha)
    target_diff = _diff_statuses(repo_path, base_sha, target_sha)
    overlap = sorted(set(candidate_diff) & set(target_diff))
    overlapping_files = [
        {
            "path": path,
            "candidate_status": candidate_diff[path],
            "target_status": target_diff[path],
            "security_sensitive": bool(SECURITY_SENSITIVE.search(path)),
        }
        for path in overlap
    ]

    tree_sha, textual_conflicts = _merge_tree_conflicts(repo_path, target_sha, candidate_sha)
    migration = _migration_analysis(
        repo_path, candidate_sha, target_sha, candidate_diff, target_diff
    )

    cherry = git(repo_path, "cherry", target_sha, candidate_sha, base_sha)
    equivalent_patch_commits: list[str] = []
    if cherry.returncode == 0:
        for line in cherry.stdout.splitlines():
            if line.startswith("- "):
                parts = line.split(maxsplit=1)
                if len(parts) == 2:
                    equivalent_patch_commits.append(parts[1])

    semantic_conflicts: list[dict[str, Any]] = []
    for item in overlapping_files:
        if item["security_sensitive"]:
            semantic_conflicts.append(
                {
                    "path": item["path"],
                    "reason": "security-sensitive path modified on both sides",
                    "requires_manual_review": True,
                    "candidate_status": item["candidate_status"],
                    "target_status": item["target_status"],
                }
            )
    for path in textual_conflicts:
        if not any(item["path"] == path for item in semantic_conflicts):
            semantic_conflicts.append(
                {
                    "path": path,
                    "reason": "textual Git conflict; semantic resolution is not automated",
                    "requires_manual_review": True,
                }
            )

    if textual_conflicts or migration["status"] == "FAIL":
        status = "FAIL"
    elif semantic_conflicts:
        status = "BLOCKED"
    else:
        status = "PASS"

    return {
        "status": status,
        "candidate": candidate_sha,
        "target": target_sha,
        "target_ref": target,
        "merge_base": base_sha,
        "already_integrated": already_integrated,
        "equivalent_patch_commits": equivalent_patch_commits,
        "candidate_only_commits": candidate_commits,
        "target_only_commits": target_commits,
        "missing_functionality": [
            {"sha": item["sha"], "subject": item["subject"]} for item in candidate_commits
        ],
        "overlapping_files": overlapping_files,
        "textual_conflicts": textual_conflicts,
        "merge_tree": tree_sha,
        "semantic_conflicts": semantic_conflicts,
        "manual_review_required": bool(semantic_conflicts),
        "migration_collisions": migration,
        "security_sensitive_files": [
            item["path"] for item in overlapping_files if item["security_sensitive"]
        ],
        "checks": {
            "candidate_exists": "PASS",
            "target_exists": "PASS",
            "merge_base_exists": "PASS",
            "merge_tree": "PASS",
            "migration_graph": migration["status"],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--target", default=DEFAULT_TARGET_REF)
    parser.add_argument("--base")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        result = analyze(
            repo=args.repo, candidate=args.candidate, target=args.target, base=args.base
        )
    except PipelineError as exc:
        result = {"status": exc.status, "error": str(exc), "detail": exc.detail}
    if args.out:
        write_json(args.out, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return status_exit(result["status"])


if __name__ == "__main__":
    raise SystemExit(main())
