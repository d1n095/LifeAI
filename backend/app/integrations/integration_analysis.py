"""Bounded, read-only integration analysis over exact GitHub observations.

GitHub text is data, never a decision or an instruction. Missing evidence is not approval.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from app.integrations.github_client import GitHubClient, GitHubClientError

MAX_ANALYZED_PRS = 8
MAX_MIGRATIONS_PER_PR = 12
MAX_GRAPH_MIGRATIONS = 128
MAX_FOUNDER_DECISION_NOTES = 100
MAX_DECISION_REFERENCES = 16
MAX_LIVE_DECISION_PR_READS = 16
MAX_DECISION_NOTE_BYTES = 32768
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


def _bounded_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(character for character in value[:limit] if character.isprintable() or character in "\n\t")


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
        "migration_revision_collision", "migration_ancestry_divergence", "open_dependency", "closed_dependency",
        "superseded_dependency", "architecture_decision_conflict", "cross_pr_migration_graph_conflict",
        "migration_duplicate_revision", "migration_missing_ancestor", "migration_multiple_heads",
        "migration_cycle", "upstream_head_not_in_child", "ci_failure", "ci_cancelled",
        "merge_conflict", "changes_requested",
    }
    severity_by_kind = {kind: "P1" for kind in blocking_kinds}
    severity_by_kind.update({
        "stale_review": "P2", "security_ownership_overlap": "P2",
        "dependency_unobserved": "P2", "upstream_comparison_unavailable": "P2",
        "ci_failure": "P1", "ci_cancelled": "P1",
        "merge_conflict": "P1", "changes_requested": "P1",
    })
    by_number = {pr["number"]: pr for pr in github.get("pull_requests", [])}
    nodes = result["candidates"]
    stale_snapshot = github.get("freshness") == "stale"
    unavailable_snapshot = github.get("freshness") == "unavailable"
    for finding in result["findings"]:
        direct = set(finding["pull_requests"])
        finding["downstream_revalidate"] = sorted(
            node["number"] for node in nodes
            if node["number"] not in direct and direct.intersection(node["dependency_chain"])
        )
        finding["severity"] = severity_by_kind.get(finding["kind"], "P3")
        finding["status"] = (
            "UNKNOWN" if finding.get("certainty") == "incomplete_evidence" or unavailable_snapshot
            else "STALE" if stale_snapshot or finding.get("certainty") == "stale_evidence"
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
        elif stale_snapshot or unavailable_snapshot or any(
            finding["status"] == "STALE" and node["number"] in finding["pull_requests"]
            for finding in result["findings"]
        ) or node.get("review_state") == "unknown_head_changed":
            node["readiness"], node["readiness_reason"] = (
                "STALE", "Relevant GitHub or review evidence is stale; refresh before assessing integration."
            )
        else:
            node["readiness"], node["readiness_reason"] = (
                "UNKNOWN", "No current blocker is established, but complete integration readiness is unverified."
            )
        node["integration_blocked"] = bool(blockers)
        node["safe_to_integrate"] = "NO" if blockers or node["readiness"] == "STALE" else "UNKNOWN"


def _promote_ready_candidates(result: dict, github: dict) -> None:
    if (
        github.get("freshness") != "live"
        or result["coverage"]["canonical_decisions"].get("status") != "VERIFIED"
    ):
        return
    decisions = [
        decision for decision in result["coverage"]["canonical_decisions"].get("decisions", [])
        if decision.get("status") == "VERIFIED"
    ]
    current_prs = {pr["number"]: pr for pr in github.get("pull_requests", [])}
    graphs = {
        graph["number"]: graph
        for graph in result["coverage"].get("migration_graph", {}).get("graphs", [])
    }
    inspections = {scan["number"]: scan for scan in result.get("_inspections", [])}
    for candidate in result["candidates"]:
        pr = current_prs.get(candidate["number"])
        scan = inspections.get(candidate["number"], {})
        if not pr or scan.get("status") != "observed":
            continue
        if (
            candidate["review_state"] != "approved_exact_head"
            or pr.get("ci_state") != "success"
            or pr.get("mergeability", {}).get("state") != "MERGEABLE"
            or any(dep.get("state") != "merged" for dep in candidate["dependencies"])
            or any(
                finding["status"] != "RESOLVED"
                and candidate["number"] in finding["pull_requests"]
                for finding in result["findings"]
            )
        ):
            continue
        if scan.get("migrations") and (
            candidate["number"] not in graphs or graphs[candidate["number"]].get("status") != "observed"
        ):
            continue
        if not any(
            decision["canonical"]["pull_request"] == candidate["number"]
            and decision["canonical"]["head_sha"] == candidate.get("head_sha")
            for decision in decisions
        ):
            continue
        candidate["readiness"] = "READY"
        candidate["readiness_reason"] = (
            "Live exact-HEAD CI, review, mergeability, dependency, migration, and Founder-decision evidence is complete."
        )
        candidate["safe_to_integrate"] = "YES"


def parse_founder_architecture_decisions(notes: list[Any], founder_email: str) -> dict:
    """Project explicit Founder-authored ProjectNote decisions into a bounded contract."""
    decisions: list[dict] = []
    invalid = 0
    not_effective = 0
    truncated = len(notes) >= MAX_FOUNDER_DECISION_NOTES
    founder = founder_email.strip().casefold()
    for note in notes[:MAX_FOUNDER_DECISION_NOTES]:
        get = note.get if isinstance(note, dict) else lambda key, default=None: getattr(note, key, default)
        kind = get("kind", "")
        status = get("status", "")
        kind = getattr(kind, "value", kind)
        status = getattr(status, "value", status)
        if (
            not isinstance(kind, str) or kind.casefold() != "decision"
            or not isinstance(status, str) or status.casefold() != "open"
        ):
            continue
        if str(get("created_by", "")).strip().casefold() != founder:
            continue
        try:
            raw_content = get("content", "")
            if not isinstance(raw_content, str) or len(raw_content.encode("utf-8")) > MAX_DECISION_NOTE_BYTES:
                raise ValueError
            content = json.loads(raw_content)
            if not isinstance(content, dict):
                raise ValueError
            decision_id = content.get("decision_id")
            version = content.get("version")
            repository = content.get("repository")
            approver = content.get("approver_email")
            approved_at = content.get("approved_at")
            effective_at = content.get("effective_at")
            canonical = content.get("canonical")
            alternatives = content.get("rejected_alternatives")
            affected_modules = content.get("affected_modules")
            if (
                content.get("schema") != "lifeai.architecture-decision.v1"
                or not isinstance(decision_id, str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,99}", decision_id)
                or not isinstance(version, int) or isinstance(version, bool) or version < 1
                or not isinstance(repository, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repository)
                or not isinstance(approver, str) or approver.strip().casefold() != founder
                or not isinstance(approved_at, str) or not isinstance(effective_at, str)
                or not isinstance(canonical, dict)
                or not isinstance(alternatives, list)
                or len(alternatives) > MAX_DECISION_REFERENCES
                or not isinstance(affected_modules, list)
                or not affected_modules
                or not all(isinstance(item, str) and item.strip() for item in affected_modules)
            ):
                raise ValueError
            approved_time = datetime.fromisoformat(approved_at.replace("Z", "+00:00"))
            effective_time = datetime.fromisoformat(effective_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            canonical_pr = canonical.get("pull_request")
            canonical_sha = canonical.get("head_sha")
            approved_architecture = canonical.get("architecture")
            if (
                approved_time.tzinfo is None or effective_time.tzinfo is None
                or not isinstance(canonical_pr, int) or isinstance(canonical_pr, bool) or canonical_pr <= 0
                or not isinstance(canonical_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", canonical_sha)
                or not isinstance(approved_architecture, list)
                or not approved_architecture
                or not all(isinstance(item, str) and item.strip() for item in approved_architecture)
            ):
                raise ValueError
            if approved_time > now:
                raise ValueError
            if effective_time > now:
                not_effective += 1
                continue
            rejected = []
            for alternative in alternatives:
                if not isinstance(alternative, dict):
                    raise ValueError
                number, sha = alternative.get("pull_request"), alternative.get("head_sha")
                signals = alternative.get("signals", [])
                if (
                    not isinstance(number, int) or isinstance(number, bool) or number <= 0
                    or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha)
                    or not isinstance(signals, list)
                    or not all(isinstance(item, str) and item.strip() for item in signals)
                ):
                    raise ValueError
                rejected.append({
                    "pull_request": number, "head_sha": sha.lower(),
                    "label": _bounded_text(alternative.get("label"), 240),
                    "rationale": _bounded_text(alternative.get("rationale"), 500),
                    "signals": [item.strip().casefold() for item in signals],
                })
            source_type, source_ref = get("source_type"), get("source_ref")
            note_id = str(get("id") or "")
            if not isinstance(source_type, str) or not isinstance(source_ref, str) or not source_ref.strip() or not note_id:
                raise ValueError
            created_at = get("created_at")
            decisions.append({
                "decision_id": decision_id,
                "version": version,
                "repository": repository,
                "status": "UNVERIFIED",
                "approver": founder_email,
                "approved_at": approved_time.isoformat(),
                "effective_at": effective_time.isoformat(),
                "summary": _bounded_text(content.get("summary"), 1000),
                "affected_modules": [_bounded_text(item.strip(), 256) for item in affected_modules],
                "canonical": {
                    "pull_request": canonical_pr,
                    "head_sha": canonical_sha.lower(),
                    "architecture": [_bounded_text(item.strip(), 256) for item in approved_architecture],
                },
                "rejected_alternatives": rejected,
                "relevant_pull_requests": sorted({
                    canonical_pr, *(item["pull_request"] for item in rejected),
                    *(number for number in content.get("relevant_pull_requests", [])[:MAX_DECISION_REFERENCES]
                      if isinstance(number, int) and not isinstance(number, bool) and number > 0),
                }),
                "source": {
                    "kind": "project_note",
                    "id": note_id,
                    "type": _bounded_text(source_type, 80),
                    "ref": _bounded_text(source_ref, 256),
                    "source_id": str(get("source_id")) if get("source_id") else None,
                    "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else None,
                    "content_sha256": hashlib.sha256(raw_content.encode("utf-8")).hexdigest(),
                },
            })
        except (ValueError, TypeError, AttributeError, UnicodeEncodeError):
            invalid += 1
    grouped: dict[str, list[dict]] = defaultdict(list)
    for decision in decisions:
        grouped[decision["decision_id"]].append(decision)
    conflicted = {key for key, entries in grouped.items() if len(entries) != 1}
    for decision in decisions:
        if decision["decision_id"] not in conflicted:
            decision["status"] = "PENDING_VALIDATION"
    return {
        "status": "UNKNOWN",
        "decisions": [item for item in decisions if item["decision_id"] not in conflicted],
        "errors": (["duplicate_active_decision_id"] if conflicted else [])
        + (["invalid_or_unapproved_decision_record"] if invalid else [])
        + (["decision_not_yet_effective"] if not_effective else [])
        + (["decision_note_limit_reached"] if truncated else []),
    }


async def validate_founder_architecture_decisions(
    client: GitHubClient, decision_set: dict, github: dict
) -> dict:
    """Bind each Founder record to live GitHub heads before it can affect readiness."""
    output = {"status": "UNKNOWN", "decisions": [], "errors": list(decision_set.get("errors", []))}
    if output["errors"]:
        output["decisions"] = [
            {**item, "status": "UNKNOWN"} for item in decision_set.get("decisions", [])
        ]
        return output
    candidate_map = {
        item["number"]: {
            "head_sha": item.get("head_sha"),
            "state": "open",
        } for item in github.get("pull_requests", [])
    }
    candidate_map.update({
        item["number"]: {
            "head_sha": item.get("head_sha"),
            "state": item.get("state"),
        } for item in github.get("closed_pull_requests", [])
    })
    repository = github.get("repository", {}).get("full_name")
    in_scope_decisions = []
    for decision in decision_set.get("decisions", []):
        if not isinstance(repository, str) or decision["repository"].casefold() != repository.casefold():
            decision["status"] = "OUT_OF_SCOPE"
            decision["repository_evidence"] = {
                "expected": repository,
                "recorded": decision["repository"],
            }
        else:
            in_scope_decisions.append(decision)
    references = {
        number
        for decision in in_scope_decisions
        for number in [
            decision["canonical"]["pull_request"],
            *(item["pull_request"] for item in decision["rejected_alternatives"]),
        ]
    }
    missing = references - candidate_map.keys()
    if len(missing) > MAX_LIVE_DECISION_PR_READS:
        return {
            **output,
            "decisions": [
                {**item, "status": "OUT_OF_SCOPE"}
                if item["repository"].casefold() != str(repository or "").casefold()
                else {**item, "status": "UNKNOWN"}
                for item in decision_set.get("decisions", [])
            ],
            "errors": output["errors"] + ["decision_reference_limit"],
        }

    async def read_pull_request(number: int) -> tuple[int, dict | None]:
        try:
            data = await client.get_pull_request(number)
            state = "open" if data.get("state") == "open" else (
                "merged" if data.get("merged_at") else "closed_unmerged"
            )
            return number, {"head_sha": (data.get("head") or {}).get("sha"), "state": state}
        except (GitHubClientError, httpx.RequestError, ValueError, TypeError, KeyError, AttributeError):
            return number, None

    if missing:
        read_results = await asyncio.gather(*(read_pull_request(number) for number in sorted(missing)))
        candidate_map.update({number: value for number, value in read_results if value is not None})
    output["decisions"].extend(
        decision for decision in decision_set.get("decisions", [])
        if decision.get("status") == "OUT_OF_SCOPE"
    )
    for decision in in_scope_decisions:
        mismatched = False
        unavailable = False
        canonical = decision["canonical"]
        refs = [(canonical["pull_request"], canonical["head_sha"], "canonical")]
        refs.extend(
            (item["pull_request"], item["head_sha"], "rejected")
            for item in decision["rejected_alternatives"]
        )
        for number, expected_sha, kind in refs:
            observed = candidate_map.get(number)
            observed_sha = observed.get("head_sha") if observed else None
            if not isinstance(observed_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", observed_sha):
                unavailable = True
                continue
            if observed_sha.lower() != expected_sha.lower():
                mismatched = True
            elif kind == "rejected" and observed["state"] == "merged":
                mismatched = True
        decision["status"] = "STALE" if mismatched else (
            "UNKNOWN" if unavailable else "VERIFIED"
        )
        decision["pr_evidence"] = [
            {"number": number, "expected_sha": expected_sha,
             "observed_sha": candidate_map.get(number, {}).get("head_sha"),
             "state": candidate_map.get(number, {}).get("state")}
            for number, expected_sha, _ in refs
        ]
        output["decisions"].append(decision)
    states = {
        item["status"] for item in output["decisions"]
        if item["status"] != "OUT_OF_SCOPE"
    }
    output["status"] = (
        "STALE" if "STALE" in states else
        "VERIFIED" if states and states == {"VERIFIED"} and not output["errors"] else
        "UNKNOWN"
    )
    if any(value is None for value in (candidate_map.get(number) for number in references)):
        output["errors"].append("decision_pr_evidence_unavailable")
    return output


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


def _blueprint_scope(scan: dict, head_sha: str | None, repo: str | None) -> dict:
    observed_sha = scan.get("head_sha")
    if (
        scan.get("status") != "observed"
        or not isinstance(head_sha, str)
        or not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha)
        or not isinstance(observed_sha, str)
        or observed_sha.casefold() != head_sha.casefold()
        or not isinstance(repo, str)
    ):
        return {"status": "UNKNOWN", "head_sha": None, "evidence": []}

    paths = sorted({
        path for path in scan.get("files", [])
        if isinstance(path, str) and "blueprint" in path.casefold()
    })
    return {
        "status": "IN_SCOPE" if paths else "NOT_IN_SCOPE",
        "head_sha": head_sha,
        "evidence": [
            {
                "path": path,
                "url": f"https://github.com/{repo}/blob/{head_sha}/{quote(path, safe='/')}",
            }
            for path in paths
        ],
    }


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
    verified_decisions = [
        decision for decision in canonical_decisions.get("decisions", [])
        if decision.get("status") == "VERIFIED"
    ]
    superseded_by = {
        item["pull_request"]: decision
        for decision in verified_decisions
        for item in decision["rejected_alternatives"]
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
        head_sha = pr.get("head_sha")
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
            if dep.get("state") == "open":
                add(
                    "open_dependency", [number, dep["number"]],
                    f"PR #{number} is stacked on open PR #{dep['number']}; that upstream change is not integrated.",
                    [{"url": dep.get("url"), "kind": dep.get("source"),
                      "head_sha": by_number.get(dep["number"], {}).get("head_sha")}],
                    "Keep the dependency explicit; wait for its integration, then revalidate this candidate at its exact HEAD.",
                    ["Verify the upstream change is merged and present before assessing downstream readiness."],
                )
            elif dep.get("state") == "closed_unmerged":
                decision = superseded_by.get(dep["number"])
                replacement = decision["canonical"]["pull_request"] if decision else None
                kind = "superseded_dependency" if decision else "closed_dependency"
                decision_evidence = [decision["source"]] if decision else []
                add(kind, [number, dep["number"], *([replacement] if replacement else [])],
                    f"Dependency #{dep['number']} is closed without merge."
                    + (f" Founder decision {decision['decision_id']} v{decision['version']} supersedes it with PR #{replacement}."
                       if decision else ""),
                    [{"url": dep.get("url"), "kind": dep.get("source")}, *decision_evidence],
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
        scan = scans.get(number, {})
        changed_paths = [str(path).casefold() for path in scan.get("files", [])]
        changed_paths.extend(
            str(migration.get("path", "")).casefold() for migration in scan.get("migrations", [])
        )
        dependency_numbers = {dep["number"] for dep in dependencies.values()}
        for decision in canonical_decisions.get("decisions", []):
            alternative = next(
                (item for item in decision.get("rejected_alternatives", [])
                 if item["pull_request"] in set(pr.get("canonical_claims", [])) | dependency_numbers),
                None,
            )
            if alternative is None:
                alternative = next(
                    (item for item in decision.get("rejected_alternatives", [])
                     if any(signal in path for signal in item.get("signals", []) for path in changed_paths)),
                    None,
                )
            if alternative is None:
                continue
            canonical_target = decision["canonical"]["pull_request"]
            if decision.get("status") == "VERIFIED":
                add(
                    "architecture_decision_conflict", [number, alternative["pull_request"], canonical_target],
                    f"PR #{number} depends on or proposes rejected architecture PR #{alternative['pull_request']}; Founder decision {decision['decision_id']} v{decision['version']} selects PR #{canonical_target}.",
                    [{"url": pr.get("url"), "kind": "untrusted_candidate_claim_or_dependency"},
                     decision["source"], *decision.get("pr_evidence", [])],
                    f"Align the proposal with the approved architecture in decision {decision['decision_id']} v{decision['version']}; do not revive the rejected alternative.",
                    ["Verify the changed components match the approved architecture and rerun exact-HEAD integration tests."],
                    "observed_authoritative_conflict",
                )
            elif decision.get("status") == "STALE":
                add(
                    "architecture_decision_evidence_stale", [number, alternative["pull_request"]],
                    f"Founder decision {decision['decision_id']} v{decision['version']} is bound to outdated GitHub PR evidence.",
                    [decision["source"], *decision.get("pr_evidence", [])],
                    "Refresh or supersede the decision record after verifying the referenced exact PR heads.",
                    ["Revalidate every canonical and rejected PR SHA before using this decision."],
                    "stale_evidence",
                )
            else:
                add(
                    "architecture_decision_unverified", [number, alternative["pull_request"]],
                    f"PR #{number} references rejected-alternative candidate #{alternative['pull_request']}, but Founder decision provenance is not fully verified.",
                    [decision.get("source", {}), *decision.get("pr_evidence", [])],
                    "Record or refresh an explicitly Founder-approved versioned decision; do not infer authority from PR text.",
                    ["Verify Founder approval, decision source, version, and referenced exact PR heads."],
                    "incomplete_evidence",
                )
        reviews = [r for r in pr.get("reviews", []) if r.get("state") in {"APPROVED", "CHANGES_REQUESTED"}]
        stale = [r for r in reviews if r.get("commit_sha") and pr.get("head_sha")
                 and r["commit_sha"].lower() != pr["head_sha"].lower()]
        if stale and pr.get("review_state") == "approval_stale_head":
            add("stale_review", [number], "A GitHub review refers to an earlier commit.",
                [{"url": r.get("url"), "reviewed_sha": r.get("commit_sha"),
                  "head_sha": pr.get("head_sha")} for r in stale],
                "Request an independent review of the exact current SHA.",
                ["Verify reviewed SHA matches the current HEAD."], "stale_evidence")
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
        elif pr.get("ci_state") == "cancelled":
            cancelled_checks = [
                {"name": check.get("name"), "state": check.get("state"),
                 "head_sha": check.get("head_sha"), "url": check.get("url")}
                for check in [*pr.get("checks", []), *pr.get("workflow_runs", [])]
                if check.get("state") == "cancelled"
            ]
            add("ci_cancelled", [number], "Current exact-HEAD CI evidence contains a cancelled check or workflow.",
                cancelled_checks, "Rerun the cancelled check and verify a successful run on the current exact HEAD.",
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
        stale_observation = github.get("freshness") in {"stale", "unavailable"}
        blocked = not stale_observation and any(
            dep.get("state") in {"open", "closed_unmerged"} for dep in dependencies.values()
        )
        blocked = blocked or (
            not stale_observation and (
                pr.get("ci_state") == "failed"
                or pr.get("ci_state") == "cancelled"
                or pr.get("mergeability", {}).get("state") == "CONFLICTING"
                or pr.get("review_state") == "changes_requested_exact_head"
                or any(claim in superseded_by for claim in pr.get("canonical_claims", []))
            )
        )
        nodes.append({
            "number": number, "head_sha": head_sha, "base_branch": pr.get("base_branch"),
            "url": pr.get("url"), "owner": _owner(pr, assignments, repo),
            "dependencies": list(dependencies.values()), "review_state": pr.get("review_state"),
            "reviews": [{"sha": r.get("commit_sha"), "state": r.get("state"), "url": r.get("url")}
                        for r in reviews],
            "ci_state": pr.get("ci_state", "unknown"),
            "ci_evidence": [
                {
                    "name": item.get("name"),
                    "state": item.get("state"),
                    "head_sha": item.get("head_sha"),
                    "url": item.get("url"),
                }
                for item in [*pr.get("checks", []), *pr.get("workflow_runs", [])]
                if isinstance(head_sha, str)
                and re.fullmatch(r"[0-9a-fA-F]{40}", head_sha)
                and isinstance(item.get("head_sha"), str)
                and item["head_sha"].casefold() == head_sha.casefold()
            ],
            "security_review": "UNKNOWN — GitHub review is not independent security acceptance",
            "migration_scan": scan.get("status", "not_scanned"),
            "migration_dependencies": scan.get("migrations", []),
            "blueprint_scope": _blueprint_scope(scan, head_sha, repo),
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
    include_migration_graph: bool = False, founder_decisions: dict | None = None,
) -> dict:
    prs = github.get("pull_requests", [])
    selected = [pr for pr in prs if numbers is None or pr["number"] in numbers][:MAX_ANALYZED_PRS]
    repo = github.get("repository", {}).get("full_name")
    founder_decisions = founder_decisions or {"status": "UNKNOWN", "decisions": []}
    if github.get("freshness") in {"stale", "unavailable"} or not repo:
        result = build_integration_graph(
            github, [], assignments, {
                **founder_decisions,
                "status": "UNKNOWN",
                "decisions": [{**item, "status": "UNKNOWN"} for item in founder_decisions.get("decisions", [])],
                "error": "live_github_unavailable",
            }
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
    validated_decisions = await validate_founder_architecture_decisions(
        client, founder_decisions, github
    )
    result = build_integration_graph(github, inspections, assignments, validated_decisions)
    result["_inspections"] = inspections

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
    _promote_ready_candidates(result, github)
    result.pop("_inspections", None)
    return result
