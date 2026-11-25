"""Phase 2 acceptance: the deep system map never overstates maturity."""

from __future__ import annotations

import json
from pathlib import Path

from app.context_atlas.git_evidence import (
    GitEvidence,
    GitRef,
    PullRequestRef,
    load_pull_requests,
    scan_local_git,
)
from app.context_atlas.system_map import scan_system_map

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_map_discovers_modules_endpoints_and_migration_head():
    system = scan_system_map(REPO_ROOT)

    assert system.counts["modules"] > 40
    assert system.counts["endpoints"] > 100
    assert system.counts["test_files"] > 200
    assert system.migrations_head == "0095"
    assert any(endpoint.router == "health" for endpoint in system.endpoints)
    assert any(endpoint.auth == "founder" for endpoint in system.endpoints)


def test_maturity_distinguishes_wired_tested_and_unknown():
    system = scan_system_map(REPO_ROOT)
    router = system.module("app.routers.library")
    assert router is not None
    assert router.maturity.present is True
    assert router.maturity.wired is True
    assert router.maturity.test_referenced is True
    # A checkout cannot prove review, integration, activation or runtime observation.
    assert router.maturity.reviewed is None
    assert router.maturity.integrated is None
    assert router.maturity.activated is None
    assert router.maturity.observed is None


def test_change_impact_reports_dependents_and_unknowns():
    system = scan_system_map(REPO_ROOT)

    impact = system.impact("app.rls")
    assert impact.direct_dependents
    assert impact.security_notes
    assert impact.unknowns
    assert any("runtime" in unknown for unknown in impact.unknowns)

    trust = system.impact("app.rag.trust")
    assert trust.direct_dependents
    assert trust.tests


def test_map_is_deterministic():
    first = scan_system_map(REPO_ROOT).to_dict()
    second = scan_system_map(REPO_ROOT).to_dict()
    assert first == second
    assert first["execution_authorized"] is False


def test_git_evidence_is_carried_without_claiming_integration():
    evidence = GitEvidence(
        current_branch="freebuff/context-atlas-20261124",
        current_sha="a" * 40,
        dirty_tracked_tree=False,
        local_branches=(GitRef("main", "b" * 40, False),),
        pull_requests=(
            PullRequestRef(501, "open", True, "claude/ws-b", "c" * 40, "integration/x", None, None),
        ),
        captured_at="2026-11-24T00:00:00+00:00",
        source="test",
    )
    system = scan_system_map(REPO_ROOT, git=evidence)
    payload = system.to_dict()

    assert payload["git"]["current_branch"] == "freebuff/context-atlas-20261124"
    assert payload["git"]["pull_requests"][0]["state"] == "open"
    # The map itself still refuses to claim integration from a local ref.
    assert all(
        node["maturity"]["integrated"] is None for node in payload["modules"]
    )


def test_pull_request_capture_is_parsed_and_never_authorizes(tmp_path: Path):
    path = tmp_path / "prs.json"
    path.write_text(
        json.dumps(
            [
                {
                    "number": 503,
                    "state": "open",
                    "draft": True,
                    "head": {"ref": "docs/register", "sha": "d" * 40},
                    "base": {"ref": "claude/real-ai"},
                    "updated_at": "2026-11-24T08:17:11Z",
                }
            ]
        ),
        encoding="utf-8",
    )

    refs = load_pull_requests(path)
    assert len(refs) == 1
    assert refs[0].number == 503
    assert refs[0].draft is True
    assert refs[0].base_ref == "claude/real-ai"


def test_local_git_scan_reads_the_checkout():
    evidence = scan_local_git(REPO_ROOT)
    assert evidence.current_sha
    assert evidence.local_branches
    assert evidence.to_dict()["execution_authorized"] is False
