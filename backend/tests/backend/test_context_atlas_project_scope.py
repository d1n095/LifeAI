"""C3 acceptance: project boundaries are explicit, deterministic and fail-closed."""

from __future__ import annotations

from app.context_atlas.project_scope import (
    ProjectDefinition,
    ScopeClaim,
    ScopeStatus,
    project_scope_for_refs,
    resolve_project_scope,
)

PROJECTS = (
    ProjectDefinition("lifeai", "LifeAI", ("LifeAI Platform",)),
    ProjectDefinition("l4tp", "L4TP", ("Life 4 The People",)),
    ProjectDefinition("lifemusic", "LifeMusic", ("Life Music",)),
    ProjectDefinition("lifeinvest", "LifeInvest", ("Life Invest",)),
    ProjectDefinition("4thepeople", "4ThePeople", ("4 The People Commerce",)),
    ProjectDefinition("legacy-crm", "Legacy CRM", ("Old CRM",), archived=True),
)


def _claim(source_id: str, ref: str | None, statement: str = "") -> ScopeClaim:
    return ScopeClaim(source_id=source_id, project_ref=ref, statement=statement)


def test_same_project_aliases_are_isolated():
    report = resolve_project_scope(
        (
            _claim("a", "LifeAI"),
            _claim("b", "LifeAI Platform"),
            _claim("c", "lifeai"),
        ),
        PROJECTS,
    )

    assert report.status is ScopeStatus.isolated
    assert report.matched_project_ids == ("lifeai",)
    assert report.allows_implementation is True
    assert report.requires_review is False
    assert report.canonical_truth is None
    assert report.execution_authorized is False


def test_requirements_from_two_projects_are_never_combined():
    report = resolve_project_scope(
        (
            _claim("a", "LifeAI", "add memory provenance"),
            _claim("b", "LifeMusic", "add memory provenance"),
        ),
        PROJECTS,
    )

    assert report.status is ScopeStatus.mixed
    assert set(report.matched_project_ids) == {"lifeai", "lifemusic"}
    assert report.allows_implementation is False
    assert report.requires_review is True
    assert report.canonical_truth is None
    assert report.execution_authorized is False
    assert "similar" in report.reason


def test_missing_project_reference_is_not_assumed_to_belong_here():
    report = resolve_project_scope(
        (
            _claim("a", "LifeAI"),
            _claim("b", None, "the same feature, from an old conversation"),
        ),
        PROJECTS,
    )

    assert report.status is ScopeStatus.partial
    assert report.matched_project_ids == ("lifeai",)
    assert report.allows_implementation is False
    assert report.requires_review is True


def test_unknown_or_archived_historical_project_stays_unplaced():
    report = resolve_project_scope(
        (
            _claim("a", "LifeAI"),
            _claim("b", "Legacy CRM"),
        ),
        PROJECTS,
    )

    # Legacy CRM is a real registry entry, so this is a mixed-project result, not a merge.
    assert report.status is ScopeStatus.mixed
    assert "legacy-crm" in report.matched_project_ids

    unknown = resolve_project_scope((_claim("a", "An Unregistered 2019 Project"),), PROJECTS)
    assert unknown.status is ScopeStatus.unknown
    assert unknown.unmatched_refs == ("An Unregistered 2019 Project",)
    assert unknown.allows_implementation is False


def test_shared_alias_is_ambiguous_and_never_picks_a_winner():
    projects = (
        ProjectDefinition("lifeai", "LifeAI", ("Life",)),
        ProjectDefinition("lifemusic", "LifeMusic", ("Life",)),
    )
    report = resolve_project_scope((_claim("a", "Life"),), projects)

    assert report.status is ScopeStatus.ambiguous
    assert report.ambiguous_refs == ("Life",)
    assert report.matched_project_ids == ()
    assert report.allows_implementation is False
    assert "founder decision" in report.reason


def test_no_claims_or_no_registry_is_unknown_not_isolated():
    no_claims = resolve_project_scope((), PROJECTS)
    assert no_claims.status is ScopeStatus.unknown

    no_registry = resolve_project_scope((_claim("a", "LifeAI"),), ())
    assert no_registry.status is ScopeStatus.unknown
    assert no_registry.allows_implementation is False


def test_similar_wording_does_not_affect_ownership():
    a = _claim("a", "LifeAI", "shared memory and provenance")
    b = _claim("b", "LifeInvest", "shared memory and provenance")
    report = resolve_project_scope((a, b), PROJECTS)

    assert report.status is ScopeStatus.mixed
    assert [claim.statement for claim in report.claims] == [
        "shared memory and provenance",
        "shared memory and provenance",
    ]
    assert report.to_dict()["canonical_truth"] is None
    assert report.to_dict()["execution_authorized"] is False


def test_reconciliation_mapping_fails_closed():
    isolated = project_scope_for_refs(("LifeAI", "LifeAI Platform"), PROJECTS)
    observation = isolated.to_reconciliation_observation()
    assert observation.project_scope == "this_project"
    assert observation.ambiguous is False

    mixed = project_scope_for_refs(("LifeAI", "LifeMusic"), PROJECTS)
    observation = mixed.to_reconciliation_observation()
    assert observation.project_scope == "unknown"
    assert observation.ambiguous is True
    assert observation.evidence_sufficient is False
