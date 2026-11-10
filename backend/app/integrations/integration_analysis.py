"""Bounded, read-only integration analysis over exact GitHub observations.

GitHub text is data, never a decision or an instruction. Missing evidence is not approval.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import re
from collections import defaultdict
from typing import Any
from urllib.parse import quote

import httpx

from app.integrations.github_client import GitHubClient, GitHubClientError

MAX_ANALYZED_PRS = 8
MAX_MIGRATIONS_PER_PR = 12
MAX_GRAPH_MIGRATIONS = 128
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


def parse_migration_schema_effects(source: str) -> list[dict]:
    """Extract literal table-oriented Alembic operations without importing or running code."""
    effects = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        method = node.func.attr
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != "op":
            continue
        if method in {"create_table", "drop_table", "add_column", "drop_column", "alter_column"}:
            if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                continue
            table = node.args[0].value
        elif method == "create_index":
            if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant) or not isinstance(node.args[1].value, str):
                continue
            table = node.args[1].value
        elif method == "drop_index":
            table_arg = next((kw.value for kw in node.keywords if kw.arg == "table_name"), None)
            if not isinstance(table_arg, ast.Constant) or not isinstance(table_arg.value, str):
                continue
            table = table_arg.value
        else:
            continue
        effects.append({"table": table, "operation": method})
    return effects


async def inspect_migration_graph(client: GitHubClient, pr: dict, repo: str) -> dict:
    """Inspect one bounded version directory without executing repository code."""
    sha, number = pr["head_sha"], pr["number"]
    result: dict[str, Any] = {"number": number, "head_sha": sha, "status": "UNKNOWN",
                              "errors": [], "migrations": [], "heads": [], "missing": []}
    try:
        before = await client.get_pull_request(number)
        if ((before.get("head") or {}).get("sha") or "").lower() != sha.lower():
            result["errors"].append("head_changed_before_graph")
            return result
        entries = await client.list_migration_directory(sha)
        paths = []
        for item in entries:
            path = item.get("path")
            if item.get("type") == "dir":
                result["errors"].append("nested_migration_directory_unchecked")
            elif isinstance(path, str) and path.endswith(".py"):
                if item.get("type") != "file" or not re.fullmatch(
                    r"backend/alembic/versions/[A-Za-z0-9_.-]+\.py", path
                ):
                    result["errors"].append("unsupported_migration_entry")
                else:
                    paths.append(path)
        if len(entries) >= 1000 or len(paths) > MAX_GRAPH_MIGRATIONS:
            result["errors"].append("migration_graph_limit_reached")
            return result
        semaphore = asyncio.Semaphore(4)

        async def read(path: str) -> tuple[dict | None, str | None]:
            async with semaphore:
                try:
                    source = await client.get_file_at_commit(path, sha)
                    revision, parents = parse_migration(source)
                    schema_effects = parse_migration_schema_effects(source)
                    tree = ast.parse(source)
                    for node in tree.body:
                        names = node.targets if isinstance(node, ast.Assign) else (
                            [node.target] if isinstance(node, ast.AnnAssign) else []
                        )
                        if any(isinstance(name, ast.Name) and name.id in {"depends_on", "branch_labels"} for name in names):
                            if node.value is None or ast.literal_eval(node.value) is not None:
                                return None, "migration_dependency_metadata_unsupported"
                    return {"revision": revision, "down_revision": list(parents), "path": path,
                            "schema_effects": schema_effects,
                            "content_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                            "url": f"https://github.com/{repo}/blob/{sha}/{quote(path, safe='/')}"}, None
                except (GitHubClientError, httpx.RequestError, ValueError, SyntaxError, TypeError):
                    return None, "migration_graph_metadata_unavailable"

        reads = await asyncio.gather(*(read(path) for path in paths))
        result["migrations"] = [migration for migration, _ in reads if migration is not None]
        result["errors"].extend(sorted({error for _, error in reads if error}))
        after = await client.get_pull_request(number)
        if ((after.get("head") or {}).get("sha") or "").lower() != sha.lower():
            result["errors"].append("head_changed_during_graph")
            result["migrations"] = []
            return result
        if result["errors"] or not paths:
            if not paths:
                result["errors"].append("migration_directory_empty")
            return result
        revisions: dict[str, list[dict]] = defaultdict(list)
        for migration in result["migrations"]:
            revisions[migration["revision"]].append(migration)
        result["duplicates"] = sorted(revision for revision, items in revisions.items() if len(items) > 1)
        parents = {parent for migration in result["migrations"] for parent in migration["down_revision"]}
        result["missing"] = sorted(parents - revisions.keys())
        result["heads"] = sorted(revisions.keys() - parents)
        remaining = {revision: set(parent for item in items for parent in item["down_revision"])
                     & revisions.keys() for revision, items in revisions.items()}
        while remaining:
            roots = {revision for revision, parents_set in remaining.items() if not parents_set}
            if not roots:
                break
            remaining = {revision: parents_set - roots for revision, parents_set in remaining.items()
                         if revision not in roots}
        result["cycle_revisions"] = sorted(remaining)
        result["status"] = "observed"
    except (GitHubClientError, httpx.RequestError, ValueError, TypeError, KeyError, AttributeError):
        result["errors"].append("migration_graph_unavailable")
    return result


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


def _finalize_findings(result: dict, github: dict) -> None:
    blocking_kinds = {
        "migration_revision_collision", "migration_ancestry_divergence", "closed_dependency",
        "superseded_dependency", "architecture_decision_conflict", "cross_pr_migration_graph_conflict",
        "migration_duplicate_revision", "migration_missing_ancestor", "migration_multiple_heads",
        "migration_cycle", "upstream_head_not_in_child", "ci_failure",
        "merge_conflict", "changes_requested",
    }
    severity_by_kind = {kind: "P1" for kind in blocking_kinds}
    severity_by_kind.update({
        "stale_review": "P2", "security_ownership_overlap": "P2",
        "dependency_unobserved": "P2", "upstream_comparison_unavailable": "P2",
        "ci_failure": "P1", "merge_conflict": "P1", "changes_requested": "P1",
    })
    by_number = {pr["number"]: pr for pr in github.get("pull_requests", [])}
    nodes = result["candidates"]
    for finding in result["findings"]:
        direct = set(finding["pull_requests"])
        finding["downstream_revalidate"] = sorted(
            node["number"] for node in nodes
            if node["number"] not in direct and direct.intersection(node["dependency_chain"])
        )
        finding["severity"] = severity_by_kind.get(finding["kind"], "P3")
        finding["status"] = (
            "UNKNOWN" if finding.get("certainty") == "incomplete_evidence"
            else "OPEN" if finding.get("certainty") in {
                "observed", "observed_cross_branch_conflict", "observed_authoritative_conflict"
            }
            else "UNKNOWN"
        )
        finding["blocks_integration"] = (
            finding["kind"] in blocking_kinds and finding["status"] == "OPEN"
        )
        finding["why_it_matters"] = finding.get("detail", "Integration evidence is incomplete.")
        finding["corrective_action"] = finding.get("recommendation", "Resolve the evidence gap.")
        finding["responsible_owner"] = finding.get("owners", [])
        finding["affected_heads"] = [
            {
                "number": number,
                "head_sha": by_number.get(number, {}).get("head_sha")
                or next((closed.get("head_sha") for closed in github.get("closed_pull_requests", [])
                         if closed.get("number") == number), None),
            }
            for number in finding.get("pull_requests", [])
        ]
    for node in nodes:
        blockers = [
            finding for finding in result["findings"]
            if finding["blocks_integration"] and node["number"] in finding["pull_requests"]
        ]
        if blockers:
            node["readiness"], node["readiness_reason"] = "BLOCKED", blockers[0]["detail"]
        node["integration_blocked"] = bool(blockers)
        node["safe_to_integrate"] = "NO" if blockers else "UNKNOWN"


def parse_authoritative_registry_decision(
    text: str, registry_sha: str, open_prs: list[dict], closed_prs: list[dict]
) -> dict:
    """Accept only the explicit stop-control decision in the pinned Founder-maintained registry."""
    rows = text.splitlines()
    canonical = [
        (index + 1, line) for index, line in enumerate(rows)
        if "[#237]" in line and "architecture selected" in line.lower()
        and "mainai_stop_state" in line and "mainai_stop_events" in line
    ]
    superseded = [
        (index + 1, line) for index, line in enumerate(rows)
        if "[#243]" in line and re.search(r"(?i)closed.{0,50}superseded by #237", line)
    ]
    current = {item["number"]: item for item in open_prs}
    historic = {item["number"]: item for item in closed_prs}
    if len(canonical) != 1 or len(superseded) != 1:
        return {"status": "UNKNOWN", "source_sha": registry_sha, "decisions": []}
    c_line, c_text = canonical[0]
    s_line, s_text = superseded[0]
    c_sha_match = re.search(r"\b[0-9a-f]{40}\b", c_text)
    s_sha_match = re.search(r"\b[0-9a-f]{40}\b", s_text)
    c_pr, s_pr = current.get(237), historic.get(243)
    if (
        not c_sha_match or not s_sha_match or not c_pr or not s_pr
        or c_pr.get("head_sha", "").lower() != c_sha_match.group(0)
        or s_pr.get("head_sha", "").lower() != s_sha_match.group(0)
        or s_pr.get("state") != "closed_unmerged"
    ):
        return {"status": "UNKNOWN", "source_sha": registry_sha, "decisions": []}
    return {
        "status": "verified_record",
        "source_sha": registry_sha,
        "decisions": [{
            "id": "stop_control_architecture",
            "canonical_pr": 237,
            "canonical_sha": c_pr["head_sha"],
            "superseded": [{"number": 243, "head_sha": s_pr["head_sha"]}],
            "evidence": [
                {"path": "docs/BRANCH_REGISTRY.md", "line": c_line,
                 "url": f"https://github.com/d1n095/LifeAI/blob/{registry_sha}/docs/BRANCH_REGISTRY.md#L{c_line}"},
                {"path": "docs/BRANCH_REGISTRY.md", "line": s_line,
                 "url": f"https://github.com/d1n095/LifeAI/blob/{registry_sha}/docs/BRANCH_REGISTRY.md#L{s_line}"},
            ],
        }],
    }


async def fetch_authoritative_registry_decisions(
    client: GitHubClient, github: dict, repo: str
) -> dict:
    branch = github.get("repository", {}).get("default_branch")
    if not isinstance(branch, str) or not branch:
        return {"status": "UNKNOWN", "decisions": [], "error": "default_branch_unavailable"}
    try:
        source_sha = await client.get_ref(branch)
        text = await client.get_branch_registry_at_commit(source_sha)
        if await client.get_ref(branch) != source_sha:
            return {"status": "UNKNOWN", "decisions": [], "error": "registry_ref_changed"}
        canonical_pr, superseded_pr = await asyncio.gather(
            client.get_pull_request(237), client.get_pull_request(243)
        )
        canonical = {
            "number": 237, "head_sha": (canonical_pr.get("head") or {}).get("sha"),
            "state": "open" if canonical_pr.get("state") == "open" else "closed",
        }
        superseded = {
            "number": 243, "head_sha": (superseded_pr.get("head") or {}).get("sha"),
            "state": "merged" if superseded_pr.get("merged_at") else "closed_unmerged",
        }
        parsed = parse_authoritative_registry_decision(
            text, source_sha, [canonical], [superseded],
        )
        if canonical["state"] != "open":
            return {"status": "UNKNOWN", "decisions": [], "error": "canonical_candidate_not_open"}
        if parsed["status"] != "verified_record":
            parsed["error"] = "explicit_current_decision_record_not_found"
        return parsed
    except (GitHubClientError, httpx.RequestError, ValueError, TypeError, KeyError, AttributeError):
        return {"status": "UNKNOWN", "decisions": [], "error": "canonical_registry_unavailable"}


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
                    "schema_effects": parse_migration_schema_effects(source),
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
    github: dict, inspections: list[dict], assignments: list[dict],
    canonical_decisions: dict | None = None,
) -> dict:
    prs = github.get("pull_requests", [])
    by_number = {pr["number"]: pr for pr in prs}
    by_branch, ambiguous_branches = _unique_branch_map(prs)
    closed_by_branch, ambiguous_closed_branches = _unique_branch_map(
        github.get("closed_pull_requests", [])
    )
    scans = {item["number"]: item for item in inspections}
    repo = github.get("repository", {}).get("full_name")
    canonical_decisions = canonical_decisions or {"status": "UNKNOWN", "decisions": []}
    superseded_by = {
        item["number"]: decision["canonical_pr"]
        for decision in canonical_decisions.get("decisions", [])
        for item in decision["superseded"]
    }
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
                replacement = superseded_by.get(dep["number"])
                kind = "superseded_dependency" if replacement else "closed_dependency"
                registry_evidence = [
                    evidence
                    for decision in canonical_decisions.get("decisions", [])
                    if decision["canonical_pr"] == replacement
                    for evidence in decision["evidence"]
                ]
                add(kind, [number, dep["number"], *([replacement] if replacement else [])],
                    f"Dependency #{dep['number']} is closed without merge."
                    + (f" The canonical registry supersedes it with PR #{replacement}." if replacement else ""),
                    [{"url": dep.get("url"), "kind": dep.get("source")}, *registry_evidence],
                    (f"Replace the dependency with the Founder-selected PR #{replacement}, then revalidate downstream."
                     if replacement else "Coordinate with the owners before using this dependency."),
                    ["Verify the replacement dependency and re-run downstream integration tests."])
            elif dep.get("state") == "unknown":
                add("dependency_unobserved", [number], f"Dependency #{dep['number']} is not present in the bounded GitHub observation.",
                    [{"url": dep.get("url"), "kind": dep.get("source"),
                      "observation": "dependency state unavailable"}],
                    "Refresh or inspect the referenced PR; do not assume it is merged or available.",
                    ["Verify the dependency's exact current state and SHA before composing branches."],
                    "incomplete_evidence")
        for claimed_pr in pr.get("canonical_claims", []):
            canonical_target = superseded_by.get(claimed_pr)
            if canonical_target is None:
                continue
            decision = next(
                item for item in canonical_decisions["decisions"]
                if item["canonical_pr"] == canonical_target
            )
            add(
                "architecture_decision_conflict", [number, claimed_pr, canonical_target],
                f"PR #{number} claims superseded PR #{claimed_pr} as canonical; the Founder-approved registry selects PR #{canonical_target}.",
                [{"url": pr.get("url"), "kind": "untrusted_candidate_claim"}, *decision["evidence"]],
                f"Align the proposal with PR #{canonical_target}; do not revive the superseded authority design.",
                ["Check the candidate against the pinned registry decision and test the canonical service integration."],
                "observed_authoritative_conflict",
            )
        reviews = [r for r in pr.get("reviews", []) if r.get("state") in {"APPROVED", "CHANGES_REQUESTED"}]
        stale = [r for r in reviews if r.get("commit_sha") and pr.get("head_sha")
                 and r["commit_sha"].lower() != pr["head_sha"].lower()]
        if stale and pr.get("review_state") == "approval_stale_head":
            add("stale_review", [number], "A GitHub review refers to an earlier commit.",
                [{"url": r.get("url"), "reviewed_sha": r.get("commit_sha"),
                  "head_sha": pr.get("head_sha")} for r in stale],
                "Request an independent review of the exact current SHA.",
                ["Verify reviewed SHA matches the current HEAD."])
        if pr.get("ci_state") == "failed":
            failed_checks = [
                {"name": check.get("name"), "state": check.get("state"),
                 "head_sha": check.get("head_sha"), "url": check.get("url")}
                for check in [*pr.get("checks", []), *pr.get("workflow_runs", [])]
                if check.get("state") in {"failure", "cancelled"}
            ]
            add("ci_failure", [number], "Current exact-HEAD CI evidence contains a failed or cancelled check.",
                failed_checks, "Resolve the failed check and verify a successful run on the current exact HEAD.",
                ["Confirm all required checks pass for the current HEAD SHA."])
        if pr.get("mergeability", {}).get("state") == "CONFLICTING":
            add("merge_conflict", [number], "GitHub reports this PR branch conflicts with its base branch.",
                [{"url": pr.get("url"), "head_sha": pr.get("head_sha"),
                  "mergeability": pr.get("mergeability")}],
                "Coordinate a conflict resolution against the current base, then rerun relevant tests.",
                ["Verify the resolved branch is conflict-free and tests pass at its new exact SHA."])
        if pr.get("review_state") == "changes_requested_exact_head":
            add("changes_requested", [number], "A review requests changes on the current exact HEAD.",
                [{"url": review.get("url"), "reviewed_sha": review.get("commit_sha"),
                  "state": review.get("state")}
                 for review in reviews
                 if review.get("state") == "CHANGES_REQUESTED"
                 and (review.get("commit_sha") or "").lower() == (pr.get("head_sha") or "").lower()],
                "Address the current-SHA review feedback and request a new exact-SHA review.",
                ["Verify the requested changes are addressed and re-review the new HEAD."])
        scan = scans.get(number, {})
        blocked = any(dep.get("state") in {"open", "closed_unmerged"} for dep in dependencies.values())
        blocked = blocked or pr.get("ci_state") == "failed" or pr.get("mergeability", {}).get("state") == "CONFLICTING"
        blocked = blocked or pr.get("review_state") == "changes_requested_exact_head"
        blocked = blocked or any(claim in superseded_by for claim in pr.get("canonical_claims", []))
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
            "required_upstream_candidates": [
                {"number": dep["number"], "state": dep.get("state"),
                 "head_sha": by_number.get(dep["number"], {}).get("head_sha")
                 or next((closed.get("head_sha") for closed in github.get("closed_pull_requests", [])
                          if closed.get("number") == dep["number"]), None)}
                for dep in dependencies.values()
            ],
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
    schema_effects: dict[str, list[tuple[int, dict, dict]]] = defaultdict(list)
    file_owners: dict[str, list[int]] = defaultdict(list)
    schema_file_owners: dict[str, list[int]] = defaultdict(list)
    for scan in inspections:
        for migration in scan.get("migrations", []):
            migrations[migration["revision"]].append((scan["number"], migration))
            for effect in migration.get("schema_effects", []):
                schema_effects[effect["table"]].append((scan["number"], migration, effect))
        for path in scan.get("files", []):
            if any(path == prefix or path.startswith(prefix + "/") for prefix in SECURITY_PATHS):
                file_owners[path].append(scan["number"])
            if path.startswith(("backend/app/models/", "backend/app/schemas/")) and path.endswith(".py"):
                schema_file_owners[path].append(scan["number"])
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
    for table, entries in schema_effects.items():
        by_candidate = {number for number, _, _ in entries}
        if len(by_candidate) < 2:
            continue
        add(
            "schema_change_conflict", sorted(by_candidate),
            f"Multiple active PRs change schema for table {table}; composed DDL compatibility is unverified.",
            [{"number": number, "revision": migration["revision"], "path": migration["path"],
              "operation": effect["operation"], "url": migration["url"]}
             for number, migration, effect in entries],
            "Coordinate a single compatible schema transition and migration ancestry before integration.",
            ["Test fresh install, populated upgrades, constraints, downgrade/upgrade and concurrent application behavior."],
            "risk",
        )
    for path, numbers in file_owners.items():
        if len(set(numbers)) > 1:
            add("security_ownership_overlap", numbers, f"Multiple candidates modify {path}.",
                [{"number": number, "path": path, "url": by_number[number].get("url")}
                 for number in sorted(set(numbers)) if number in by_number],
                "Owners should agree on an integration plan before composing changes.",
                ["Re-run security and concurrency tests on the integrated exact SHA."], "risk")
    for path, numbers in schema_file_owners.items():
        if len(set(numbers)) > 1:
            add("schema_change_overlap", sorted(set(numbers)),
                f"Multiple candidates modify the same schema/model file: {path}.",
                [{"number": number, "path": path,
                  "sha": by_number.get(number, {}).get("head_sha"),
                  "url": f"https://github.com/{repo}/blob/{by_number[number].get('head_sha')}/{quote(path, safe='/')}"
                  if number in by_number else None}
                 for number in sorted(set(numbers))],
                "Compose the exact candidate changes and review the resulting schema constraints and migration compatibility.",
                ["Run schema/model tests and a fresh plus populated Alembic upgrade on the composed candidate."],
                "risk")

    _finalize_findings(
        {"findings": findings, "candidates": nodes}, github
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
            "migration_ancestry": "UNKNOWN — request an exact-HEAD graph scan for up to two PRs",
            "founder_decisions": canonical_decisions.get("status", "UNKNOWN"),
            "canonical_decisions": canonical_decisions,
            "engineering_lessons": "NOT_CHECKED — external-handoff lesson authorization is blocked",
            "independent_security_reviews": "UNKNOWN — GitHub approvals are not independent acceptance",
        },
    }


async def analyze_integration(
    github: dict, assignments: list[dict], numbers: list[int] | None = None,
    include_migration_graph: bool = False,
) -> dict:
    prs = github.get("pull_requests", [])
    selected = [pr for pr in prs if numbers is None or pr["number"] in numbers][:MAX_ANALYZED_PRS]
    repo = github.get("repository", {}).get("full_name")
    if github.get("freshness") in {"stale", "unavailable"} or not repo:
        result = build_integration_graph(
            github, [], assignments, {"status": "UNKNOWN", "decisions": [], "error": "live_github_unavailable"}
        )
        result["coverage"]["inspection_errors"].append({"number": None, "errors": ["live_github_unavailable"]})
        if include_migration_graph:
            result["coverage"]["migration_graph"] = {
                "status": "UNKNOWN", "graphs": [], "errors": ["live_github_unavailable"]
            }
        return result
    client = GitHubClient()
    semaphore = asyncio.Semaphore(4)

    async def inspect(pr: dict) -> dict:
        async with semaphore:
            return await _inspect_pr(client, pr, repo)

    inspections = await asyncio.gather(*(inspect(pr) for pr in selected))
    canonical_decisions = await fetch_authoritative_registry_decisions(client, github, repo)
    result = build_integration_graph(github, inspections, assignments, canonical_decisions)

    def invalidate(number: int) -> None:
        graph_result = result["coverage"].get("migration_graph")
        if graph_result:
            for graph in graph_result.get("graphs", []):
                if graph.get("number") == number:
                    graph.update(status="UNKNOWN", migrations=[], errors=["head_changed_after_graph"])
            result["coverage"]["migration_ancestry"] = "UNKNOWN — HEAD changed; refresh required"
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
            if finding["kind"] in {
                "migration_revision_collision", "migration_ancestry_divergence", "closed_dependency",
                "migration_duplicate_revision", "migration_missing_ancestor", "migration_multiple_heads", "migration_cycle",
                "superseded_dependency", "architecture_decision_conflict",
            }
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
    selection_complete = (
        numbers is None or len(selected) == len(set(numbers))
    )
    if (
        include_migration_graph and selection_complete and selected
        and all(scan["status"] == "observed" for scan in inspections)
    ):
        graphs = await asyncio.gather(*(inspect_migration_graph(client, pr, repo) for pr in selected))
        result["coverage"]["migration_graph"] = {
            "status": "observed" if all(graph["status"] == "observed" for graph in graphs) else "UNKNOWN",
            "graphs": graphs,
        }
        for graph in graphs:
            if any(error.startswith("head_changed_") for error in graph["errors"]):
                invalidate(graph["number"])
        if all(graph["status"] == "observed" for graph in graphs):
            result["coverage"]["migration_ancestry"] = (
                "Observed selected exact-HEAD version directories only; configuration and other branches are not verified."
            )
            for graph in graphs:
                pr = next(item for item in selected if item["number"] == graph["number"])
                kinds = [
                    ("migration_duplicate_revision", graph.get("duplicates", []), "Duplicate revision IDs"),
                    ("migration_missing_ancestor", graph["missing"], "Missing parent revisions"),
                    ("migration_multiple_heads", graph["heads"] if len(graph["heads"]) > 1 else [], "Multiple revision heads"),
                    ("migration_cycle", graph.get("cycle_revisions", []), "Cyclic or cycle-dependent revisions"),
                ]
                for kind, values, label in kinds:
                    if not values:
                        continue
                    result["findings"].append({
                        "kind": kind, "certainty": "observed", "pull_requests": [pr["number"]],
                        "detail": f"{label} in the selected version directory: {', '.join(values)}.",
                        "evidence": graph["migrations"],
                        "recommendation": "Coordinate a reviewed migration ancestry without rewriting applied history.",
                        "owners": [{"number": pr["number"], **_owner(pr, assignments, repo)}],
                        "tests": ["Test Alembic heads, fresh install and populated upgrade/downgrade."],
                        "downstream_revalidate": [
                            node["number"] for node in result["candidates"]
                            if pr["number"] in node["dependency_chain"]
                        ],
                        "reuse": "UNKNOWN — require an independently verified canonical implementation",
                    })
                    node = next(node for node in result["candidates"] if node["number"] == pr["number"])
                    node["readiness"], node["readiness_reason"] = "BLOCKED", "Observed integration conflict."
            revisions: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
            for graph in graphs:
                for migration in graph["migrations"]:
                    revisions[migration["revision"]].append((graph, migration))
            changed_paths = {
                inspection["number"]: set(inspection.get("files", []))
                for inspection in inspections
            }
            seen_pairs: set[tuple[int, int, str]] = set()
            for revision, entries in revisions.items():
                pairs = []
                for index, (left_graph, left) in enumerate(entries):
                    for right_graph, right in entries[index + 1:]:
                        if left_graph["number"] == right_graph["number"]:
                            continue
                        if (
                            left["path"] not in changed_paths.get(left_graph["number"], set())
                            and right["path"] not in changed_paths.get(right_graph["number"], set())
                        ):
                            continue
                        if (left["path"], tuple(left["down_revision"]), left["content_sha256"]) == (
                            right["path"], tuple(right["down_revision"]), right["content_sha256"]
                        ):
                            continue
                        pair = (left_graph["number"], right_graph["number"], revision)
                        if pair not in seen_pairs:
                            pairs.append((left_graph, left, right_graph, right))
                            seen_pairs.add(pair)
                for left_graph, left, right_graph, right in pairs:
                    numbers_pair = sorted([left_graph["number"], right_graph["number"]])
                    result["findings"].append({
                        "kind": "cross_pr_migration_graph_conflict", "certainty": "observed",
                        "pull_requests": numbers_pair,
                        "detail": f"Revision {revision} differs across selected PR migration graphs.",
                        "evidence": [{"number": left_graph["number"], **left},
                                     {"number": right_graph["number"], **right}],
                        "recommendation": "Coordinate one migration definition and ancestry; preserve already-applied history.",
                        "owners": [
                            {"number": left_graph["number"], **_owner(next(p for p in selected if p["number"] == left_graph["number"]), assignments, repo)},
                            {"number": right_graph["number"], **_owner(next(p for p in selected if p["number"] == right_graph["number"]), assignments, repo)},
                        ],
                        "tests": ["Compose the exact PR heads, then verify Alembic heads, fresh install, populated upgrade and downgrade/upgrade."],
                        "downstream_revalidate": sorted(
                            node["number"] for node in result["candidates"]
                            if set(numbers_pair).intersection(node["dependency_chain"])
                        ),
                        "reuse": "UNKNOWN — require an independently verified canonical implementation",
                    })
                    for number in numbers_pair:
                        node = next(node for node in result["candidates"] if node["number"] == number)
                        node["readiness"], node["readiness_reason"] = "BLOCKED", "Observed cross-PR migration graph conflict."
            changed_effects: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
            for graph in graphs:
                for migration in graph["migrations"]:
                    if migration["path"] not in changed_paths.get(graph["number"], set()):
                        continue
                    for effect in migration.get("schema_effects", []):
                        changed_effects[effect["table"]].append((graph, migration | effect))
            for table, entries in changed_effects.items():
                if len({graph["number"] for graph, _ in entries}) < 2:
                    continue
                numbers_pair = sorted({graph["number"] for graph, _ in entries})
                result["findings"].append({
                    "kind": "cross_pr_schema_change_overlap", "certainty": "risk",
                    "pull_requests": numbers_pair,
                    "detail": f"Selected PRs change schema for table {table}; compatibility of the composed DDL is unverified.",
                    "evidence": [{"number": graph["number"], **migration} for graph, migration in entries],
                    "recommendation": "Review the combined schema transition and migration order before integration.",
                    "owners": [
                        {"number": number, **_owner(
                            next(p for p in selected if p["number"] == number), assignments, repo
                        )} for number in numbers_pair
                    ],
                    "tests": ["Test fresh install, populated upgrades, constraints and downgrade/upgrade on the composed heads."],
                    "downstream_revalidate": sorted(
                        node["number"] for node in result["candidates"]
                        if set(numbers_pair).intersection(node["dependency_chain"])
                    ),
                    "reuse": "UNKNOWN — inspect exact changed migration bodies.",
                })
    elif include_migration_graph:
        result["coverage"]["migration_graph"] = {
            "status": "UNKNOWN", "graphs": [],
            "errors": ["incomplete_or_unobserved_pr_selection"]
        }
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
    _finalize_findings(result, github)
    return result
