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


def _axis_sample_size(meta: dict, caveats: list[str]) -> AxisFinding:
    """Round 8 M2 axis. Limiting ONLY when a declared size is genuinely small — absence is
    surfaced (power cannot be assessed) but never penalized, so every metadata dict graded
    before this axis existed keeps its grade and only gains the explicit caveat."""
    n = meta.get("sample_size")
    if n is None or str(n).strip() == "":
        caveats.append("sample size not recorded — statistical power cannot be assessed")
        return AxisFinding("sample_size", "unknown", "not recorded", "an underpowered study can hide real effects")
    try:
        count = int(n)
    except (TypeError, ValueError):
        caveats.append("sample size not recorded as a number — power cannot be assessed")
        return AxisFinding("sample_size", "unknown", str(n), "non-numeric sample size")
    if count < 30:
        caveats.append(f"sample size {count} is small — underpowered for small or moderate effects")
        return AxisFinding("sample_size", "limiting", str(count), "small samples overstate precision and miss small effects")
    supportive = count >= 100
    return AxisFinding(
        "sample_size",
        "supportive" if supportive else "neutral",
        str(count),
        "adequate for moderate effects" if supportive else "moderate — adequate for large effects only",
    )


_STATISTICAL_REPORTING_VALUES = ("confidence_intervals", "p_values", "bayesian", "none_reported")


def _axis_statistical_reporting(meta: dict, caveats: list[str]) -> AxisFinding:
    """Round 8 M2 axis: does the study quantify its own uncertainty? `none_reported` is
    LIMITING — a point estimate without spread cannot bound the evidence, and the absence
    of uncertainty language is how overconfidence smuggles itself into a conclusion."""
    raw = meta.get("statistical_reporting")
    if raw is None or str(raw).strip() == "":
        caveats.append("statistical uncertainty reporting not recorded — the precision of the effect is unknown")
        return AxisFinding("statistical_reporting", "unknown", "not recorded", "uncertainty language is part of the evidence")
    value = str(raw).strip()
    if value not in _STATISTICAL_REPORTING_VALUES:
        valid = ", ".join(_STATISTICAL_REPORTING_VALUES)
        caveats.append(f"statistical reporting value '{value}' is not part of the closed vocabulary ({valid})")
        return AxisFinding("statistical_reporting", "unknown", value, "unrecognized reporting vocabulary")
    if value == "none_reported":
        caveats.append("no uncertainty quantification reported — a point estimate without spread cannot bound the evidence")
        return AxisFinding("statistical_reporting", "limiting", value, "unquantified uncertainty is unbounded uncertainty")
    return AxisFinding("statistical_reporting", "supportive", value, "uncertainty quantified in the study itself")


_REPLICATION_VALUES = ("independent", "same_team", "none")


def _axis_replication(meta: dict, caveats: list[str]) -> AxisFinding:
    """Round 8 M2 axis. `none` is deliberately NEUTRAL-with-caveat, not limiting: most
    single studies are unreplicated at publication, and penalizing the standard case would
    grade every honest study as weak. The caveat says why replication still matters; the
    landscape summarizer is where replication coverage across studies is weighed."""
    raw = meta.get("replication")
    if raw is None or str(raw).strip() == "":
        caveats.append("replication status not recorded — whether the finding has been reproduced is unknown")
        return AxisFinding("replication", "unknown", "not recorded", "replication is how findings stop being one team's result")
    value = str(raw).strip()
    if value not in _REPLICATION_VALUES:
        valid = ", ".join(_REPLICATION_VALUES)
        caveats.append(f"replication value '{value}' is not part of the closed vocabulary ({valid})")
        return AxisFinding("replication", "unknown", value, "unrecognized replication vocabulary")
    if value == "independent":
        return AxisFinding("replication", "supportive", value, "independently reproduced — the strongest ordinary evidence of reliability")
    if value == "same_team":
        caveats.append("replication is by the same team — not independent replication")
        return AxisFinding("replication", "neutral", value, "same-team reproduction shares the original's methods and blind spots")
    caveats.append("no independent replication recorded — a single finding is not established until reproduced")
    return AxisFinding("replication", "neutral", value, "single study")


def _axis_outcome_followup(meta: dict, caveats: list[str]) -> AxisFinding:
    """Round 8 M2 axis: short-term versus long-term. Short declared follow-up is LIMITING
    for any claim about lasting effects — acute outcomes cannot vouch for chronic ones.
    Absence is surfaced, never penalized."""
    raw = meta.get("outcome_followup")
    if raw is None or str(raw).strip() == "":
        caveats.append("outcome follow-up duration not recorded — short versus long-term effects cannot be distinguished")
        return AxisFinding("outcome_followup", "unknown", "not recorded", "duration changes what an outcome can mean")
    text_value = str(raw).lower()
    long_term = any(w in text_value for w in ("month", "year", "chronic", "long-term", "long term", "longitudinal"))
    if long_term:
        return AxisFinding("outcome_followup", "supportive", str(raw), "documented long-term follow-up")
    caveats.append(
        f"short follow-up ({raw}) — long-term effects are not addressed by this evidence alone"
    )
    return AxisFinding("outcome_followup", "limiting", str(raw), "acute outcomes cannot vouch for chronic ones")


