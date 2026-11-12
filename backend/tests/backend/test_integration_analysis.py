from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.integrations import integration_analysis as analysis
from app.integrations.github_client import GitHubClientError, GitHubListResult

SHA_237 = "7878958bfa57fa999122ac3008e9f261233f158e"
SHA_243 = "3a19c7640ec75b5574cc268ed1c8b76ddba4d43e"
SHA_244 = "3ed59b5065e897deba26f1c9ec4febdc4f1ef06d"
SHA_363 = "1d3fcb0440aca0f460330ecacfb99c15809665fa"
FOUNDER_EMAIL = "founder@example.test"


def pr(number, sha, branch, base="main", **changes):
    return {
        "number": number, "head_sha": sha, "head_branch": branch, "base_branch": base,
        "head_repository": "d1n095/LifeAI", "author": "builder",
        "url": f"https://github.com/d1n095/LifeAI/pull/{number}",
        "dependencies": [], "reviews": [], "review_state": "pending_or_not_requested",
        "ci_state": "success", "mergeability": {"state": "UNKNOWN"}, **changes,
    }


def observation(prs, *, closed=None, freshness="live"):
    return {
        "pull_requests": prs, "closed_pull_requests": closed or [], "freshness": freshness,
        "repository": {"full_name": "d1n095/LifeAI"},
        "pagination": {"open_pull_requests_truncated": False},
        "observed_at": "2026-11-10T00:00:00Z",
    }


def scan(number, sha, path, parent="0068"):
    return {
        "number": number, "head_sha": sha, "status": "observed", "errors": [], "files": [path],
        "migrations": [{
            "revision": "0069", "down_revision": [parent], "path": path,
            "url": f"https://github.com/d1n095/LifeAI/blob/{sha}/{path}",
        }],
    }


def founder_decision_note(*, created_by=FOUNDER_EMAIL, approver=FOUNDER_EMAIL, **changes):
    content = {
        "schema": "lifeai.architecture-decision.v1",
        "decision_id": "stop-control",
        "version": 1,
        "repository": "d1n095/LifeAI",
        "approver_email": approver,
        "approved_at": "2026-11-10T12:00:00+00:00",
        "effective_at": "2026-11-10T12:00:00+00:00",
        "summary": "Use the founder-selected mainai stop-state architecture.",
        "affected_modules": ["backend/app/workforce/"],
        "canonical": {
            "pull_request": 237,
            "head_sha": SHA_237,
            "architecture": ["mainai_stop_state", "mainai_stop_events"],
        },
        "rejected_alternatives": [{
            "pull_request": 243,
            "head_sha": SHA_243,
            "label": "workforce_authority_epoch",
            "signals": ["workforce_authority_epoch"],
        }],
        "relevant_pull_requests": [244],
        **changes,
    }
    return SimpleNamespace(
        id="decision-note-1",
        kind="decision",
        status="open",
        content=json.dumps(content),
        source_type="doc",
        source_ref="Founder approval: stop-control decision",
        source_id=None,
        created_by=created_by,
        created_at=datetime(2026, 11, 10, tzinfo=timezone.utc),
    )


def test_real_237_363_revision_collision_is_blocked_with_sources():
    prs = [pr(237, SHA_237, "cursor/mainai-safe-internal-startup"),
           pr(363, SHA_363, "codex/founder-alpha-0069-migration-compat")]
    scans = [
        scan(237, SHA_237, "backend/alembic/versions/0069_mainai_stop_controls.py"),
        scan(363, SHA_363, "backend/alembic/versions/0069_provider_disclosure_local_decision.py"),
    ]
    result = analysis.build_integration_graph(observation(prs), scans, [])
    finding = next(item for item in result["findings"] if item["kind"] == "migration_revision_collision")
    assert finding["pull_requests"] == [237, 363]
    assert all(item["revision"] == "0069" for item in finding["evidence"])
    assert SHA_237 in finding["evidence"][0]["url"] and SHA_363 in finding["evidence"][1]["url"]
    assert all(node["readiness"] == "BLOCKED" for node in result["candidates"])
    assert "UNKNOWN" in result["coverage"]["migration_ancestry"]


def test_divergent_parent_on_same_revision_detected():
    prs = [pr(1, "a" * 40, "a"), pr(2, "b" * 40, "b")]
    scans = [scan(1, "a" * 40, "backend/alembic/versions/0069_same.py"),
             scan(2, "b" * 40, "backend/alembic/versions/0069_same.py", "0067")]
    result = analysis.build_integration_graph(observation(prs), scans, [])
    assert any(item["kind"] == "migration_revision_collision" for item in result["findings"])
    assert any(item["kind"] == "migration_ancestry_divergence" for item in result["findings"])


