"""Reusable evidence-quality assessment — the P4 "scientific reasoning" increment
(Founder Alpha round, 2026-11-18). A deterministic, auditable rubric over STRUCTURED
study metadata, integrated with the P2 epistemics ladder — not a new service, not a
model leg, not a knowledge base.

What it is:
  `grade_evidence()` consumes a plain metadata dict (whoever holds study facts can call
  it: the founder, an import pipeline, a future gated model leg) and returns an
  `EvidenceQualityAssessment` with per-axis findings, an overall grade, explicit caveats,
  and the highest `EpistemicStatus` the evidence can support.

What it deliberately IS NOT (the mission's own anti-bias rules, enforced structurally):
  - A speculative MECHANISM is never graded as established harm: mechanism-only /
    in-vitro evidence caps the epistemic ladder at hypothesis with a named caveat.
  - A funding CONFLICT reduces confidence by one axis and is always surfaced as a
    caveat — it is never "automatic proof of invalidity": the grade floors at `low`
    (not `rejected`) when every other axis is sound, and the report says so.
  - Correlation is never silently promoted to causation: causal uncertainty is its own
    axis, and correlation-only evidence caps at hypothesis.

Grades (closed vocabulary, ordered): `high > moderate > low > very_low > unassessable`.
`unassessable` = the metadata needed to say anything is absent — an honest "unknown",
never a guess dressed as a grade. EVERY unknown input field produces an explicit caveat;
silence about missing evidence is exactly the failure mode this module exists to remove.

Axis set (mission): study design, exposure duration, dose, interactions,
conflicts of interest, competing explanations, causal uncertainty. Each axis is
independent: a poor axis cannot be outvoted by strong others (mirrors
assess_claim_confidence's "a contradicts edge is never outvoted" rule), and axis
findings are reported per-axis so a reviewer can disagree with the aggregate while
keeping the evidence trail.

Integration: `epistemic_cap()` feeds app/rag/epistemics.py's ladder (classify_claim's
optional `quality_cap`), and `format_quality_instructions()` renders the assessment for
prompt-building layers the way app/rag/trust.py's build_trust_instructions() already
does for source trust. No redundancy: both reuse existing vocabularies.
"""

import enum
from dataclasses import dataclass, field

from app.rag.epistemics import EpistemicStatus


class StudyDesign(str, enum.Enum):
    """Closed design vocabulary, ordered by the strength of causal inference the design
    can carry. `unknown` is a real value: missing design metadata is reported, never
    inferred."""

    meta_analysis = "meta_analysis"
    randomized_trial = "randomized_trial"
    prospective_cohort = "prospective_cohort"
    retrospective_cohort = "retrospective_cohort"
    case_control = "case_control"
    case_series = "case_series"
    in_vitro = "in_vitro"
    mechanism_only = "mechanism_only"
    unknown = "unknown"


class QualityGrade(str, enum.Enum):
    high = "high"
    moderate = "moderate"
    low = "low"
    very_low = "very_low"
    unassessable = "unassessable"


# Design → the ceiling it puts on the overall grade. Mechanistic-only work can never
# produce more than very_low-grade evidence ABOUT OUTCOMES — that is the structural
# form of "a speculative mechanism is not established harm".
_DESIGN_CEILING: dict[StudyDesign, QualityGrade] = {
    StudyDesign.meta_analysis: QualityGrade.high,
    StudyDesign.randomized_trial: QualityGrade.high,
    StudyDesign.prospective_cohort: QualityGrade.moderate,
    StudyDesign.retrospective_cohort: QualityGrade.moderate,
    StudyDesign.case_control: QualityGrade.low,
    StudyDesign.case_series: QualityGrade.low,
    StudyDesign.in_vitro: QualityGrade.very_low,
    StudyDesign.mechanism_only: QualityGrade.very_low,
}

# The highest EpistemicStatus each design can support, regardless of other axes.
_DESIGN_EPISTEMIC_CAP: dict[StudyDesign, EpistemicStatus] = {
    StudyDesign.meta_analysis: EpistemicStatus.verified_fact,
    StudyDesign.randomized_trial: EpistemicStatus.verified_fact,
    StudyDesign.prospective_cohort: EpistemicStatus.verified_fact,  # causality still gated by the causal axis below
    StudyDesign.retrospective_cohort: EpistemicStatus.verified_fact,
    StudyDesign.case_control: EpistemicStatus.hypothesis,
    StudyDesign.case_series: EpistemicStatus.hypothesis,
    StudyDesign.in_vitro: EpistemicStatus.hypothesis,
    StudyDesign.mechanism_only: EpistemicStatus.hypothesis,
    StudyDesign.unknown: EpistemicStatus.hypothesis,
}

_LADDER = [QualityGrade.high, QualityGrade.moderate, QualityGrade.low, QualityGrade.very_low]


def _floor(grade: QualityGrade, floor: QualityGrade) -> QualityGrade:
    """Push `grade` DOWN to `floor` if it currently sits above it (earlier in the ladder
    = stronger). Unknown/unassessable never participates."""
    if grade == QualityGrade.unassessable or floor == QualityGrade.unassessable:
        return grade
    if _LADDER.index(grade) > _LADDER.index(floor):
        return grade
    return floor if _LADDER.index(floor) > _LADDER.index(grade) else grade


