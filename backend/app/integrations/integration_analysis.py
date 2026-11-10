"""Bounded, read-only integration analysis over exact GitHub observations.

GitHub text is data, never a decision or an instruction. Missing evidence is not approval.
"""

from __future__ import annotations

import ast
import asyncio
import re
from collections import defaultdict
from typing import Any
from urllib.parse import quote

import httpx

from app.integrations.github_client import GitHubClient, GitHubClientError

MAX_ANALYZED_PRS = 8
MAX_MIGRATIONS_PER_PR = 12
SECURITY_PATHS = (
    "backend/app/workforce/kill_switch.py",
    "backend/app/workforce/authority.py",
    "backend/app/workforce/failure.py",
    "backend/app/workforce/broker.py",
    "backend/app/egress_policy",
    "backend/app/deps.py",
    "backend/app/rls.py",
)


def parse_migration(source: str) -> tuple[str, tuple[str, ...]]:
    """Read literal Alembic metadata without importing or executing a migration."""
    tree = ast.parse(source)
    values: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            names, value = [node.target], node.value
        else:
            continue
        for name in names:
            if isinstance(name, ast.Name) and name.id in {"revision", "down_revision"}:
                if name.id in values or value is None:
                    raise ValueError("Ambiguous migration metadata.")
                values[name.id] = ast.literal_eval(value)
    revision = values.get("revision")
    parents = values.get("down_revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,128}", revision):
        raise ValueError("Revision is not a literal identifier.")
    if "down_revision" not in values:
        raise ValueError("Missing down_revision.")
    if parents is None:
        result: tuple[str, ...] = ()
    elif isinstance(parents, str):
        result = (parents,)
    elif isinstance(parents, (tuple, list)) and all(isinstance(item, str) for item in parents):
        result = tuple(parents)
    else:
        raise ValueError("down_revision is not literal.")
    if any(not re.fullmatch(r"[A-Za-z0-9_]{1,128}", parent) for parent in result):
        raise ValueError("Invalid parent revision.")
    return revision, result


def _owner(pr: dict, assignments: list[dict], repo: str | None) -> dict:
    matches = {
        item.get("agent_key")
        for item in assignments
        if item.get("branch") == pr.get("head_branch")
        and item.get("repository") == repo
        and item.get("head_sha") == pr.get("head_sha")
        and item.get("agent_key")
    }
    return {
        "github_author": pr.get("author"),
        "agent": next(iter(matches)) if len(matches) == 1 else None,
        "status": "registered_exact_sha" if len(matches) == 1 else (
            "conflicting_assignments" if len(matches) > 1 else "unknown"
        ),
    }