def test_identical_revision_and_path_is_not_cross_branch_collision():
    prs = [pr(1, "a" * 40, "a"), pr(2, "b" * 40, "b")]
    scans = [scan(1, "a" * 40, "backend/alembic/versions/0069_same.py"),
             scan(2, "b" * 40, "backend/alembic/versions/0069_same.py")]
    result = analysis.build_integration_graph(observation(prs), scans, [])
    assert not any(item["kind"] == "migration_revision_collision" for item in result["findings"])


def test_shared_table_schema_effect_is_unknown_risk_not_confirmed_blocker():
    prs = [pr(1, "a" * 40, "a"), pr(2, "b" * 40, "b")]
    first, second = scan(1, "a" * 40, "backend/alembic/versions/0069_a.py"), scan(
        2, "b" * 40, "backend/alembic/versions/0070_b.py"
    )
    first["migrations"][0]["revision"] = "0069"
    second["migrations"][0]["revision"] = "0070"
    first["migrations"][0]["schema_effects"] = [{"table": "mainai_stop_state", "operation": "create_table"}]
    second["migrations"][0]["schema_effects"] = [{"table": "mainai_stop_state", "operation": "alter_column"}]

    result = analysis.build_integration_graph(observation(prs), [first, second], [])
    finding = next(item for item in result["findings"] if item["kind"] == "schema_change_conflict")
    assert finding["certainty"] == "risk"
    assert finding["status"] == "UNKNOWN"
    assert finding["blocks_integration"] is False
    assert all(candidate["readiness"] == "UNKNOWN" for candidate in result["candidates"])


def test_schema_effect_parser_only_extracts_literal_table_operations():
    effects = analysis.parse_migration_schema_effects(
        'op.create_table("mainai_stop_state")\n'
        'op.alter_column("mainai_stop_state", "stopped", nullable=False)\n'
        'op.create_table(dynamic_table)\n'
        'op.execute("DROP TABLE mainai_stop_state")'
    )
    assert effects == [
        {"table": "mainai_stop_state", "operation": "create_table"},
        {"table": "mainai_stop_state", "operation": "alter_column"},
    ]


def test_stale_review_and_closed_unmerged_base_never_become_ready():
    item = pr(244, "c" * 40, "child", base="superseded",
              review_state="approval_stale_head",
              reviews=[{"state": "APPROVED", "commit_sha": "d" * 40, "url": "https://github.com/d1n095/LifeAI/pull/244#review"}])
    closed = [{"number": 243, "state": "closed_unmerged", "head_branch": "superseded",
               "url": "https://github.com/d1n095/LifeAI/pull/243"}]
    result = analysis.build_integration_graph(observation([item], closed=closed), [], [])
    assert {f["kind"] for f in result["findings"]} >= {"stale_review", "closed_dependency"}
    assert result["candidates"][0]["readiness"] == "BLOCKED"
    assert result["candidates"][0]["dependencies"][0]["number"] == 243


def test_open_stacked_dependency_is_blocked_not_approved_or_ready():
    parent = pr(1, "a" * 40, "upstream")
    child = pr(2, "b" * 40, "downstream", base="upstream")
    result = analysis.build_integration_graph(observation([parent, child]), [], [])
    finding = next(
        item for item in result["findings"]
        if item["kind"] == "open_dependency" and 2 in item["pull_requests"]
    )
    assert finding["status"] == "OPEN" and finding["blocks_integration"] is True
    assert finding["evidence"][0]["head_sha"] == parent["head_sha"]
    assert result["candidates"][1]["readiness"] == "BLOCKED"
    assert result["candidates"][1]["dependencies"] == [{
        "number": 1, "state": "open", "source": "base_branch", "url": parent["url"],
    }]


def test_failed_exact_head_ci_is_an_evidence_backed_integration_blocker():
    item = pr(
        1, "a" * 40, "branch", ci_state="failed",
        checks=[{"name": "tests", "state": "failure", "head_sha": "a" * 40,
                 "url": "https://github.com/d1n095/LifeAI/actions/runs/1"}],
    )
    result = analysis.build_integration_graph(observation([item]), [], [])
    finding = next(f for f in result["findings"] if f["kind"] == "ci_failure")
    assert finding["status"] == "OPEN" and finding["blocks_integration"] is True
    assert finding["affected_heads"] == [{"number": 1, "head_sha": "a" * 40}]
    assert finding["evidence"][0]["head_sha"] == "a" * 40
    assert result["candidates"][0]["safe_to_integrate"] == "NO"