def _raise_cap(grade: QualityGrade, ceiling: QualityGrade) -> QualityGrade:
    """Pull `grade` down to the design ceiling (a ceiling is a maximum, not a floor):
    a weaker ceiling (later in the ladder) always wins over a stronger grade."""
    if grade == QualityGrade.unassessable:
        return grade
    return ceiling if _LADDER.index(ceiling) > _LADDER.index(grade) else grade


@dataclass
class AxisFinding:
    """One axis's finding: what the metadata shows, the confidence-relevant direction
    (`supportive` / `limiting` / `neutral`), and the exact reason. `limiting` never
    silently vanishes from the final report."""

    axis: str
    direction: str  # supportive | limiting | neutral | unknown
    finding: str
    reason: str = ""


@dataclass
class EvidenceQualityAssessment:
    """The rubric output: per-axis findings, the aggregate grade, and the epistemic cap.
    `caveats` accumulates every limiting finding AND every unknown input — an assessment
    that hides what it didn't know would repeat the failure mode this module replaces."""

    grade: QualityGrade
    epistemic_cap: EpistemicStatus
    axes: list[AxisFinding] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    def to_instruction(self) -> str:
        return format_quality_instructions(self)


def _axis_study_design(meta: dict, caveats: list[str]) -> tuple[AxisFinding, StudyDesign]:
    raw = meta.get("study_design")
    try:
        design = StudyDesign(raw) if raw else StudyDesign.unknown
    except ValueError:
        design = StudyDesign.unknown
    if design is StudyDesign.unknown:
        caveats.append("study design not recorded — the strongest causal-inference fact is unknown")
    elif design in (StudyDesign.in_vitro, StudyDesign.mechanism_only):
        caveats.append(
            f"evidence is {design.value} only — a plausible mechanism is a HYPOTHESIS, never "
            "established harm or established benefit in itself"
        )
    return (
        AxisFinding(
            axis="study_design",
            direction="supportive" if design in (StudyDesign.meta_analysis, StudyDesign.randomized_trial) else ("limiting" if design in (StudyDesign.in_vitro, StudyDesign.mechanism_only, StudyDesign.unknown) else "neutral"),
            finding=design.value,
            reason="design determines the ceiling on causal inference",
        ),
        design,
    )


def _axis_exposure_duration(meta: dict, caveats: list[str]) -> AxisFinding:
    duration = meta.get("exposure_duration")
    if duration is None or str(duration).strip() == "":
        caveats.append("exposure duration not recorded — chronic-vs-acute effects cannot be distinguished")
        return AxisFinding("exposure_duration", "unknown", "not recorded", "duration changes what an outcome can mean")
    text = str(duration).lower()
    chronic = any(w in text for w in ("week", "month", "year", "chronic", "long-term", "long term"))
    if chronic:
        return AxisFinding("exposure_duration", "supportive", str(duration), "documented sustained exposure")
    return AxisFinding("exposure_duration", "limiting", str(duration), "acute or single-dose exposure — chronic claims are not supported by this alone")


def _axis_dose(meta: dict, caveats: list[str]) -> AxisFinding:
    dose = meta.get("dose")
    if dose is None or str(dose).strip() == "":
        caveats.append("dose not recorded — effects cannot be related to any exposure level")
        return AxisFinding("dose", "unknown", "not recorded", "dose-response is the core of toxicological inference")
    return AxisFinding("dose", "supportive" if meta.get("dose_response_documented") else "neutral", str(dose), "documented" if meta.get("dose_response_documented") else "recorded but no dose-response relationship documented")


def _axis_interactions(meta: dict, caveats: list[str]) -> AxisFinding:
    interactions = meta.get("interactions")
    if interactions in (None, "", []):
        caveats.append("co-exposures/interactions not recorded — isolated-agent evidence may not transfer to real exposure patterns")
        return AxisFinding("interactions", "unknown", "not recorded", "combination effects can dominate single-agent effects")
    return AxisFinding("interactions", "neutral", str(interactions), "recorded")


def _axis_conflicts_of_interest(meta: dict, caveats: list[str]) -> AxisFinding:
    coi = meta.get("conflicts_of_interest")
    if coi is None or str(coi).strip() == "":
        caveats.append("conflicts of interest not recorded — absence of disclosure is not absence of conflict")
        return AxisFinding("conflicts_of_interest", "unknown", "not recorded", "disclosure status unknown")
    text = str(coi).lower()
    if any(w in text for w in ("none", "no conflict", "declared none", "independent")):
        return AxisFinding("conflicts_of_interest", "neutral", str(coi), "no conflict disclosed")
    # A declared conflict (e.g. industry funding) limits confidence by one axis step and
    # is ALWAYS surfaced — but it is not evidence the finding is wrong. The grade floor
    # for otherwise-sound evidence stays `low`, never `rejected`/`unassessable`.
    caveats.append(
        f"declared conflict of interest ({coi}) — reduces confidence; it does NOT by itself "
        "invalidate the evidence"
    )
    return AxisFinding("conflicts_of_interest", "limiting", str(coi), "declared conflict reduces confidence without invalidating the evidence")


