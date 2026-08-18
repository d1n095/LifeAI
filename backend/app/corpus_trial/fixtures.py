"""A small, deliberately mixed, deliberately adversarial test corpus for the Life Corpus
Trial Harness -- NOT the founder's real corpus (that ingestion stays explicitly deferred, see
docs/LIFE_CORPUS_TRIAL_HARNESS.md). Every item below is plain Python data, replayed through
the REAL `app.founder_memory`/`app.diagnosis` recording APIs by `harness.py` -- nothing here
talks to the database directly, so the trial exercises the actual production code path, not a
mock of it.

The corpus is "mixed" on purpose: it interleaves a founder decision, an AI suggestion, an
inferred pattern, an explicitly unknown-provenance statement, a founder correction that
supersedes an earlier note, a disputed note, a proven diagnosis, a ruled-out diagnosis, and a
diagnosis correction -- so no single scoring dimension in `scoring.py` can pass by accident."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class CorpusItem:
    key: str  # stable fixture label, used as the idempotency key
    system: Literal["founder_memory", "diagnosis"]
    action: Literal["record", "dispute", "supersede", "prove", "rule_out"]
    kwargs: dict[str, Any] = field(default_factory=dict)
    supersedes_key: str | None = None  # references another item's `key`, resolved at run time
    expect_contradicted: bool = False


# Deliberately mixed corpus. Ordering matters -- corrections/disputes reference earlier keys.
CORPUS: list[CorpusItem] = [
    # A founder decision -- explicit, highest authority.
    CorpusItem(
        key="founder-decision-deploy-window",
        system="founder_memory",
        action="record",
        kwargs=dict(
            note_type="decision",
            content="Ship the account-erasure worker only during the Tuesday maintenance window.",
            authority="founder",
            basis="manual",
        ),
    ),
    # An AI suggestion -- must never be conflated with a founder decision.
    CorpusItem(
        key="ai-suggestion-retry-backoff",
        system="founder_memory",
        action="record",
        kwargs=dict(
            note_type="observation",
            content="Consider exponential backoff for the outbox worker's retry loop.",
            authority="ai_interpretation",
            basis="ai_interpretation",
        ),
    ),
    # An inferred recurring pattern -- weaker than a founder statement, stronger than a guess.
    CorpusItem(
        key="inferred-pattern-review-cadence",
        system="founder_memory",
        action="record",
        kwargs=dict(
            note_type="recurring_pattern",
            content="Founder tends to review PRs in the morning, not late evening.",
            authority="inferred_pattern",
            basis="inferred",
        ),
    ),
    # Explicitly unknown provenance -- adversarial: text READS like a confident fact, but its
    # authority/basis/confidence must stay unknown/None, never upgraded because it "sounds
    # certain."
    CorpusItem(
        key="unknown-provenance-timezone-claim",
        system="founder_memory",
        action="record",
        kwargs=dict(
            note_type="observation",
            content="The founder is definitely always working from UTC+1.",
            authority="unknown",
            basis="unknown",
            confidence=None,
        ),
    ),
    # A founder correction that supersedes the earlier inferred pattern -- both must survive.
    CorpusItem(
        key="founder-correction-review-cadence",
        system="founder_memory",
        action="supersede",
        supersedes_key="inferred-pattern-review-cadence",
        kwargs=dict(
            note_type="correction",
            content="Founder corrected this: review timing varies, there is no fixed cadence.",
            authority="founder",
            basis="manual",
        ),
    ),
    # A note later disputed without a replacement yet available.
    CorpusItem(
        key="disputed-deploy-window-note",
        system="founder_memory",
        action="record",
        kwargs=dict(
            note_type="observation",
            content="Deploys never happen on Fridays.",
            authority="repeated_founder_preference",
            basis="inferred",
        ),
    ),
    CorpusItem(key="disputed-deploy-window-note", system="founder_memory", action="dispute", expect_contradicted=True),
    # A diagnosis observation with no classification yet -- must default to unknown, not guess.
    CorpusItem(
        key="diag-ci-failure-observed",
        system="diagnosis",
        action="record",
        kwargs=dict(observation="CI job failed immediately after a routine dependency bump."),
    ),
    # The mission's own worked example, replayed as a corpus item: green tests + a transient
    # external 503 must never read back as a code regression.
    CorpusItem(
        key="diag-github-503-hypothesis",
        system="diagnosis",
        action="record",
        kwargs=dict(
            observation="PR's own tests were green; merge attempt failed with GitHub API HTTP 503.",
            hypothesis_category="external_service_failure",
            hypothesis_reasoning="503 is GitHub's own transient-unavailability status.",
            epistemic_stage="hypothesis",
            authority="deterministic_source",
            basis="deterministic",
        ),
    ),
    CorpusItem(key="diag-github-503-hypothesis", system="diagnosis", action="prove"),
    # A hypothesis later ruled out by further evidence -- preserved, not deleted.
    CorpusItem(
        key="diag-flaky-suspected-regression",
        system="diagnosis",
        action="record",
        kwargs=dict(observation="Intermittent test failure on the payments module.", hypothesis_category="code_regression", epistemic_stage="hypothesis"),
    ),
    CorpusItem(key="diag-flaky-suspected-regression", system="diagnosis", action="rule_out", expect_contradicted=True),
    # A diagnosis correction that supersedes an earlier, wrong hypothesis.
    CorpusItem(
        key="diag-timing-first-guess",
        system="diagnosis",
        action="record",
        kwargs=dict(observation="Test occasionally times out under load.", hypothesis_category="unknown", epistemic_stage="hypothesis"),
    ),
    CorpusItem(
        key="diag-timing-corrected",
        system="diagnosis",
        action="supersede",
        supersedes_key="diag-timing-first-guess",
        kwargs=dict(
            observation="Re-investigated: a shared test fixture leaks connection pool slots under concurrency.",
            hypothesis_category="concurrency_timing",
            epistemic_stage="hypothesis",
        ),
    ),
]