@pytest.mark.asyncio
async def test_approved_decision_overrides_untrusted_claim_and_blocks_superseded_architecture():
    decision = analysis.parse_founder_architecture_decisions(
        [founder_decision_note()], FOUNDER_EMAIL
    )
    assert decision["status"] == "UNKNOWN"
    assert decision["decisions"][0]["status"] == "PENDING_VALIDATION"
    live = observation(
        [pr(237, SHA_237, "canonical")],
        closed=[{"number": 243, "head_sha": SHA_243, "state": "closed_unmerged",
                 "head_branch": "superseded", "url": "https://github.com/d1n095/LifeAI/pull/243"}],
    )

    class Fake:
        async def get_pull_request(self, number):
            raise AssertionError(f"PR #{number} should already be in the observation")

    validated = await analysis.validate_founder_architecture_decisions(Fake(), decision, live)
    assert validated["status"] == "VERIFIED"
    candidate = pr(
        244, SHA_244, "reconciliation", canonical_claims=[243],
        dependencies=[{"number": 243, "state": "closed_unmerged",
                       "url": "https://github.com/d1n095/LifeAI/pull/243",
                       "source": "explicit_dependency_in_pull_request_body"}],
        url="https://github.com/d1n095/LifeAI/pull/244",
    )
    result = analysis.build_integration_graph(
        observation([candidate], closed=live["closed_pull_requests"]), [], [],
        canonical_decisions=validated,
    )
    finding = next(f for f in result["findings"] if f["kind"] == "architecture_decision_conflict")
    assert finding["status"] == "OPEN" and finding["blocks_integration"] is True
    assert finding["severity"] == "P1"
    assert result["candidates"][0]["readiness"] == "BLOCKED"
    assert result["candidates"][0]["safe_to_integrate"] == "NO"
    assert any(f["kind"] == "superseded_dependency" for f in result["findings"])
    assert finding["evidence"][1]["kind"] == "project_note"
    assert finding["evidence"][1]["content_sha256"]


@pytest.mark.asyncio
async def test_founder_decision_requires_creator_approver_and_exact_current_pr_heads():
    assert analysis.parse_founder_architecture_decisions(
        [founder_decision_note(created_by="agent@example.test")], FOUNDER_EMAIL
    )["decisions"] == []
    assert analysis.parse_founder_architecture_decisions(
        [founder_decision_note(approver="agent@example.test")], FOUNDER_EMAIL
    )["decisions"] == []

    class Fake:
        async def get_pull_request(self, number):
            if number == 237:
                return {"state": "open", "head": {"sha": "f" * 40}}
            return {"state": "closed", "merged_at": None, "head": {"sha": SHA_243}}

    validated = await analysis.validate_founder_architecture_decisions(
        Fake(), analysis.parse_founder_architecture_decisions(
            [founder_decision_note()], FOUNDER_EMAIL
        ), observation([]),
    )
    assert validated["status"] == "STALE"
    assert validated["decisions"][0]["status"] == "STALE"


@pytest.mark.asyncio
async def test_truncated_founder_decision_set_cannot_authorize_candidates():
    notes = []
    for index in range(analysis.MAX_FOUNDER_DECISION_NOTES):
        note = founder_decision_note()
        content = json.loads(note.content)
        content["decision_id"] = f"stop-control-{index}"
        note.content = json.dumps(content)
        notes.append(note)
    parsed = analysis.parse_founder_architecture_decisions(notes, FOUNDER_EMAIL)
    assert "decision_note_limit_reached" in parsed["errors"]

    class Fake:
        async def get_pull_request(self, _number):
            raise AssertionError("Truncated decision evidence must not authorize GitHub reads")

    validated = await analysis.validate_founder_architecture_decisions(
        Fake(), parsed, observation([])
    )
    assert validated["status"] == "UNKNOWN"
    assert all(item["status"] == "UNKNOWN" for item in validated["decisions"])


