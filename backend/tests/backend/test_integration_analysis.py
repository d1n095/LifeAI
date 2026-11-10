from __future__ import annotations

import pytest

from app.integrations import integration_analysis as analysis
from app.integrations.github_client import GitHubClientError, GitHubListResult

SHA_237 = "7878958bfa57fa999122ac3008e9f261233f158e"
SHA_363 = "1d3fcb0440aca0f460330ecacfb99c15809665fa"


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


def test_founder_registry_decision_overrides_pr_claim_and_blocks_superseded_architecture():
    registry_sha = "e" * 40
    registry = "\n".join([
        "| `cursor/mainai-safe-internal-startup` | [#237](https://github.com/d1n095/LifeAI/pull/237) | **Open; `7878958bfa57fa999122ac3008e9f261233f158e`; architecture selected** | canonical `mainai_stop_state`/`mainai_stop_events` direction |",
        "| `claude/kill-switch-authority-epoch-race-fix` | [#243](https://github.com/d1n095/LifeAI/pull/243) | **Closed, superseded by #237**; historical HEAD `3a19c7640ec75b5574cc268ed1c8b76ddba4d43e` |",
    ])
    decision = analysis.parse_authoritative_registry_decision(
        registry, registry_sha,
        [pr(237, SHA_237, "canonical")],
        [{"number": 243, "head_sha": "3a19c7640ec75b5574cc268ed1c8b76ddba4d43e",
          "state": "closed_unmerged", "url": "https://github.com/d1n095/LifeAI/pull/243"}],
    )
    assert decision["status"] == "verified_record"
    candidate = pr(244, "c" * 40, "reconciliation", canonical_claims=[243],
                   dependencies=[{"number": 243, "state": "closed_unmerged",
                                  "url": "https://github.com/d1n095/LifeAI/pull/243",
                                  "source": "explicit_dependency_in_pull_request_body"}],
                   url="https://github.com/d1n095/LifeAI/pull/244")
    result = analysis.build_integration_graph(
        observation([candidate]), [], [], canonical_decisions=decision
    )
    finding = next(f for f in result["findings"] if f["kind"] == "architecture_decision_conflict")
    assert finding["status"] == "OPEN" and finding["blocks_integration"] is True
    assert finding["severity"] == "P1"
    assert result["candidates"][0]["readiness"] == "BLOCKED"
    assert result["candidates"][0]["integration_blocked"] is True
    assert result["candidates"][0]["safe_to_integrate"] == "NO"
    assert any(f["kind"] == "superseded_dependency" for f in result["findings"])
    assert any(item["url"].endswith("docs/BRANCH_REGISTRY.md#L1") for item in finding["evidence"])


def test_registry_decision_is_unknown_if_record_or_exact_pr_sha_is_stale():
    registry = (
        "| [#237] | architecture selected; canonical `mainai_stop_state`/`mainai_stop_events`; "
        "`1111111111111111111111111111111111111111` |\n"
        "| [#243] | Closed, superseded by #237; historical HEAD "
        "`3a19c7640ec75b5574cc268ed1c8b76ddba4d43e` |"
    )
    result = analysis.parse_authoritative_registry_decision(
        registry, "e" * 40, [pr(237, SHA_237, "canonical")],
        [{"number": 243, "head_sha": "3a19c7640ec75b5574cc268ed1c8b76ddba4d43e",
          "state": "closed_unmerged"}],
    )
    assert result["status"] == "UNKNOWN" and result["decisions"] == []


@pytest.mark.asyncio
async def test_registry_fetch_is_exact_sha_pinned_and_requires_stable_default_ref():
    registry = (
        "| [#237] | architecture selected; canonical `mainai_stop_state`/`mainai_stop_events`; "
        f"`{SHA_237}` |\n"
        "| [#243] | Closed, superseded by #237; historical HEAD "
        "`3a19c7640ec75b5574cc268ed1c8b76ddba4d43e` |"
    )

    class Fake:
        refs = 0
        async def get_ref(self, branch):
            assert branch == "main"
            self.refs += 1
            return "e" * 40
        async def get_branch_registry_at_commit(self, sha):
            assert sha == "e" * 40
            return registry
        async def get_pull_request(self, number):
            if number == 237:
                return {"state": "open", "head": {"sha": SHA_237}}
            return {"state": "closed", "merged_at": None,
                    "head": {"sha": "3a19c7640ec75b5574cc268ed1c8b76ddba4d43e"}}

    result = await analysis.fetch_authoritative_registry_decisions(
        Fake(), {**observation([]), "repository": {"full_name": "d1n095/LifeAI",
                                                     "default_branch": "main"}},
        "d1n095/LifeAI",
    )
    assert result["status"] == "verified_record"
    assert result["source_sha"] == "e" * 40


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


def test_conflicting_registered_owners_and_unavailable_data_stay_unknown():
    item = pr(1, "a" * 40, "branch")
    assignments = [{"branch": "branch", "head_sha": "a" * 40, "repository": "d1n095/LifeAI", "agent_key": agent}
                   for agent in ("agent-a", "agent-b")]
    result = analysis.build_integration_graph(observation([item], freshness="stale"), [], assignments)
    assert result["candidates"][0]["owner"]["status"] == "conflicting_assignments"
    assert result["candidates"][0]["owner"]["agent"] is None
    assert result["candidates"][0]["readiness"] == "UNKNOWN"
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
    assert node["owner"]["status"] == "unknown" and node["readiness"] == "UNKNOWN"
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

    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(route.router)
    app.dependency_overrides[get_db] = lambda: object()
    monkeypatch.setattr(route, "_local_project_state", lambda db, owner: {"assignments": []})

    async def github(_force):
        return observation([])

    monkeypatch.setattr(route, "_get_github_observation", github)
    client = TestClient(app)
    assert client.get("/api/github-operations/integration-analysis").status_code == 401
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4(), role=UserRole.member)
    assert client.get("/api/github-operations/integration-analysis").status_code == 403
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=FOUNDER_USER_ID, role=UserRole.founder)
    response = client.get("/api/github-operations/integration-analysis?pr=237&pr=363")
    assert response.status_code == 200
    assert response.json()["coverage"]["inspection_errors"][0]["numbers"] == [237, 363]
    assert client.get("/api/github-operations/integration-analysis?pr=0").status_code == 422
    assert client.get("/api/github-operations/integration-analysis?pr=1&pr=1").status_code == 422
    assert client.get("/api/github-operations/integration-analysis?include_migration_graph=true").status_code == 422
    assert client.get("/api/github-operations/integration-analysis?" + "&".join("pr=1" for _ in range(9))).status_code == 422
