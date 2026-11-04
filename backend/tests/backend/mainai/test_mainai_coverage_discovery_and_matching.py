"""`app.mainai_coverage.source_adapters` + `matching` + `requirement_extraction` +
`discovery_pipeline` -- Part A (automatic discovery) and Part B (layered matching). See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md."""

from __future__ import annotations

import os

from app.mainai_coverage.discovery_pipeline import run_discovery
from app.mainai_coverage.matching import DEFAULT_ALIAS_GROUPS, layered_match, semantic_similarity_available
from app.mainai_coverage.requirement_extraction import build_claims_from_observations, deduplicate_claims, extract_capability_claims
from app.mainai_coverage.source_adapters import (
    SourceAdapterError,
    conversation_history_availability,
    discover_docs,
    ingest_branch_registry,
    ingest_git_commit_log,
    ingest_markdown_doc,
)
from app.mainai_coverage.types import ExtractionMethod, MatchLayer, NormalizedObservation, SourceKind

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
_DOCS_MAINAI_V2 = os.path.join(_REPO_ROOT, "docs", "mainai_v2")
_BRANCH_REGISTRY = os.path.join(_REPO_ROOT, "docs", "BRANCH_REGISTRY.md")


def test_ingest_markdown_doc_reads_a_real_handoff_file():
    handoffs = discover_docs(_DOCS_MAINAI_V2, name_predicate=lambda n: n.startswith("HANDOFF_"))
    assert len(handoffs) >= 1
    observations = ingest_markdown_doc(handoffs[0], source_kind=SourceKind.HANDOFF_DOC)
    assert len(observations) > 0
    assert all(o.source_kind == SourceKind.HANDOFF_DOC for o in observations)
    assert all(o.extraction_method == ExtractionMethod.MARKDOWN_HEADING_SECTION for o in observations)
    assert any("EXACT SHA" in o.location.upper() for o in observations if o.location)


def test_ingest_markdown_doc_raises_on_missing_file():
    with pytest.raises(SourceAdapterError):
        ingest_markdown_doc("/nonexistent/path/does-not-exist.md", source_kind=SourceKind.HANDOFF_DOC)


def test_ingest_branch_registry_reads_real_table_rows():
    observations = ingest_branch_registry(_BRANCH_REGISTRY)
    assert len(observations) > 0
    assert all(o.source_kind == SourceKind.BRANCH_REGISTRY for o in observations)


def test_ingest_git_commit_log_reads_real_commits_with_real_sha():
    observations = ingest_git_commit_log(_REPO_ROOT, max_commits=5)
    assert len(observations) > 0
    assert all(len(o.exact_sha) == 40 for o in observations)
    assert all(o.source_kind == SourceKind.GIT_COMMIT_LOG for o in observations)


def test_conversation_history_is_explicitly_unavailable_never_fabricated():
    availability = conversation_history_availability()
    assert availability.available is False
    assert availability.source_kind == SourceKind.CONVERSATION_HISTORY
    assert "no durable" in availability.reason


def test_run_discovery_ingests_real_sources_and_discloses_unavailable_ones():
    report = run_discovery(docs_dir=_DOCS_MAINAI_V2, branch_registry_path=_BRANCH_REGISTRY, repo_path=_REPO_ROOT, max_commits=5)
    assert len(report.sources_ingested) > 0
    assert len(report.claims) > 0
    assert report.sources_unavailable[0].source_kind == SourceKind.CONVERSATION_HISTORY
    assert report.sources_unavailable[0].available is False
    assert report.sources_failed == ()


def test_run_discovery_reports_a_missing_expected_source_as_failed_not_silently_dropped():
    report = run_discovery(docs_dir=_DOCS_MAINAI_V2, branch_registry_path="/nonexistent/registry.md", repo_path=None)
    assert "/nonexistent/registry.md" in report.sources_failed


# ---------------------------------------------------------------- matching (§B)


def test_exact_identity_layer():
    result = layered_match("autonomous development", "autonomous development")
    assert result.matched is True
    assert result.layer == MatchLayer.EXACT_IDENTITY