@pytest.mark.asyncio
async def test_ready_requires_complete_live_exact_head_evidence_and_founder_decision():
    candidate = pr(
        237, SHA_237, "canonical", review_state="approved_exact_head",
        reviews=[{"state": "APPROVED", "commit_sha": SHA_237}],
        mergeability={"state": "MERGEABLE"},
    )
    live = observation(
        [candidate],
        closed=[{"number": 243, "head_sha": SHA_243, "state": "closed_unmerged"}],
    )
    decisions = analysis.parse_founder_architecture_decisions(
        [founder_decision_note()], FOUNDER_EMAIL
    )

    class Fake:
        async def get_pull_request(self, number):
            raise AssertionError(f"PR #{number} should already be in the observation")

    verified = await analysis.validate_founder_architecture_decisions(Fake(), decisions, live)
    result = analysis.build_integration_graph(
        live, [{"number": 237, "status": "observed", "migrations": []}], [], verified
    )
    result["_inspections"] = [{"number": 237, "status": "observed", "migrations": []}]
    analysis._finalize_findings(result, live)
    analysis._promote_ready_candidates(result, live)
    assert result["candidates"][0]["readiness"] == "READY"
    assert result["candidates"][0]["safe_to_integrate"] == "YES"

    result["findings"].append({
        "kind": "schema_change_conflict",
        "certainty": "risk",
        "pull_requests": [237],
        "detail": "Schema effects need composed-tree verification.",
        "evidence": [],
        "recommendation": "Verify the composed schema transition.",
        "owners": [],
        "tests": [],
    })
    analysis._finalize_findings(result, live)
    analysis._promote_ready_candidates(result, live)
    assert result["candidates"][0]["readiness"] == "UNKNOWN"
    assert result["candidates"][0]["safe_to_integrate"] == "UNKNOWN"


def test_unverified_decision_claim_remains_unknown_and_does_not_invent_authority():
    decision = analysis.parse_founder_architecture_decisions(
        [founder_decision_note(created_by="agent@example.test")], FOUNDER_EMAIL
    )
    candidate = pr(244, SHA_244, "reconciliation", canonical_claims=[243])
    result = analysis.build_integration_graph(
        observation([candidate]), [], [], canonical_decisions=decision
    )
    assert result["coverage"]["founder_decisions"] == "UNKNOWN"
    assert not any(f["kind"] == "architecture_decision_conflict" for f in result["findings"])
    assert result["candidates"][0]["readiness"] == "UNKNOWN"


def test_future_effective_founder_decision_does_not_become_authoritative():
    note = founder_decision_note()
    content = json.loads(note.content)
    content["effective_at"] = "2999-01-01T00:00:00+00:00"
    note.content = json.dumps(content)
    decision = analysis.parse_founder_architecture_decisions([note], FOUNDER_EMAIL)
    assert decision["decisions"] == []
    assert "decision_not_yet_effective" in decision["errors"]


def test_stale_review_evidence_is_stale_but_missing_review_is_unknown():
    stale = pr(1, "a" * 40, "stale", review_state="approval_stale_head",
               reviews=[{"state": "APPROVED", "commit_sha": "b" * 40}])
    missing = pr(2, "c" * 40, "missing")
    result = analysis.build_integration_graph(observation([stale, missing]), [], [])
    assert result["candidates"][0]["readiness"] == "STALE"
    assert next(f for f in result["findings"] if f["kind"] == "stale_review")["status"] == "STALE"
    assert result["candidates"][1]["readiness"] == "UNKNOWN"
    assert not any(f["kind"] == "ci_failure" for f in result["findings"])


def test_stale_github_snapshot_never_promotes_old_failure_or_approval_to_current_blocker():
    item = pr(1, "a" * 40, "branch", ci_state="failed",
              review_state="changes_requested_exact_head")
    result = analysis.build_integration_graph(observation([item], freshness="stale"), [], [])
    assert result["candidates"][0]["readiness"] == "STALE"
    assert not any(f["blocks_integration"] for f in result["findings"])


def test_security_overlap_is_risk_not_confirmed_architecture_decision():
    prs = [pr(237, SHA_237, "canonical"), pr(243, "a" * 40, "alternative")]
    path = "backend/app/workforce/kill_switch.py"
    scans = [dict(scan(237, SHA_237, "backend/alembic/versions/a.py"), files=[path]),
             dict(scan(243, "a" * 40, "backend/alembic/versions/a.py"), files=[path])]
    result = analysis.build_integration_graph(observation(prs), scans, [])
    finding = next(item for item in result["findings"] if item["kind"] == "security_ownership_overlap")
    assert finding["certainty"] == "risk"
    assert result["coverage"]["founder_decisions"] == "UNKNOWN"
    assert "not independent" in result["candidates"][0]["security_review"]