def _axis_competing_explanations(meta: dict, caveats: list[str]) -> AxisFinding:
    competing = meta.get("competing_explanations")
    if competing in (None, "", []):
        caveats.append("competing explanations not addressed — an unexamined confounder is indistinguishable from a real effect")
        return AxisFinding("competing_explanations", "limiting", "not addressed", "unexamined alternatives cap causal claims")
    return AxisFinding("competing_explanations", "supportive", str(competing), "alternatives explicitly considered")


def _axis_causal_uncertainty(meta: dict, caveats: list[str]) -> tuple[AxisFinding, bool]:
    """Returns the finding and whether the evidence claims causation with design support.
    Correlation-only language caps the epistemic ladder at hypothesis — structurally."""
    causal_claimed = bool(meta.get("causal_claim"))
    causal_supported = bool(meta.get("causal_claim_supported_by_design"))
    if causal_claimed and causal_supported:
        return AxisFinding("causal_uncertainty", "supportive", "causal claim consistent with the design", "design can carry the claim"), True
    if causal_claimed and not causal_supported:
        caveats.append("causal claim NOT supported by the design — treated as correlation only")
        return AxisFinding("causal_uncertainty", "limiting", "causal claim exceeds the design", "correlation-only evidence caps at hypothesis"), False
    caveats.append("no causal claim recorded — association at best")
    return AxisFinding("causal_uncertainty", "neutral", "association only", "no causal claim to evaluate"), False


def grade_evidence(meta: dict) -> EvidenceQualityAssessment:
    """Grade one study's structured metadata. Pure function: no DB, no model leg — the
    same metadata always yields the same assessment."""
    meta = meta or {}
    caveats: list[str] = []

    if not any(meta.get(k) not in (None, "", []) for k in ("study_design", "exposure_duration", "dose", "population", "outcomes")):
        return EvidenceQualityAssessment(
            grade=QualityGrade.unassessable,
            epistemic_cap=EpistemicStatus.unknown,
            axes=[AxisFinding("metadata", "unknown", "no study metadata at all", "nothing to grade honestly")],
            caveats=["no metadata provided — refusing to invent a grade"],
        )

    design_finding, design = _axis_study_design(meta, caveats)
    axes = [design_finding]
    axes.append(_axis_exposure_duration(meta, caveats))
    axes.append(_axis_dose(meta, caveats))
    axes.append(_axis_interactions(meta, caveats))
    axes.append(_axis_conflicts_of_interest(meta, caveats))
    axes.append(_axis_competing_explanations(meta, caveats))
    causal_finding, causal_ok = _axis_causal_uncertainty(meta, caveats)
    axes.append(causal_finding)

    # Aggregate: start from the design ceiling, then take every LIMITING axis's floor.
    grade = QualityGrade.high
    grade = _raise_cap(grade, _DESIGN_CEILING.get(design, QualityGrade.very_low))
    limiting_floors = {
        "exposure_duration": QualityGrade.moderate,
        "conflicts_of_interest": QualityGrade.low,
        "competing_explanations": QualityGrade.moderate,
        "causal_uncertainty": QualityGrade.moderate,
    }
    for finding in axes:
        if finding.direction == "limiting":
            floor = limiting_floors.get(finding.axis, QualityGrade.low)
            if finding.axis == "conflicts_of_interest":
                # Mission rule: a funding conflict reduces confidence but is never
                # automatic proof of invalidity — floor at `low` for otherwise-sound
                # evidence (a weak design floors the grade lower on its own axis).
                if _LADDER.index(grade) < _LADDER.index(QualityGrade.low):
                    grade = QualityGrade.low
            else:
                grade = _floor(grade, floor)

    cap = _DESIGN_EPISTEMIC_CAP.get(design, EpistemicStatus.hypothesis)
    if not causal_ok:
        cap = EpistemicStatus.hypothesis
    if design is StudyDesign.unknown:
        cap = EpistemicStatus.hypothesis

    return EvidenceQualityAssessment(grade=grade, epistemic_cap=cap, axes=axes, caveats=caveats)


def format_quality_instructions(assessment: EvidenceQualityAssessment) -> str:
    """One-line instruction text for prompt-building layers (the build_trust_instructions
    pattern): grade, cap, and the caveats that MUST travel with any use of this evidence.
    Deliberately compact; the full per-axis findings stay available in the dataclass."""
    caveats = "; ".join(assessment.caveats) if assessment.caveats else "no caveats recorded"
    return (
        f"Evidence quality: {assessment.grade.value} (supports at most "
        f"{assessment.epistemic_cap.value} on the epistemic ladder). Caveats: {caveats}"
    )


def epistemic_cap(meta: dict) -> EpistemicStatus:
    """Convenience: the highest EpistemicStatus the metadata can support — the single
    integration point with app/rag/epistemics.classify_claim(quality_cap=...)."""
    return grade_evidence(meta).epistemic_cap
