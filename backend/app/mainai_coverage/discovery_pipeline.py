"""Automatic Coverage / Omission Discovery -- the full real pipeline. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md §A.

DURABLE SOURCES -> SOURCE ADAPTERS -> NORMALIZED OBSERVATIONS -> PROVENANCE -> REQUIREMENT/
CAPABILITY EXTRACTION -> DEDUP/SUPERSESSION -> (caller then runs) COVERAGE TRACEABILITY ->
OMISSION CANDIDATES -> HUMAN/MAINAI REVIEW.

Real, bounded, read-only composition over `source_adapters.py`'s real file/git reads. Every
source that is genuinely available on this branch is ingested for real; `CONVERSATION_HISTORY`
is always reported unavailable (see `source_adapters.conversation_history_availability()`) --
never fabricated. This function itself performs the "HUMAN / MAINAI REVIEW" handoff by
RETURNING candidates for a caller to review/authorize -- it never auto-stages anything itself
(that composition, when wanted, is `dynamic_denominator.stage_omission_for_vision_expansion()`,
called separately and explicitly by a caller after review)."""

from __future__ import annotations

from dataclasses import dataclass

from app.mainai_coverage.requirement_extraction import build_claims_from_observations
from app.mainai_coverage.source_adapters import (
    SourceAdapterError,
    conversation_history_availability,
    discover_docs,
    ingest_branch_registry,
    ingest_git_commit_log,
    ingest_markdown_doc,
    ingest_test_evidence,
)
from app.mainai_coverage.types import CapabilityClaim, NormalizedObservation, SourceAvailability, SourceKind


@dataclass(frozen=True)
class DiscoveryRunReport:
    observations: tuple[NormalizedObservation, ...]
    claims: tuple[CapabilityClaim, ...]
    sources_ingested: tuple[str, ...]
    sources_unavailable: tuple[SourceAvailability, ...]
    sources_failed: tuple[str, ...]  # source identifiers this run expected to read but could not


def run_discovery(
    *,
    docs_dir: str = "docs/mainai_v2",
    branch_registry_path: str = "docs/BRANCH_REGISTRY.md",
    repo_path: str | None = None,
    max_commits: int = 200,
    tests_dir: str | None = None,
    test_keyword: str | None = None,
) -> DiscoveryRunReport:
    """Ingests every REAL source this repo actually has (handoff docs + reconciliation docs
    under `docs_dir`, `docs/BRANCH_REGISTRY.md`, the real git commit log if `repo_path` is
    given, real test-file evidence if `tests_dir`/`test_keyword` are given), and reports
    `CONVERSATION_HISTORY` as explicitly unavailable. A source this run WAS asked to read but
    failed (e.g. a missing expected file) is recorded in `sources_failed`, never silently
    dropped."""

    observations: list[NormalizedObservation] = []
    sources_ingested: list[str] = []
    sources_failed: list[str] = []

    handoff_paths = discover_docs(docs_dir, name_predicate=lambda name: name.startswith("HANDOFF_"))
    reconciliation_paths = discover_docs(docs_dir, name_predicate=lambda name: name.endswith("_RECONCILIATION.md"))

    for path in handoff_paths:
        try:
            observations.extend(ingest_markdown_doc(path, source_kind=SourceKind.HANDOFF_DOC))
            sources_ingested.append(path)
        except SourceAdapterError:
            sources_failed.append(path)

    for path in reconciliation_paths:
        try:
            observations.extend(ingest_markdown_doc(path, source_kind=SourceKind.RECONCILIATION_DOC))
            sources_ingested.append(path)
        except SourceAdapterError:
            sources_failed.append(path)

    try:
        observations.extend(ingest_branch_registry(branch_registry_path))
        sources_ingested.append(branch_registry_path)
    except SourceAdapterError:
        sources_failed.append(branch_registry_path)

    if repo_path is not None:
        try:
            observations.extend(ingest_git_commit_log(repo_path, max_commits=max_commits))
            sources_ingested.append(f"git:{repo_path}")
        except SourceAdapterError:
            sources_failed.append(f"git:{repo_path}")

    if tests_dir is not None and test_keyword is not None:
        observations.extend(ingest_test_evidence(tests_dir, keyword=test_keyword))
        sources_ingested.append(f"tests:{tests_dir}:{test_keyword}")

    claims = build_claims_from_observations(tuple(observations))

    return DiscoveryRunReport(
        observations=tuple(observations), claims=claims, sources_ingested=tuple(sources_ingested),
        sources_unavailable=(conversation_history_availability(),), sources_failed=tuple(sources_failed),
    )