def test_conflicting_registered_owners_and_stale_data_never_become_ready():
    item = pr(1, "a" * 40, "branch")
    assignments = [{"branch": "branch", "head_sha": "a" * 40, "repository": "d1n095/LifeAI", "agent_key": agent}
                   for agent in ("agent-a", "agent-b")]
    result = analysis.build_integration_graph(observation([item], freshness="stale"), [], assignments)
    assert result["candidates"][0]["owner"]["status"] == "conflicting_assignments"
    assert result["candidates"][0]["owner"]["agent"] is None
    assert result["candidates"][0]["readiness"] == "STALE"
    assert result["freshness"] == "stale"


def test_downstream_revalidation_is_transitive_and_ambiguous_heads_are_not_authoritative():
    prs = [pr(1, "a" * 40, "first"),
           pr(2, "b" * 40, "second", base="first"),
           pr(3, "c" * 40, "third", base="second"),
           pr(4, "d" * 40, "other")]
    scans = [scan(1, "a" * 40, "backend/alembic/versions/a.py"),
             scan(4, "d" * 40, "backend/alembic/versions/b.py")]
    result = analysis.build_integration_graph(observation(prs), scans, [])
    finding = next(f for f in result["findings"] if f["kind"] == "migration_revision_collision")
    assert finding["downstream_revalidate"] == [2, 3]
    assert result["candidates"][2]["dependency_chain"] == [1, 2]
    ambiguous = analysis.build_integration_graph(
        observation([prs[0], pr(9, "e" * 40, "first"), prs[1]]), [], []
    )
    assert ambiguous["coverage"]["ambiguous_head_branches"] == ["first"]
    assert ambiguous["candidates"][2]["dependencies"] == []


def test_closed_branch_history_does_not_override_active_or_ambiguous_branches():
    closed = [{"number": 7, "state": "closed_unmerged", "head_branch": "parent", "url": "https://example.invalid/7"},
              {"number": 8, "state": "closed_unmerged", "head_branch": "parent", "url": "https://example.invalid/8"}]
    child = pr(2, "b" * 40, "child", base="parent")
    result = analysis.build_integration_graph(observation([child], closed=closed), [], [])
    assert result["coverage"]["ambiguous_closed_head_branches"] == ["parent"]
    assert not result["findings"]
    parent = pr(1, "a" * 40, "parent")
    result = analysis.build_integration_graph(observation([parent, child], closed=closed[:1]), [], [])
    assert result["candidates"][1]["dependencies"][0]["number"] == 1
    assert not any(f["kind"] == "closed_dependency" for f in result["findings"])


def test_parse_migration_never_executes_code_and_rejects_dynamic_metadata():
    revision, parents = analysis.parse_migration('revision: str = "0069"\ndown_revision = ("0068",)\nraise Exception("never run")')
    assert (revision, parents) == ("0069", ("0068",))
    with pytest.raises(ValueError):
        analysis.parse_migration('revision = str("0069")\ndown_revision = "0068"')
    with pytest.raises(ValueError):
        analysis.parse_migration('revision = "0069"\nrevision = "0070"\ndown_revision = None')


@pytest.mark.asyncio
async def test_real_inspection_path_detects_missing_parent_head_and_only_reads(monkeypatch):
    parent = pr(237, SHA_237, "parent")
    child = pr(363, SHA_363, "child", base="parent")
    calls = []

    class Fake:
        async def get_pull_request(self, number):
            calls.append(("GET", "pull", number))
            return {"head": {"sha": SHA_237 if number == 237 else SHA_363}}

        async def list_pull_request_files(self, number):
            calls.append(("GET", "files", number))
            return GitHubListResult([], 1, False)

        async def compare_commits(self, ancestor, descendant):
            calls.append(("GET", "compare", ancestor, descendant))
            return {"status": "diverged"}

    monkeypatch.setattr(analysis, "GitHubClient", Fake)
    result = await analysis.analyze_integration(observation([parent, child]), [], [237, 363])
    assert any(f["kind"] == "upstream_head_not_in_child" for f in result["findings"])
    assert next(node for node in result["candidates"] if node["number"] == 363)["readiness"] == "BLOCKED"
    assert all(call[0] == "GET" for call in calls)
    assert result["coverage"]["engineering_lessons"].startswith("NOT_CHECKED")


@pytest.mark.asyncio
async def test_changed_head_invalidates_metadata_and_github_errors_are_sanitized():
    item = pr(1, "a" * 40, "branch")

    class Changed:
        async def get_pull_request(self, _number):
            return {"head": {"sha": "b" * 40}}

    result = await analysis._inspect_pr(Changed(), item, "d1n095/LifeAI")
    assert result["migrations"] == [] and result["errors"] == ["head_changed_before_analysis"]

    class Unavailable:
        async def get_pull_request(self, _number):
            raise GitHubClientError("sensitive raw response")

    result = await analysis._inspect_pr(Unavailable(), item, "d1n095/LifeAI")
    assert "sensitive" not in repr(result)
    assert result["status"] == "unknown"


