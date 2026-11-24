"""Read-only Context Atlas CLI.

Usage:
    python backend/scripts/context_atlas.py map --repo-root . --out map.json
    python backend/scripts/context_atlas.py map --repo-root . --deep --git-evidence prs.json
    python backend/scripts/context_atlas.py impact app.rag.trust --repo-root .
    python backend/scripts/context_atlas.py recommend --capability memory --repo-root .
    python backend/scripts/context_atlas.py compare left.txt right.txt

Writes nothing except the explicitly requested --out file. No database, network or git write
access; `execution_authorized` is always false in the emitted JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.context_atlas.capability_map import scan_capabilities  # noqa: E402
from app.context_atlas.consistency import check_consistency  # noqa: E402
from app.context_atlas.git_evidence import load_pull_requests, scan_local_git  # noqa: E402
from app.context_atlas.identity import compare_sources  # noqa: E402
from app.context_atlas.project_scope import (  # noqa: E402
    ProjectDefinition,
    ScopeClaim,
    resolve_project_scope,
)
from app.context_atlas.recommendation import (  # noqa: E402
    CapabilityFact,
    EngineeringRequest,
    recommend,
)
from app.context_atlas.system_map import scan_system_map  # noqa: E402


def _emit(payload: dict, out: str | None) -> None:
    text = json.dumps(payload, indent=2, sort_keys=False) + "\n"
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text, encoding="utf-8")
        print(f"wrote {out}")
    else:
        sys.stdout.write(text)


def _cmd_map(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root)
    if args.deep:
        git = None
        if args.git_evidence:
            git = scan_local_git(repo_root, pull_requests_path=Path(args.git_evidence))
        elif args.with_git:
            git = scan_local_git(repo_root)
        payload = scan_system_map(repo_root, git=git).to_dict()
    else:
        payload = scan_capabilities(repo_root).to_dict()
    _emit(payload, args.out)
    return 0


def _cmd_impact(args: argparse.Namespace) -> int:
    system = scan_system_map(Path(args.repo_root))
    try:
        payload = system.impact(args.module).to_dict()
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _emit(payload, args.out)
    return 0


def _cmd_recommend(args: argparse.Namespace) -> int:
    system = scan_system_map(Path(args.repo_root))
    facts = {
        node.module: CapabilityFact(
            component=node.module,
            present=node.maturity.present,
            wired=node.maturity.wired,
            test_referenced=node.maturity.test_referenced,
            path=node.path,
            endpoints=node.endpoints,
        )
        for node in system.modules
    }
    request = EngineeringRequest(
        request_text=args.text or "",
        capability_aliases=tuple(args.capability or ()),
        project_ref=args.project or "unknown",
        active_assignment=args.active_assignment,
        conflicting_sources=args.conflicting,
        unresolved_evidence=tuple(args.unresolved or ()),
        prior_decision=args.prior_decision,
        superseded_by=args.superseded_by,
        other_project=args.other_project,
        implies_existing_failure=args.implies_failure,
        root_cause_verified=args.root_cause_verified,
    )
    _emit(recommend(request, facts).to_dict(), args.out)
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    left = Path(args.left).read_bytes()
    right = Path(args.right).read_bytes()
    assessment = compare_sources(left, right)
    payload = {
        "verdict": assessment.verdict.value,
        "authoritative": assessment.authoritative,
        "reason": assessment.reason,
        "similarity": assessment.similarity,
        "edit_distance": assessment.edit_distance,
        "differences": list(assessment.differences),
        "proposed_relationship": assessment.proposed_relationship,
        "requires_review": assessment.requires_review,
        "preserve_originals": assessment.preserve_originals,
        "left": asdict(assessment.left),
        "right": asdict(assessment.right),
        "execution_authorized": False,
    }
    payload["left"]["media_kind"] = assessment.left.media_kind.value
    payload["right"]["media_kind"] = assessment.right.media_kind.value
    print(json.dumps(payload, indent=2))
    return 0


def _cmd_scope(args: argparse.Namespace) -> int:
    projects_payload = json.loads(Path(args.projects).read_text(encoding="utf-8"))
    claims_payload = json.loads(Path(args.claims).read_text(encoding="utf-8"))
    projects = tuple(
        ProjectDefinition(
            project_id=str(item["project_id"]),
            name=str(item["name"]),
            aliases=tuple(item.get("aliases") or ()),
            archived=bool(item.get("archived", False)),
        )
        for item in projects_payload
    )
    claims = tuple(
        ScopeClaim(
            source_id=str(item["source_id"]),
            project_ref=item.get("project_ref"),
            statement=str(item.get("statement", "")),
            evidence=str(item.get("evidence", "")),
        )
        for item in claims_payload
    )
    _emit(resolve_project_scope(claims, projects).to_dict(), args.out)
    return 0


def _cmd_consistency(args: argparse.Namespace) -> int:
    system = scan_system_map(Path(args.repo_root))
    _emit(
        check_consistency(system, repo_root=Path(args.repo_root)).to_dict(),
        args.out,
    )
    return 0


def _cmd_prs(args: argparse.Namespace) -> int:
    refs = load_pull_requests(Path(args.path))
    _emit(
        {
            "pull_requests": [
                {
                    "number": pr.number,
                    "state": pr.state,
                    "draft": pr.draft,
                    "head_ref": pr.head_ref,
                    "head_sha": pr.head_sha,
                    "base_ref": pr.base_ref,
                    "updated_at": pr.updated_at,
                    "url": pr.url,
                }
                for pr in refs
            ],
            "execution_authorized": False,
        },
        args.out,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="context_atlas")
    sub = parser.add_subparsers(dest="command", required=True)

    map_parser = sub.add_parser("map", help="emit the capability or deep system map")
    map_parser.add_argument("--repo-root", default=".")
    map_parser.add_argument("--out")
    map_parser.add_argument("--deep", action="store_true")
    map_parser.add_argument("--with-git", action="store_true")
    map_parser.add_argument("--git-evidence", help="captured pull-request JSON")
    map_parser.set_defaults(func=_cmd_map)

    impact_parser = sub.add_parser("impact", help="change-impact analysis for one module")
    impact_parser.add_argument("module")
    impact_parser.add_argument("--repo-root", default=".")
    impact_parser.add_argument("--out")
    impact_parser.set_defaults(func=_cmd_impact)

    recommend_parser = sub.add_parser("recommend", help="read-only engineering recommendation")
    recommend_parser.add_argument("--repo-root", default=".")
    recommend_parser.add_argument("--capability", action="append")
    recommend_parser.add_argument("--text")
    recommend_parser.add_argument("--project")
    recommend_parser.add_argument("--active-assignment")
    recommend_parser.add_argument("--conflicting", action="store_true")
    recommend_parser.add_argument("--unresolved", action="append")
    recommend_parser.add_argument("--prior-decision", choices=["approved", "rejected", "postponed", "superseded"])
    recommend_parser.add_argument("--superseded-by")
    recommend_parser.add_argument("--other-project", action="store_true")
    recommend_parser.add_argument("--implies-failure", action="store_true")
    recommend_parser.add_argument("--root-cause-verified", action="store_true")
    recommend_parser.add_argument("--out")
    recommend_parser.set_defaults(func=_cmd_recommend)

    compare_parser = sub.add_parser("compare", help="compare two source files")
    compare_parser.add_argument("left")
    compare_parser.add_argument("right")
    compare_parser.set_defaults(func=_cmd_compare)

    scope_parser = sub.add_parser("scope", help="check explicit project boundaries")
    scope_parser.add_argument("projects", help="reviewed project registry JSON")
    scope_parser.add_argument("claims", help="claims with explicit project_ref JSON")
    scope_parser.add_argument("--out")
    scope_parser.set_defaults(func=_cmd_scope)

    consistency_parser = sub.add_parser("consistency", help="static architecture consistency findings")
    consistency_parser.add_argument("--repo-root", default=".")
    consistency_parser.add_argument("--out")
    consistency_parser.set_defaults(func=_cmd_consistency)

    prs_parser = sub.add_parser("prs", help="normalize captured pull-request metadata")
    prs_parser.add_argument("path")
    prs_parser.add_argument("--out")
    prs_parser.set_defaults(func=_cmd_prs)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
