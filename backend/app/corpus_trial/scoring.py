"""Pure scoring functions for the Life Corpus Trial Harness -- each one checks ONE structural
invariant of the already-existing provenance systems (`app.founder_memory`,
`app.diagnosis`) against a read-back snapshot, and returns a list of violation strings (empty
= that dimension passed). These are deliberately generic structural checks, never fixture-
tuned expected outputs -- a scorer here would catch the same violation on ANY corpus, not just
the bundled bootstrap fixtures in `fixtures.py`. See `docs/LIFE_CORPUS_TRIAL_HARNESS.md` for
why that distinction matters (a harness that only recognizes its own fixtures proves nothing
about a real, later, mixed founder corpus).

Every function takes plain snapshots (dicts/simple objects), not live ORM rows or a DB
session -- this lets each dimension be unit-tested in isolation with a hand-built, deliberately
corrupted snapshot, proving the scorer actually detects the violation it claims to check for,
without needing a database at all."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RecordSnapshot:
    """A read-back of one recorded fact, system-agnostic. `text` is whichever immutable
    content field the source system uses (`content` for a founder memory note, `observation`
    for a diagnosis record)."""

    system: str
    record_id: str
    text_submitted: str
    text_read_back: str
    authority_submitted: str
    authority_read_back: str
    basis_submitted: str
    basis_read_back: str
    confidence_submitted: float | None
    confidence_read_back: float | None
    supersedes_id: str | None
    is_current: bool
    is_contradicted: bool  # disputed (founder_memory) or ruled_out (diagnosis)
    contradiction_text_read_back: str | None = None
    # Whether being contradicted removes this record from its system's own "current" query.
    # True for founder_memory (disputed is excluded from status="active"). False for diagnosis
    # (rule_out_diagnosis() reaches a resolved state about the SAME observation -- by that
    # system's own design it stays the current, up-to-date understanding of that lineage, see
    # app.diagnosis.service.list_current_diagnoses()'s own docstring). Not a fixture-specific
    # tuning knob -- it reflects a real, documented difference between the two systems' own
    # semantics, supplied by the caller (harness.py), never guessed by the scorer.
    contradiction_excludes_currency: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


def score_source_preservation(snapshots: list[RecordSnapshot]) -> list[str]:
    """The raw, factual text of a record must never be silently rewritten -- what was
    submitted must be byte-identical to what a later read returns, for every record
    regardless of what later happened to its epistemic stage/status."""

    violations = []
    for s in snapshots:
        if s.text_read_back != s.text_submitted:
            violations.append(f"{s.system}:{s.record_id}: text mutated in place (submitted={s.text_submitted!r}, read_back={s.text_read_back!r})")
        if s.contradiction_text_read_back is not None and s.contradiction_text_read_back != s.text_submitted:
            violations.append(f"{s.system}:{s.record_id}: text changed when contradicted (expected unchanged original {s.text_submitted!r}, got {s.contradiction_text_read_back!r})")
    return violations


def score_attribution_accuracy(snapshots: list[RecordSnapshot]) -> list[str]:
    """`authority`/`basis` must be stored exactly as asserted -- never silently upgraded (e.g.
    an `ai_interpretation` becoming `founder`) or downgraded."""

    violations = []
    for s in snapshots:
        if s.authority_read_back != s.authority_submitted:
            violations.append(f"{s.system}:{s.record_id}: authority drifted from {s.authority_submitted!r} to {s.authority_read_back!r}")
        if s.basis_read_back != s.basis_submitted:
            violations.append(f"{s.system}:{s.record_id}: basis drifted from {s.basis_submitted!r} to {s.basis_read_back!r}")
    return violations


def score_epistemic_distinction(snapshots: list[RecordSnapshot], expected_distinct_authorities: set[str]) -> list[str]:
    """Decision-vs-idea-vs-suggestion-vs-fact-vs-inference must remain genuinely distinct
    stored states, not collapsed into one bucket. Checks that every authority value the corpus
    intentionally exercised is actually present, distinctly, among the read-back rows."""

    seen = {s.authority_read_back for s in snapshots}
    missing = expected_distinct_authorities - seen
    return [f"expected distinct authority {a!r} never appears among read-back records -- epistemic distinction collapsed" for a in sorted(missing)]


def score_contradiction_detection(snapshots: list[RecordSnapshot]) -> list[str]:
    """A contradicted record (disputed / ruled out) must be recorded as a NEW epistemic state
    on the SAME row, never deleted and never silently overwritten with different text."""

    violations = []
    for s in snapshots:
        if s.extra.get("expected_contradicted") and not s.is_contradicted:
            violations.append(f"{s.system}:{s.record_id}: expected to be marked contradicted (disputed/ruled_out) but was not")
        if s.is_contradicted and s.contradiction_text_read_back is None:
            violations.append(f"{s.system}:{s.record_id}: marked contradicted but the row is no longer readable -- looks deleted, not preserved")
    return violations


def score_supersession_detection(snapshots: list[RecordSnapshot]) -> list[str]:
    """A correction must create a NEW row that points back at the OLD one via
    `supersedes_id` -- the old row must remain durably readable with its ORIGINAL text intact,
    never rewritten in place to reflect the correction."""

    by_id = {s.record_id: s for s in snapshots}
    violations = []
    for s in snapshots:
        if s.supersedes_id is None:
            continue
        old = by_id.get(s.supersedes_id)
        if old is None:
            violations.append(f"{s.system}:{s.record_id}: supersedes {s.supersedes_id!r} but that record was not captured in the read-back set at all")
            continue
        if old.text_read_back != old.text_submitted:
            violations.append(f"{s.system}:{s.record_id}: superseding {old.record_id} mutated the old row's own text instead of leaving it untouched")
    return violations


def score_uncertainty_preservation(snapshots: list[RecordSnapshot]) -> list[str]:
    """`unknown` authority/basis and a `None` confidence are genuine, meaningful states --
    never silently defaulted to a false certainty on read-back."""

    violations = []
    for s in snapshots:
        if s.confidence_submitted is None and s.confidence_read_back is not None:
            violations.append(f"{s.system}:{s.record_id}: confidence fabricated on read-back (submitted None, got {s.confidence_read_back})")
        if s.authority_submitted == "unknown" and s.authority_read_back != "unknown":
            violations.append(f"{s.system}:{s.record_id}: 'unknown' authority silently resolved to {s.authority_read_back!r}")
    return violations


def score_current_state_reconstruction(snapshots: list[RecordSnapshot]) -> list[str]:
    """Given the recorded lineage (supersession + contradiction edges), the set of records a
    "what do we currently believe" query returns must be EXACTLY the set that is neither
    superseded by another record nor itself marked contradicted -- computed generically from
    the snapshot graph, never a hardcoded expected id list, so this check is not tied to the
    specific bootstrap fixtures."""

    superseded_ids = {s.supersedes_id for s in snapshots if s.supersedes_id is not None}
    violations = []
    for s in snapshots:
        contradiction_removes_currency = s.is_contradicted and s.contradiction_excludes_currency
        should_be_current = s.record_id not in superseded_ids and not contradiction_removes_currency
        if should_be_current != s.is_current:
            state = "current" if s.is_current else "not current"
            expected = "current" if should_be_current else "not current"
            violations.append(f"{s.system}:{s.record_id}: reported as {state}, but the recorded lineage implies it should be {expected}")
    return violations


SCORERS = {
    "source_preservation": score_source_preservation,
    "attribution_accuracy": score_attribution_accuracy,
    "contradiction_detection": score_contradiction_detection,
    "supersession_detection": score_supersession_detection,
    "uncertainty_preservation": score_uncertainty_preservation,
    "current_state_reconstruction": score_current_state_reconstruction,
}