@pytest.mark.asyncio
async def test_changed_head_invalidates_snapshot_review_and_ownership(monkeypatch):
    item = pr(1, "a" * 40, "branch", review_state="approval_stale_head",
              reviews=[{"state": "APPROVED", "commit_sha": "b" * 40}])
    assignments = [{"branch": "branch", "head_sha": "a" * 40,
                    "repository": "d1n095/LifeAI", "agent_key": "agent"}]

    class Changed:
        async def get_pull_request(self, _number):
            return {"head": {"sha": "c" * 40}}

    monkeypatch.setattr(analysis, "GitHubClient", Changed)
    result = await analysis.analyze_integration(observation([item]), assignments)
    node = result["candidates"][0]
    assert node["review_state"] == "unknown_head_changed" and node["reviews"] == []
    assert node["owner"]["status"] == "unknown" and node["readiness"] == "STALE"
    assert not result["findings"]
    assert result["coverage"]["inspection_errors"][0]["errors"] == ["head_changed_before_analysis"]


@pytest.mark.asyncio
async def test_migration_client_restricts_paths_shas_size_and_uses_get(monkeypatch):
    import base64
    import httpx
    from app.integrations.github_client import GitHubClient

    client = GitHubClient()
    monkeypatch.setattr(client.settings, "github_repo", "d1n095/LifeAI")
    monkeypatch.setattr(client.settings, "github_token", "synthetic-token")
    calls = []

    async def request(self, method, url, **kwargs):
        calls.append((method, str(url)))
        return httpx.Response(200, request=httpx.Request(method, url), json={
            "size": 20, "encoding": "base64",
            "content": base64.b64encode(b'revision = "0069"').decode(),
        })

    monkeypatch.setattr(httpx.AsyncClient, "request", request)
    with pytest.raises(ValueError):
        await client.get_file_at_commit("../../secrets", "a" * 40)
    with pytest.raises(ValueError):
        await client.get_file_at_commit("backend/alembic/versions/a.py", "main")
    assert not calls
    result = await client.get_file_at_commit("backend/alembic/versions/a.py", "a" * 40)
    assert result == 'revision = "0069"'
    assert calls[0][0] == "GET" and "ref=" + "a" * 40 in calls[0][1]


@pytest.mark.asyncio
async def test_analysis_fetches_pinned_migrations_and_preserves_input(monkeypatch):
    prs = [pr(237, SHA_237, "stop"), pr(363, SHA_363, "migration")]
    data = observation(prs)
    original = repr(data)
    paths = {
        237: "backend/alembic/versions/0069_mainai_stop_controls.py",
        363: "backend/alembic/versions/0069_provider_disclosure_local_decision.py",
    }
    calls = []

    class Fake:
        async def get_pull_request(self, number):
            calls.append(("GET", "pull", number))
            return {"head": {"sha": SHA_237 if number == 237 else SHA_363}}

        async def list_pull_request_files(self, number):
            calls.append(("GET", "files", number))
            return GitHubListResult([{"filename": paths[number], "status": "added"}], 1, False)

        async def get_file_at_commit(self, path, sha):
            calls.append(("GET", "content", path, sha))
            return 'revision = "0069"\ndown_revision = "0068"'

    monkeypatch.setattr(analysis, "GitHubClient", Fake)
    result = await analysis.analyze_integration(data, [], [237, 363])
    assert next(f for f in result["findings"] if f["kind"] == "migration_revision_collision")
    assert all(call[0] == "GET" for call in calls)
    assert {call[-1] for call in calls if call[1] == "content"} == {SHA_237, SHA_363}
    assert repr(data) == original