def test_normalized_lexical_layer_ignores_case_and_punctuation():
    result = layered_match("Autonomous Development!", "autonomous development")
    assert result.matched is True
    assert result.layer == MatchLayer.NORMALIZED_LEXICAL


def test_alias_layer_links_known_synonymous_terms():
    result = layered_match("we need better situational awareness", "the agent runtime view already covers this")
    assert result.matched is True
    assert result.layer == MatchLayer.ALIAS


def test_keyword_overlap_is_never_mislabeled_as_semantic():
    result = layered_match("the completion engine tracks maturity states across nodes", "maturity states are tracked across many completion nodes")
    assert result.matched is True
    assert result.layer == MatchLayer.KEYWORD_OVERLAP
    assert "NOT semantic" in result.detail


def test_no_keyword_match_does_not_imply_no_relationship_when_structurally_linked():
    result = layered_match("alpha capability", "totally different wording", a_entity_ids=frozenset({"e1"}), b_entity_ids=frozenset({"e1"}))
    assert result.matched is True
    assert result.layer == MatchLayer.STRUCTURED_LINK


def test_semantic_similarity_is_never_silently_claimed_available():
    assert semantic_similarity_available() is False
    result = layered_match("completely unrelated alpha", "totally unrelated beta")
    assert result.matched is False
    assert result.semantic_similarity_available is False


def test_default_alias_groups_are_bounded_not_a_thesaurus():
    assert 1 <= len(DEFAULT_ALIAS_GROUPS) <= 10


# ---------------------------------------------------------------- dedup/supersession


def _obs(text: str, source_id: str) -> NormalizedObservation:
    return NormalizedObservation(
        source_kind=SourceKind.HANDOFF_DOC, source_identifier=source_id, text=text, location=None,
        timestamp=None, exact_sha=None, extraction_method=ExtractionMethod.MARKDOWN_HEADING_SECTION, confidence=0.8,
    )


def test_two_differently_worded_duplicate_observations_are_linked_and_provenance_retained():
    claims = extract_capability_claims((_obs("Autonomous development capability", "doc-a.md"), _obs("autonomous development capability!", "doc-b.md")))
    merged = deduplicate_claims(claims)
    assert len(merged) == 1
    assert merged[0].mention_count == 2
    assert set(merged[0].provenance) == {"doc-a.md", "doc-b.md"}
    assert len(merged[0].source_records) == 2


def test_two_similar_but_distinct_observations_are_not_incorrectly_deduplicated():
    claims = extract_capability_claims((_obs("revenue grew 10 percent in Q1", "doc-a.md"), _obs("revenue grew 40 percent in Q3", "doc-b.md")))
    merged = deduplicate_claims(claims)
    assert len(merged) == 2


def test_short_numeric_tokens_are_not_dropped_from_keyword_matching():
    """A real bug caught by the test above's own failure during development: "10"/"Q1" are
    short tokens that a bare length filter would drop, silently erasing the ONE thing that
    distinguishes two otherwise similarly-worded claims."""

    from app.mainai_coverage.matching import keyword_overlap_score

    score = keyword_overlap_score("revenue grew 10 percent in Q1", "revenue grew 40 percent in Q3")
    assert score < 1.0


def test_bare_keyword_overlap_alone_never_merges_two_claims_similar_wording_is_not_same_requirement():
    claims = extract_capability_claims((
        _obs("the completion engine tracks maturity states across nodes", "doc-a.md"),
        _obs("maturity states are tracked across many completion nodes", "doc-b.md"),
    ))
    merged = deduplicate_claims(claims)
    assert len(merged) == 2  # KEYWORD_OVERLAP alone is excluded from the merge bar


def test_build_claims_from_observations_is_the_full_extract_and_dedup_pipeline():
    claims = build_claims_from_observations((_obs("alpha capability", "a"), _obs("alpha capability", "b"), _obs("beta capability", "c")))
    assert len(claims) == 2