def _unique_branch_map(prs: list[dict]) -> tuple[dict[str, dict], list[str]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for pr in prs:
        if pr.get("head_branch"):
            grouped[pr["head_branch"]].append(pr)
    return (
        {branch: items[0] for branch, items in grouped.items() if len(items) == 1},
        sorted(branch for branch, items in grouped.items() if len(items) > 1),
    )


async def _inspect_pr(client: GitHubClient, pr: dict, repo: str) -> dict:
    sha = pr.get("head_sha")
    result: dict[str, Any] = {
        "number": pr["number"], "head_sha": sha, "files": [], "migrations": [],
        "status": "unknown", "errors": [],
    }
    if (
        not isinstance(sha, str)
        or not re.fullmatch(r"[0-9a-fA-F]{40}", sha)
        or pr.get("head_repository") != repo
    ):
        result["errors"].append("exact_same_repository_head_unavailable")
        return result
    try:
        current = await client.get_pull_request(pr["number"])
        if ((current.get("head") or {}).get("sha") or "").lower() != sha.lower():
            result["errors"].append("head_changed_before_analysis")
            return result
        files = await client.list_pull_request_files(pr["number"])
        result["files"] = [
            item["filename"] for item in files.items
            if isinstance(item.get("filename"), str)
            and len(item["filename"]) <= 512
        ]
        if files.truncated:
            result["errors"].append("changed_files_truncated")
        migration_paths = [
            item["filename"] for item in files.items
            if item.get("status") != "removed"
            and isinstance(item.get("filename"), str)
            and re.fullmatch(r"backend/alembic/versions/[A-Za-z0-9_.-]+\.py", item["filename"])
        ]
        if len(migration_paths) > MAX_MIGRATIONS_PER_PR:
            result["errors"].append("migration_limit_reached")
        for path in migration_paths[:MAX_MIGRATIONS_PER_PR]:
            try:
                source = await client.get_file_at_commit(path, sha)
                revision, parents = parse_migration(source)
                result["migrations"].append({
                    "revision": revision, "down_revision": list(parents), "path": path,
                    "url": f"https://github.com/{repo}/blob/{sha}/{quote(path, safe='/')}",
                })
            except (GitHubClientError, httpx.RequestError, ValueError, SyntaxError):
                result["errors"].append("migration_metadata_unavailable")
        final = await client.get_pull_request(pr["number"])
        if ((final.get("head") or {}).get("sha") or "").lower() != sha.lower():
            result["files"], result["migrations"] = [], []
            result["errors"].append("head_changed_during_analysis")
        result["status"] = "partial" if result["errors"] else "observed"
    except (GitHubClientError, httpx.RequestError, ValueError, TypeError, KeyError):
        result["files"], result["migrations"] = [], []
        result["errors"].append("github_analysis_unavailable")
    return result


def build_integration_graph(
    github: dict, inspections: list[dict], assignments: list[dict]
) -> dict:
    prs = github.get("pull_requests", [])
    by_number = {pr["number"]: pr for pr in prs}
    by_branch, ambiguous_branches = _unique_branch_map(prs)
    closed_by_branch, ambiguous_closed_branches = _unique_branch_map(
        github.get("closed_pull_requests", [])
    )
    scans = {item["number"]: item for item in inspections}
    repo = github.get("repository", {}).get("full_name")
    nodes = []
    findings: list[dict] = []

    def add(kind: str, numbers: list[int], detail: str, evidence: list[dict],
            action: str, tests: list[str], certainty: str = "observed") -> None:
        affected = sorted(set(numbers))
        downstream = sorted({
            pr["number"] for pr in prs
            if any(dep.get("number") in affected for dep in pr.get("dependencies", []))
            or (pr.get("base_branch") in {
                by_number[number].get("head_branch") for number in affected if number in by_number
            })
        } - set(affected))
        findings.append({
            "kind": kind, "certainty": certainty, "pull_requests": affected,
            "detail": detail, "evidence": evidence, "recommendation": action,
            "owners": [{"number": number, **_owner(by_number[number], assignments, repo)}
                       for number in affected if number in by_number],
            "tests": tests, "downstream_revalidate": downstream,
            "reuse": "UNKNOWN — require an independently verified canonical implementation",
        })

    for pr in prs:
        number = pr["number"]
        dependencies = {item["number"]: dict(item) for item in pr.get("dependencies", [])}
        parent = by_branch.get(pr.get("base_branch"))
        if parent and parent["number"] != number:
            dependencies.setdefault(parent["number"], {
                "number": parent["number"], "state": "open", "source": "base_branch",
                "url": parent.get("url"),
            })
        closed_parent = (
            closed_by_branch.get(pr.get("base_branch"))
            if not parent and pr.get("base_branch") not in ambiguous_branches else None
        )
        if closed_parent:
            dependencies.setdefault(closed_parent["number"], {
                "number": closed_parent["number"], "state": closed_parent["state"],
                "source": "base_branch", "url": closed_parent.get("url"),
            })
        for dep in dependencies.values():
            if dep.get("state") == "closed_unmerged":
                add("closed_dependency", [number], f"Dependency #{dep['number']} is closed without merge.",
                    [{"url": dep.get("url"), "kind": dep.get("source")}],
                    "Coordinate with the owners before using this dependency.",
                    ["Verify the replacement dependency and re-run downstream integration tests."])
        reviews = [r for r in pr.get("reviews", []) if r.get("state") in {"APPROVED", "CHANGES_REQUESTED"}]
        stale = [r for r in reviews if r.get("commit_sha") and pr.get("head_sha")
                 and r["commit_sha"].lower() != pr["head_sha"].lower()]
        if stale and pr.get("review_state") == "approval_stale_head":
            add("stale_review", [number], "A GitHub review refers to an earlier commit.",
                [{"url": r.get("url"), "reviewed_sha": r.get("commit_sha"),
                  "head_sha": pr.get("head_sha")} for r in stale],
                "Request an independent review of the exact current SHA.",
                ["Verify reviewed SHA matches the current HEAD."])
        scan = scans.get(number, {})
        blocked = any(dep.get("state") in {"open", "closed_unmerged"} for dep in dependencies.values())
        blocked = blocked or pr.get("ci_state") == "failed" or pr.get("mergeability", {}).get("state") == "CONFLICTING"
        blocked = blocked or pr.get("review_state") == "changes_requested_exact_head"
        nodes.append({
            "number": number, "head_sha": pr.get("head_sha"), "base_branch": pr.get("base_branch"),
            "url": pr.get("url"), "owner": _owner(pr, assignments, repo),
            "dependencies": list(dependencies.values()), "review_state": pr.get("review_state"),
            "reviews": [{"sha": r.get("commit_sha"), "state": r.get("state"), "url": r.get("url")}
                        for r in reviews],
            "security_review": "UNKNOWN — GitHub review is not independent security acceptance",
            "migration_scan": scan.get("status", "not_scanned"),
            "migration_dependencies": scan.get("migrations", []),
            "readiness": "BLOCKED" if blocked else "UNKNOWN",
            "readiness_reason": "Observed blocker." if blocked else (
                "Independent security approval, decision authority and complete migration ancestry not established."
            ),
        })

    node_by_number = {node["number"]: node for node in nodes}
    for node in nodes:
        pending = [dep["number"] for dep in node["dependencies"]]
        seen: set[int] = set()
        while pending:
            number = pending.pop()
            if number in seen or number == node["number"]:
                continue
            seen.add(number)
            if number in node_by_number:
                pending.extend(dep["number"] for dep in node_by_number[number]["dependencies"])
        node["dependency_chain"] = sorted(seen)

    migrations: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    file_owners: dict[str, list[int]] = defaultdict(list)
    for scan in inspections:
        for migration in scan.get("migrations", []):
            migrations[migration["revision"]].append((scan["number"], migration))
        for path in scan.get("files", []):
            if any(path == prefix or path.startswith(prefix + "/") for prefix in SECURITY_PATHS):
                file_owners[path].append(scan["number"])
    for revision, entries in migrations.items():
        if len({number for number, _ in entries}) < 2:
            continue
        if len({(m["path"], tuple(m["down_revision"])) for _, m in entries}) < 2:
            continue
        add("migration_revision_collision", [number for number, _ in entries],
            f"Different changed migrations declare revision {revision}.",
            [{"number": number, **migration} for number, migration in entries],
            "Coordinate a single reviewed migration ancestry; preserve already-applied history.",
            ["Test fresh install, populated upgrades and downgrade/upgrade on the composed lineage."],
            "observed_cross_branch_conflict")
        if len({tuple(m["down_revision"]) for _, m in entries}) > 1:
            add("migration_ancestry_divergence", [number for number, _ in entries],
                f"Revision {revision} declares different parent revisions across candidates.",
                [{"number": number, **migration} for number, migration in entries],
                "Coordinate a single reviewed ancestry without rewriting applied history.",
                ["Test populated upgrades from each supported historical lineage."],
                "observed_cross_branch_conflict")
    for path, numbers in file_owners.items():
        if len(set(numbers)) > 1:
            add("security_ownership_overlap", numbers, f"Multiple candidates modify {path}.",
                [{"number": number, "path": path, "url": by_number[number].get("url")}
                 for number in sorted(set(numbers)) if number in by_number],
                "Owners should agree on an integration plan before composing changes.",
                ["Re-run security and concurrency tests on the integrated exact SHA."], "risk")

    blocked_numbers = {number for finding in findings if finding["kind"] in {
        "migration_revision_collision", "migration_ancestry_divergence", "closed_dependency"
    } for number in finding["pull_requests"]}
    for node in nodes:
        if node["number"] in blocked_numbers:
            node["readiness"], node["readiness_reason"] = "BLOCKED", "Observed integration conflict."
    for finding in findings:
        direct = set(finding["pull_requests"])
        finding["downstream_revalidate"] = sorted(
            node["number"] for node in nodes
            if node["number"] not in direct and direct.intersection(node["dependency_chain"])
        )
    return {
        "schema_version": 1, "freshness": github.get("freshness", "unavailable"),
        "observed_at": github.get("observed_at"), "candidates": nodes, "findings": findings,
        "coverage": {
            "inspected_pull_requests": [item["number"] for item in inspections],
            "limit": MAX_ANALYZED_PRS,
            "open_prs_truncated": github.get("pagination", {}).get("open_pull_requests_truncated", True),
            "inspection_errors": [{"number": item["number"], "errors": item["errors"]}
                                  for item in inspections if item.get("errors")],
            "ambiguous_head_branches": ambiguous_branches,
            "ambiguous_closed_head_branches": ambiguous_closed_branches,
            "migration_ancestry": "UNKNOWN — changed files only, not full revision graphs",
            "founder_decisions": "NOT_CHECKED — no authenticated approval record was established",
            "engineering_lessons": "NOT_CHECKED — external-handoff lesson authorization is blocked",
            "independent_security_reviews": "UNKNOWN — GitHub approvals are not independent acceptance",
        },
    }


async def analyze_integration(
    github: dict, assignments: list[dict], numbers: list[int] | None = None
) -> dict:
    prs = github.get("pull_requests", [])
    selected = [pr for pr in prs if numbers is None or pr["number"] in numbers][:MAX_ANALYZED_PRS]
    repo = github.get("repository", {}).get("full_name")
    if github.get("freshness") in {"stale", "unavailable"} or not repo:
        result = build_integration_graph(github, [], assignments)
        result["coverage"]["inspection_errors"].append({"number": None, "errors": ["live_github_unavailable"]})
        return result
    client = GitHubClient()
    semaphore = asyncio.Semaphore(4)

    async def inspect(pr: dict) -> dict:
        async with semaphore:
            return await _inspect_pr(client, pr, repo)

    inspections = await asyncio.gather(*(inspect(pr) for pr in selected))
    result = build_integration_graph(github, inspections, assignments)

    def invalidate(number: int) -> None:
        node = next(item for item in result["candidates"] if item["number"] == number)
        node["review_state"] = "unknown_head_changed"
        node["reviews"] = []
        node["owner"] = {"github_author": node["owner"]["github_author"], "agent": None, "status": "unknown"}
        node["migration_scan"], node["migration_dependencies"] = "unknown", []
        node["readiness"], node["readiness_reason"] = "UNKNOWN", "HEAD changed during analysis; refresh required."
        result["findings"] = [
            finding for finding in result["findings"] if number not in finding["pull_requests"]
        ]
        still_conflicting = {
            affected for finding in result["findings"]
            if finding["kind"] in {"migration_revision_collision", "migration_ancestry_divergence", "closed_dependency"}
            for affected in finding["pull_requests"]
        }
        for candidate in result["candidates"]:
            if (candidate["number"] not in still_conflicting
                    and candidate["readiness_reason"] == "Observed integration conflict."):
                candidate["readiness"], candidate["readiness_reason"] = (
                    "UNKNOWN", "A related observation changed; refresh required."
                )

    for inspection in inspections:
        if any(error.startswith("head_changed_") for error in inspection["errors"]):
            invalidate(inspection["number"])
    by_branch, _ = _unique_branch_map(prs)
    scan_by_number = {item["number"]: item for item in inspections}
    for pr in selected:
        parent = by_branch.get(pr.get("base_branch"))
        scan = scan_by_number[pr["number"]]
        if (
            not parent or parent["number"] == pr["number"] or scan["status"] != "observed"
            or any(node["review_state"] == "unknown_head_changed"
                   for node in result["candidates"] if node["number"] in {pr["number"], parent["number"]})
        ):
            continue
        try:
            if parent.get("head_repository") != repo:
                raise ValueError("Upstream head is not in the configured repository.")
            comparison = await client.compare_commits(parent.get("head_sha"), pr.get("head_sha"))
            parent_now, child_now = await asyncio.gather(
                client.get_pull_request(parent["number"]), client.get_pull_request(pr["number"])
            )
            if (
                ((parent_now.get("head") or {}).get("sha") or "").lower() != parent["head_sha"].lower()
                or ((child_now.get("head") or {}).get("sha") or "").lower() != pr["head_sha"].lower()
            ):
                if ((parent_now.get("head") or {}).get("sha") or "").lower() != parent["head_sha"].lower():
                    invalidate(parent["number"])
                if ((child_now.get("head") or {}).get("sha") or "").lower() != pr["head_sha"].lower():
                    invalidate(pr["number"])
                result["coverage"]["inspection_errors"].append({
                    "number": pr["number"], "errors": ["head_changed_during_comparison"]
                })
                continue
            status = comparison.get("status")
            if status in {"behind", "diverged"}:
                node = next(item for item in result["candidates"] if item["number"] == pr["number"])
                node["readiness"], node["readiness_reason"] = "BLOCKED", "Current upstream HEAD is not an ancestor."
                result["findings"].append({
                    "kind": "upstream_head_not_in_child", "certainty": "observed",
                    "pull_requests": [pr["number"], parent["number"]],
                    "detail": "The current upstream HEAD is not an ancestor of the downstream HEAD; missing changes are not identified.",
                    "evidence": [{
                        "parent_sha": parent.get("head_sha"), "child_sha": pr.get("head_sha"),
                        "url": f"https://github.com/{repo}/compare/{parent['head_sha']}...{pr['head_sha']}",
                        "comparison_status": status,
                    }],
                    "recommendation": "Owners should evaluate missing upstream changes after the dependency is integrated; do not rebase preemptively.",
                    "owners": [{"number": item["number"], **_owner(item, assignments, repo)}
                               for item in (pr, parent)],
                    "tests": ["Verify ancestry and rerun downstream security and integration tests."],
                    "downstream_revalidate": [pr["number"]],
                    "reuse": "UNKNOWN — inspect the upstream exact-SHA changes.",
                })
            elif status not in {"ahead", "identical"}:
                result["coverage"]["inspection_errors"].append({
                    "number": pr["number"], "errors": ["upstream_comparison_incomplete"]
                })
        except (GitHubClientError, httpx.RequestError, ValueError, TypeError, KeyError):
            result["coverage"]["inspection_errors"].append({
                "number": pr["number"], "errors": ["upstream_comparison_unavailable"]
            })
    if numbers:
        missing = sorted(set(numbers) - {pr["number"] for pr in selected})
        if missing:
            result["coverage"]["inspection_errors"].append({
                "number": None, "errors": ["requested_pr_not_in_open_observation"], "numbers": missing
            })
    return result