@pytest.mark.asyncio
async def test_complete_migration_graph_detects_missing_parents_heads_duplicates_and_cycles(monkeypatch):
    item = pr(1, "a" * 40, "branch")
    sources = {
        "root.py": 'revision = "root"\ndown_revision = None',
        "one.py": 'revision = "one"\ndown_revision = "root"',
        "two.py": 'revision = "two"\ndown_revision = "absent"',
        "duplicate.py": 'revision = "one"\ndown_revision = "root"',
        "cycle.py": 'revision = "cycle"\ndown_revision = "cycle"',
    }

    class Fake:
        async def get_pull_request(self, number):
            return {"head": {"sha": item["head_sha"]}}

        async def list_pull_request_files(self, number):
            return GitHubListResult([], 1, False)

        async def list_migration_directory(self, sha):
            assert sha == item["head_sha"]
            return [{"type": "file", "path": f"backend/alembic/versions/{name}"} for name in sources]

        async def get_file_at_commit(self, path, sha):
            assert sha == item["head_sha"]
            return sources[path.rsplit("/", 1)[-1]]

    monkeypatch.setattr(analysis, "GitHubClient", Fake)
    result = await analysis.analyze_integration(observation([item]), [], [1], True)
    assert {f["kind"] for f in result["findings"]} == {
        "migration_duplicate_revision", "migration_missing_ancestor", "migration_multiple_heads", "migration_cycle"
    }
    assert result["coverage"]["migration_graph"]["status"] == "observed"
    assert result["candidates"][0]["readiness"] == "BLOCKED"


@pytest.mark.asyncio
async def test_two_exact_pr_graphs_detect_cross_pr_revision_collision(monkeypatch):
    paths = {
        237: "backend/alembic/versions/0069_mainai_stop_controls.py",
        363: "backend/alembic/versions/0069_provider_disclosure_local_decision.py",
    }
    prs = [pr(237, SHA_237, "stop"), pr(363, SHA_363, "disclosure")]
    sources = {
        237: {
            "0069_mainai_stop_controls.py": 'revision = "0069"\ndown_revision = "0068"\nop.create_table("shared_table")',
            "0004_legacy.py": 'revision = "0004"\ndown_revision = "0003"\n# branch A',
        },
        363: {
            "0069_provider_disclosure_local_decision.py": 'revision = "0069"\ndown_revision = "0068"\nop.add_column("shared_table", object())',
            "0004_legacy.py": 'revision = "0004"\ndown_revision = "0003"\n# branch B',
        },
    }

    class Fake:
        async def get_pull_request(self, number):
            return {"head": {"sha": SHA_237 if number == 237 else SHA_363}}

        async def list_pull_request_files(self, number):
            return GitHubListResult([{"filename": paths[number], "status": "added"}], 1, False)

        async def list_migration_directory(self, sha):
            number = 237 if sha == SHA_237 else 363
            return [{"type": "file", "path": f"backend/alembic/versions/{name}"} for name in sources[number]]

        async def get_file_at_commit(self, path, sha):
            number = 237 if sha == SHA_237 else 363
            if path in sources[number]:
                return sources[number][path]
            return sources[number][path.rsplit("/", 1)[-1]]

    monkeypatch.setattr(analysis, "GitHubClient", Fake)
    result = await analysis.analyze_integration(
        observation(prs), [], [237, 363], include_migration_graph=True
    )
    findings = [f for f in result["findings"] if f["kind"] == "cross_pr_migration_graph_conflict"]
    assert len(findings) == 1
    finding = findings[0]
    assert finding["pull_requests"] == [237, 363]
    assert {e["revision"] for e in finding["evidence"]} == {"0069"}
    assert {e["number"] for e in finding["evidence"]} == {237, 363}
    schema_finding = next(
        f for f in result["findings"] if f["kind"] == "cross_pr_schema_change_overlap"
    )
    assert schema_finding["status"] == "UNKNOWN"
    assert schema_finding["blocks_integration"] is False
    assert all(node["readiness"] == "BLOCKED" for node in result["candidates"])
    assert result["coverage"]["migration_graph"]["status"] == "observed"


@pytest.mark.asyncio
async def test_partial_requested_graph_selection_stays_unknown(monkeypatch):
    item = pr(1, "a" * 40, "branch")

    class Fake:
        async def get_pull_request(self, number):
            return {"head": {"sha": item["head_sha"]}}

        async def list_pull_request_files(self, number):
            return GitHubListResult([], 1, False)

        async def list_migration_directory(self, sha):
            raise AssertionError("Must not scan an incomplete requested selection")

    monkeypatch.setattr(analysis, "GitHubClient", Fake)
    result = await analysis.analyze_integration(
        observation([item]), [], [1, 2], include_migration_graph=True
    )
    assert result["coverage"]["migration_graph"]["status"] == "UNKNOWN"
    assert result["coverage"]["migration_graph"]["errors"] == ["incomplete_or_unobserved_pr_selection"]


