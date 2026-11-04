"""Evidence Intelligence -- genuinely new reasoning primitives this codebase does not have
today (confirmed by the reconciliation audit: no existing module models evidence quality,
confounding, denominator/baseline reasoning, or source independence). See
docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the architecture decision.

Pure: no `db`, no I/O anywhere in this module -- these are reasoning primitives a caller applies
to whatever real evidence it already has (from `resource_intelligence`, `capability_reality`,
`why_graph`, or a caller's own observation), never a new evidence STORE of its own.

REPEATED SOURCE != INDEPENDENT EVIDENCE. AUTHORITY != EVIDENCE. POPULAR CONSENSUS != PROOF.
MINORITY CLAIM != SUPPRESSED TRUTH."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class EvidenceState(str, enum.Enum):
    OBSERVED = "OBSERVED"
    DERIVED = "DERIVED"
    INFERRED = "INFERRED"
    PLAUSIBLE = "PLAUSIBLE"
    SPECULATIVE = "SPECULATIVE"
    CONTRADICTED = "CONTRADICTED"
    VERIFIED = "VERIFIED"


@dataclass(frozen=True)
class RawEvidence:
    """One atomic piece of evidence -- a real observation, measurement, or reported result.
    `underlying_source_id` is the KEY field for REPEATED SOURCE != INDEPENDENT EVIDENCE: two
    `RawEvidence` items citing the SAME `underlying_source_id` (e.g. the same study, the same
    log line re-quoted twice, the same founder statement repeated) are the SAME evidence, not
    two independent confirmations -- see `count_independent_sources()` below."""

    evidence_id: str
    state: EvidenceState
    underlying_source_id: str
    population: str | None = None
    time_window: str | None = None
    method: str | None = None
    sample_size: int | None = None
    funding_or_conflict: str | None = None
    mechanism: str | None = None
    replication_of: str | None = None  # another evidence_id this one explicitly replicates
    notes: str | None = None


@dataclass(frozen=True)
class EvidenceClaim:
    """A claim plus the evidence graph around it -- support/contradiction/alternative
    explanations/missing information/uncertainty, never collapsed into a single confidence
    number without also keeping the reasoning that produced it."""

    claim: str
    support: tuple[RawEvidence, ...] = field(default_factory=tuple)
    contradiction: tuple[RawEvidence, ...] = field(default_factory=tuple)
    alternative_explanation: tuple[str, ...] = field(default_factory=tuple)
    missing_information: tuple[str, ...] = field(default_factory=tuple)
    uncertainty: str | None = None
    confidence: float | None = None  # 0..1, caller-supplied, never auto-derived from count alone


def count_independent_sources(evidence: tuple[RawEvidence, ...]) -> int:
    """REPEATED SOURCE != INDEPENDENT EVIDENCE -- the actual count of DISTINCT
    `underlying_source_id` values, never the raw item count. Ten citations of the same one
    study are ONE independent source, not ten."""

    return len({item.underlying_source_id for item in evidence})


def classify_claim_state(claim: EvidenceClaim) -> EvidenceState:
    """Deterministic classification from the claim's own real support/contradiction structure
    -- never an LLM judgment call, matching this program's own established
    deterministic-policy-table convention (`judgment.decide_judgment()`,
    `resource_intelligence.decision.propose_resource_action()`).

    Order (first match wins):
      1. Real, unresolved contradiction present -> CONTRADICTED (even with strong support --
         a contradiction is not silently outvoted by a larger pile of supporting evidence,
         especially if that pile turns out to share one underlying source; see (3)/(4) below).
      2. At least one `VERIFIED`-state support item with NO contradiction -> VERIFIED.
      3. >= 2 INDEPENDENT sources (by `count_independent_sources()`) supporting, no
         contradiction -> DERIVED.
      4. Exactly 1 independent source (however many times repeated) -> INFERRED. POPULAR
         CONSENSUS != PROOF: many citations of the SAME source never promote past INFERRED on
         source-count alone.
      5. Some support exists but is thin/unclear -> PLAUSIBLE.
      6. No support at all -> SPECULATIVE.
    """

    if claim.contradiction:
        return EvidenceState.CONTRADICTED
    if any(item.state == EvidenceState.VERIFIED for item in claim.support):
        return EvidenceState.VERIFIED
    independent = count_independent_sources(claim.support)
    if independent >= 2:
        return EvidenceState.DERIVED
    if independent == 1:
        return EvidenceState.INFERRED
    if claim.support:
        return EvidenceState.PLAUSIBLE
    return EvidenceState.SPECULATIVE


@dataclass(frozen=True)
class ConfoundCheck:
    """Explicit reasoning about WHY a number might mislead -- never silently assumed clean."""

    denominator_disclosed: bool
    baseline_disclosed: bool
    time_window_disclosed: bool
    sample_disclosed: bool
    absolute_and_relative_both_given: bool
    selection_risk: str | None = None
    confounding_risk: str | None = None
    reverse_causation_risk: str | None = None
    survivorship_risk: str | None = None
    definition_drift_risk: str | None = None

    @property
    def fully_disclosed(self) -> bool:
        return (
            self.denominator_disclosed and self.baseline_disclosed and self.time_window_disclosed
            and self.sample_disclosed and self.absolute_and_relative_both_given
        )

    @property
    def open_risks(self) -> tuple[str, ...]:
        return tuple(
            risk for risk in (
                self.selection_risk, self.confounding_risk, self.reverse_causation_risk,
                self.survivorship_risk, self.definition_drift_risk,
            ) if risk
        )


def evaluate_confounds(evidence: RawEvidence, *, absolute_and_relative_both_given: bool = False, **risks: str | None) -> ConfoundCheck:
    """Pure inspection of ONE piece of evidence's own disclosed metadata -- never infers a risk
    that was not actually named by the caller (MISSING != ZERO applies here too: a risk field
    left `None` means "not assessed," never "assessed as absent")."""

    return ConfoundCheck(
        denominator_disclosed=evidence.sample_size is not None,
        baseline_disclosed=evidence.population is not None,
        time_window_disclosed=evidence.time_window is not None,
        sample_disclosed=evidence.sample_size is not None,
        absolute_and_relative_both_given=absolute_and_relative_both_given,
        selection_risk=risks.get("selection_risk"),
        confounding_risk=risks.get("confounding_risk"),
        reverse_causation_risk=risks.get("reverse_causation_risk"),
        survivorship_risk=risks.get("survivorship_risk"),
        definition_drift_risk=risks.get("definition_drift_risk"),
    )


@dataclass(frozen=True)
class MindChangeRecord:
    """'What Changes My Mind' -- a durable-shaped (persisted via `mind_change.py`, composing
    `why_graph`/`FounderMemoryNote`, never a new table) ledger entry for one important
    conclusion."""

    conclusion: str
    confidence: float
    support: tuple[str, ...]
    contradictions: tuple[str, ...]
    assumptions: tuple[str, ...]
    unknowns: tuple[str, ...]
    what_strengthens: tuple[str, ...]
    what_weakens: tuple[str, ...]
    what_reverses: tuple[str, ...]


def mind_change_as_dict(record: MindChangeRecord) -> dict[str, Any]:
    return {
        "conclusion": record.conclusion,
        "confidence": record.confidence,
        "support": list(record.support),
        "contradictions": list(record.contradictions),
        "assumptions": list(record.assumptions),
        "unknowns": list(record.unknowns),
        "what_strengthens": list(record.what_strengthens),
        "what_weakens": list(record.what_weakens),
        "what_reverses": list(record.what_reverses),
    }