def grade_evidence(meta: dict) -> EvidenceQualityAssessment:
    """Grade one study's structured metadata. Pure function: no DB, no model leg — the
    same metadata always yields the same assessment.

    Round 8 M2 adds four axes (sample_size, statistical_reporting, replication,
    outcome_followup) plus a descriptive study_setting field. The new axes are LIMITING
    only when a value IS declared and that value is genuinely limiting; ABSENCE of a new
    axis is surfaced as an explicit caveat but never penalized — every metadata dict graded
    under the earlier vocabulary keeps its grade and only gains the caveats, so the
    closed-vocabulary contract with stamped claims never shifts underneath them."""
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
    # Round 8 M2 axes: methodology depth (sample size, uncertainty reporting), replication,
    # and short-vs-long-term follow-up. study_setting is deliberately NOT a grading axis —
    # where a study was done is descriptive context for the landscape summarizer, and
    # grading by geography would be exactly the "mainstream vs alternative" bias the
    # mission forbids.
    axes.append(_axis_sample_size(meta, caveats))
    axes.append(_axis_statistical_reporting(meta, caveats))
    axes.append(_axis_replication(meta, caveats))
    axes.append(_axis_outcome_followup(meta, caveats))

    # Aggregate: start from the design ceiling, then take every LIMITING axis's floor.
    grade = QualityGrade.high
    grade = _raise_cap(grade, _DESIGN_CEILING.get(design, QualityGrade.very_low))
    limiting_floors = {
        "exposure_duration": QualityGrade.moderate,
        "conflicts_of_interest": QualityGrade.low,
        "competing_explanations": QualityGrade.moderate,
        "causal_uncertainty": QualityGrade.moderate,
        # Round 8 M2: a declared-and-limiting methodology axis floors the grade at
        # moderate — it degrades confidence in the EFFECT SIZE, it does not change what
        # the design can structurally prove (the ladder cap stays design-driven).
        "sample_size": QualityGrade.moderate,
        "statistical_reporting": QualityGrade.moderate,
        "outcome_followup": QualityGrade.moderate,
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


# --------------------------------------------------------------------------------------
# Round 8 M2: the BODY-OF-EVIDENCE layer. grade_evidence() grades ONE study; a real
# research question is decided by a SET of studies that may agree, disagree, replicate,
# contradict, or share funders. summarize_evidence_landscape() aggregates a set of
# already-computed assessments into a structured, provenance-aware report — and is
# deliberately blind to source type: nothing here ranks, weights, or excludes by
# mainstream/alternative/independent/industry. Funding conflicts are COUNTS with their
# per-study provenance, never verdicts; institutional diversity is descriptive context.
# --------------------------------------------------------------------------------------

from collections import Counter  # noqa: E402  (module-level by design: pure stdlib)
from dataclasses import dataclass  # noqa: E402

_DESIGN_CLASS = {
    StudyDesign.meta_analysis: "experimental",
    StudyDesign.randomized_trial: "experimental",
    StudyDesign.prospective_cohort: "observational",
    StudyDesign.retrospective_cohort: "observational",
    StudyDesign.case_control: "observational",
    StudyDesign.case_series: "observational",
    StudyDesign.in_vitro: "mechanistic",
    StudyDesign.mechanism_only: "mechanistic",
}


@dataclass
class EvidenceLandscape:
    """The structured body-of-evidence report for one question. Every field is a COUNT
    with its derivation documented — the report says what the corpus looks like, never
    which side wins (contradiction handling stays with the ladder's conflicting_evidence
    verdict and the graph layer, which carry claim-level provenance)."""

    study_count: int
    by_design_class: dict[str, int]          # experimental | observational | mechanistic | unknown
    funding_declared_none: int
    funding_industry_or_conflict: int
    funding_unknown: int
    replication_independent: int
    replication_none_or_same_team: int
    replication_unknown: int
    followup_long_term: int
    followup_short_term: int
    followup_unknown: int
    settings_reported: list[str]             # distinct, order-stable
    settings_unknown: int
    grade_counts: dict[str, int]
    caveats_top: list[tuple[str, int]]       # the caveats the corpus keeps producing
    limited_by: list[str]                    # axes that LIMIT the strongest studies here


def summarize_evidence_landscape(items: list[tuple[dict, "EvidenceQualityAssessment"]]) -> EvidenceLandscape:
    """Aggregate (metadata, assessment) pairs for studies addressing one question.

    Deterministic; pure. `items` carries each study's own declared metadata next to its
    assessment so every count is traceable to what the founder/producer actually declared
    — no re-derivation, no inference of undisclosed fields."""
    design_classes: Counter = Counter()
    funding_none = funding_conflict = funding_unknown = 0
    repl_independent = repl_weak = repl_unknown = 0
    followup_long = followup_short = followup_unknown = 0
    settings: list[str] = []
    settings_unknown = 0
    grades: Counter = Counter()
    caveat_counter: Counter = Counter()
    limited_axes: Counter = Counter()

    for meta, assessment in items:
        meta = meta or {}
        design_raw = meta.get("study_design")
        try:
            design = StudyDesign(design_raw) if design_raw else StudyDesign.unknown
        except ValueError:
            design = StudyDesign.unknown
        design_classes[_DESIGN_CLASS.get(design, "unknown")] += 1

        coi = str(meta.get("conflicts_of_interest") or "").strip().lower()
        if not coi:
            funding_unknown += 1
        elif any(w in coi for w in ("none", "no conflict", "declared none", "independent")):
            funding_none += 1
        else:
            funding_conflict += 1

        repl = str(meta.get("replication") or "").strip()
        if not repl:
            repl_unknown += 1
        elif repl == "independent":
            repl_independent += 1
        else:
            repl_weak += 1

        fu = str(meta.get("outcome_followup") or "").lower()
        if not fu:
            followup_unknown += 1
        elif any(w in fu for w in ("month", "year", "chronic", "long-term", "long term", "longitudinal")):
            followup_long += 1
        else:
            followup_short += 1

        setting = str(meta.get("study_setting") or "").strip()
        if setting:
            settings.append(setting)
        else:
            settings_unknown += 1

        grades[assessment.grade.value] += 1
        for caveat in assessment.caveats:
            caveat_counter[caveat] += 1
        for axis in assessment.axes:
            if axis.direction == "limiting":
                limited_axes[axis.axis] += 1

    return EvidenceLandscape(
        study_count=len(items),
        by_design_class={k: design_classes.get(k, 0) for k in ("experimental", "observational", "mechanistic", "unknown")},
        funding_declared_none=funding_none,
        funding_industry_or_conflict=funding_conflict,
        funding_unknown=funding_unknown,
        replication_independent=repl_independent,
        replication_none_or_same_team=repl_weak,
        replication_unknown=repl_unknown,
        followup_long_term=followup_long,
        followup_short_term=followup_short,
        followup_unknown=followup_unknown,
        settings_reported=sorted(set(settings)),
        settings_unknown=settings_unknown,
        grade_counts={k: grades.get(k, 0) for k in ("high", "moderate", "low", "very_low", "unassessable")},
        caveats_top=caveat_counter.most_common(5),
        limited_by=sorted(limited_axes.keys()),
    )


def format_landscape_instructions(landscape: EvidenceLandscape) -> str:
    """The one-line instruction text for research/chat prompt layers (Swedish, matching
    format_epistemic_instructions). Structural neutrality is the point: it reports SHAPE
    (designs, funding disclosure, replication, follow-up, settings) and NEVER a verdict —
    the model is told to treat the corpus's limitations as limitations of the evidence,
    not to prefer any source type."""
    if landscape.study_count == 0:
        return (
            "EVIDENSlandskap: inga studier med deklarerad metadata att sammanfatta — "
            "behandla frågan som OLÖST tills strukturerad evidens finns."
        )
    diversity = (
        f"miljöer/inställningar rapporterade: {len(landscape.settings_reported)}"
        if landscape.settings_reported
        else "miljöer/inställningar ej rapporterade"
    )
    return (
        f"EVIDENSlandskap ({landscape.study_count} studier): "
        f"{landscape.by_design_class.get('experimental', 0)} experimentella, "
        f"{landscape.by_design_class.get('observational', 0)} observationella, "
        f"{landscape.by_design_class.get('mechanistic', 0)} mekanistiska; "
        f"intressekonflikt deklarerad: ingen {landscape.funding_declared_none}, "
        f"konflikt {landscape.funding_industry_or_conflict}, okänd {landscape.funding_unknown}; "
        f"replikering oberoende {landscape.replication_independent}, "
        f"ej oberoende {landscape.replication_none_or_same_team}, okänd {landscape.replication_unknown}; "
        f"långtidsuppföljning {landscape.followup_long_term}, kort {landscape.followup_short_term}, "
        f"okänd {landscape.followup_unknown}; {diversity}. "
        "Källtyp ger INTE automatiskt förtroende eller misstro: väg strukturen, inte ursprunget."
    )