def test_unobserved_dependency_has_evidence_but_does_not_claim_missing_or_ready():
    item = pr(1, "a" * 40, "branch", dependencies=[{
        "number": 99, "state": "unknown", "source": "explicit_dependency_in_pull_request_body",
        "url": "https://github.com/d1n095/LifeAI/pull/99",
    }])
    result = analysis.build_integration_graph(observation([item]), [], [])
    finding = next(f for f in result["findings"] if f["kind"] == "dependency_unobserved")
    assert finding["certainty"] == "incomplete_evidence"
    assert finding["pull_requests"] == [1]
    assert result["candidates"][0]["readiness"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_incomplete_migration_graph_never_claims_missing_ancestor_or_multiple_heads():
    item = pr(1, "a" * 40, "branch")

    class Fake:
        async def get_pull_request(self, number):
            return {"head": {"sha": item["head_sha"]}}

        async def list_migration_directory(self, sha):
            return [{"type": "file", "path": "backend/alembic/versions/a.py"},
                    {"type": "file", "path": "backend/alembic/versions/b.py"}]

        async def get_file_at_commit(self, path, sha):
            if path.endswith("a.py"):
                return 'revision = "a"\ndown_revision = "missing"'
            raise GitHubClientError("unavailable")

    result = await analysis.inspect_migration_graph(Fake(), item, "d1n095/LifeAI")
    assert result["status"] == "UNKNOWN"
    assert result["missing"] == [] and result["heads"] == []
    assert result["errors"] == ["migration_graph_metadata_unavailable"]


@pytest.mark.asyncio
async def test_migration_graph_disallows_unsupported_metadata_and_invalidates_changed_head():
    item = pr(1, "a" * 40, "branch")

    class Fake:
        calls = 0

        async def get_pull_request(self, number):
            self.calls += 1
            return {"head": {"sha": "a" * 40 if self.calls == 1 else "b" * 40}}

        async def list_migration_directory(self, sha):
            return [{"type": "file", "path": "backend/alembic/versions/a.py"}]

        async def get_file_at_commit(self, path, sha):
            return 'revision = "a"\ndown_revision = None\nbranch_labels = "label"'

    result = await analysis.inspect_migration_graph(Fake(), item, "d1n095/LifeAI")
    assert result["status"] == "UNKNOWN" and result["migrations"] == []
    assert "migration_dependency_metadata_unsupported" in result["errors"]
    assert "head_changed_during_graph" in result["errors"]


def test_founder_only_analysis_endpoint_and_bounds(monkeypatch):
    import uuid
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.deps import get_current_user
    from app.founder import FOUNDER_USER_ID
    from app.limiter import limiter
    from app.models.user import UserRole
    from app.routers import github_operations as route

    class FakeDB:
        def execute(self, _statement):
            return SimpleNamespace(scalars=lambda: SimpleNamespace().all)

    db = FakeDB()
    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(route.router)
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(route, "_local_project_state", lambda db, owner: {"assignments": []})
    monkeypatch.setattr(
        db, "execute",
        lambda _statement: SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [founder_decision_note()])
        ),
    )

    async def github(_force):
        return observation([])

    monkeypatch.setattr(route, "_get_github_observation", github)

    async def analyze(github_observation, assignments, numbers, include_graph, decisions):
        result = analysis.build_integration_graph(github_observation, [], assignments, decisions)
        if numbers:
            result["coverage"]["inspection_errors"].append({
                "number": None, "errors": ["requested_pr_not_in_open_observation"], "numbers": numbers,
            })
        return result

    monkeypatch.setattr(route, "analyze_integration", analyze)
    client = TestClient(app)
    assert client.get("/api/github-operations/integration-analysis").status_code == 401
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4(), role=UserRole.member)
    assert client.get("/api/github-operations/integration-analysis").status_code == 403
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=FOUNDER_USER_ID, role=UserRole.founder, email=FOUNDER_EMAIL
    )
    response = client.get("/api/github-operations/integration-analysis?pr=237&pr=363")
    assert response.status_code == 200
    assert response.json()["coverage"]["inspection_errors"][0]["numbers"] == [237, 363]
    assert response.json()["coverage"]["canonical_decisions"]["decisions"][0]["status"] == "PENDING_VALIDATION"
    assert client.get("/api/github-operations/integration-analysis?pr=0").status_code == 422
    assert client.get("/api/github-operations/integration-analysis?pr=1&pr=1").status_code == 422
    assert client.get("/api/github-operations/integration-analysis?include_migration_graph=true").status_code == 422
    assert client.get("/api/github-operations/integration-analysis?" + "&".join("pr=1" for _ in range(9))).status_code == 422
