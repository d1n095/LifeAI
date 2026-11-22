"""Round 10 M6: SCIENTIFIC REASONING over a topic + the BOUNDED INVESTIGATION POLICY.

Built ON TOP of Round 9's topic landscapes (summarize_topic_landscape) and the stamped
closed-vocabulary metadata — no new query patterns, no inferred facts. Every number here
comes from rows the founder actually declared or edges they actually drew.

analyze_topic_science() extends the topic view with what the owner-wide landscape cannot
answer: long-term vs short-term follow-up, cumulative exposure, dose-response, declared
interactions, population spread, mechanistic-vs-empirical composition, the confounding
gap (causal claims without declared alternatives), replication pressure (declared
contradictions vs same-topic studies), international disagreement (R9's derived flags),
and the MISSING-EVIDENCE report — which closed-vocabulary axes are entirely unknown for
this topic. Absence of evidence is REPORTED, never inferred away.

investigation_policy() is the bounded decision rule for research depth, proportional to
risk, expected information gain, and cost:
  - answer          — sufficient evidence: >= 2 independent studies, no unresolved
                      declared disagreement, no funding-conflict floor, load-bearing
                      axes declared;
  - investigate     — the disagreement/quality signals above make further reading
                      materially decision-relevant (bounded: max_studies_to_examine);
  - stop            — the question is settled for THIS purpose: sufficient evidence AND
                      the remaining disagreements are already reflected in a
                      conflicting_evidence verdict (re-litigating them adds nothing);
  - report_uncertainty — too little evidence (< 2 studies or no metadata at all):
                      the honest answer is uncertainty, never fabricated certainty.
The policy is deterministic, owner-scoped, and READ-ONLY. It recommends depth; it never
executes research, spawns agents, or spends anything.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.rag.evidence_metadata import summarize_topic_landscape

# The load-bearing axes for a causal question: if NONE of these are declared for a
# topic's studies, the evidence cannot carry a causal conversation.
_LOAD_BEARING_AXES = ("study_design", "sample_size", "competing_explanations", "causal_claim")

_OBSERVATIONAL_DESIGNS = {"prospective_cohort", "retrospective_cohort", "case_control", "case_series"}
_LONG_FOLLOWUP = {"long_term", "multi_year", "years", "longitudinal", "multi_year_followup"}
_SHORT_FOLLOWUP = {"short_term", "weeks", "days", "months", "single_session", "acute"}
_CUMULATIVE_EXPOSURE = {"chronic", "long_term", "cumulative", "years", "multi_year", "repeated"}


@dataclass
class TopicScience:
    topic: str
    study_count: int
    long_term_followup: int
    short_term_followup: int
    followup_unspecified: int
    cumulative_exposure_studies: int
    dose_response_documented: int
    dose_range_reported: list
    interaction_studies: int
    distinct_populations: int
    distinct_settings: int
    mechanistic_studies: int
    observational_studies: int
    experimental_studies: int
    meta_analyses_studies: int  # secondary synthesis, kept DISTINCT from primary designs
    causal_claims_total: int
    causal_without_alternatives: int  # the confounding gap
    declared_disagreements: int
    cross_country_disagreements: int
    funding_flagged_studies: int
    unknown_axes: list = field(default_factory=list)  # missing-evidence report

    def to_dict(self) -> dict:
        return {
            "topic": self.topic,
            "study_count": self.study_count,
            "followup": {"long_term": self.long_term_followup, "short_term": self.short_term_followup, "unspecified": self.followup_unspecified},
            "cumulative_exposure_studies": self.cumulative_exposure_studies,
            "dose_response_documented": self.dose_response_documented,
            "dose_range_reported": self.dose_range_reported,
            "interaction_studies": self.interaction_studies,
            "populations": self.distinct_populations,
            "settings": self.distinct_settings,
            "designs": {"mechanistic": self.mechanistic_studies, "observational": self.observational_studies, "experimental": self.experimental_studies, "meta_analyses": self.meta_analyses_studies},
            "causal_claims": self.causal_claims_total,
            "causal_without_declared_alternatives": self.causal_without_alternatives,
            "declared_disagreements": self.declared_disagreements,
            "cross_country_disagreements": self.cross_country_disagreements,
            "funding_flagged_studies": self.funding_flagged_studies,
            "missing_evidence_axes": self.unknown_axes,
        }


def analyze_topic_science(
    db: Session, owner_id: uuid.UUID, topic: str, *, limit: int = 50,
    landscape=None, rows=None,
) -> TopicScience:
    """The per-topic scientific composition, deterministic and bounded. Reuses the R9
    landscape's exact row selection (owner-scoped, topic-filtered, LIMIT-capped).
    `landscape` and `rows` let a caller that ALREADY fetched them (e.g. the graph
    instruction block) pass them in — the R10 perf bounds caught the duplicate-query
    cost of re-fetching, so sharing is the required shape on the prompt path."""
    from sqlalchemy import text as sa_text

    from app.rag.evidence_metadata import ClaimEvidenceMetadata

    if landscape is None:
        landscape = summarize_topic_landscape(db, owner_id, topic, limit=limit)
    if rows is None:
        rows = (
            db.query(ClaimEvidenceMetadata)
            .filter(
                ClaimEvidenceMetadata.owner_id == owner_id,
                ClaimEvidenceMetadata.metadata_json["research_topic"].astext == topic,
            )
            .order_by(ClaimEvidenceMetadata.updated_at.desc())
            .limit(max(1, int(limit)))
            .all()
        )

    long_f = short_f = unspecified_f = cumulative = dose_resp = interactions_n = 0
    causal_total = causal_gap = funding_flagged = 0
    mech = obs = exp = meta_n = 0
    populations: set[str] = set()
    settings: set[str] = set()
    doses: list[str] = []
    declared_axes: set[str] = set()

    for row in rows:
        m = row.metadata_json or {}
        declared_axes.update(m.keys())
        followup = (m.get("outcome_followup") or "").strip().lower()
        if followup in _LONG_FOLLOWUP:
            long_f += 1
        elif followup in _SHORT_FOLLOWUP:
            short_f += 1
        elif followup:
            unspecified_f += 1  # declared but outside the known vocabulary buckets
        if (m.get("exposure_duration") or "").strip().lower() in _CUMULATIVE_EXPOSURE:
            cumulative += 1
        if m.get("dose_response_documented") is True:
            dose_resp += 1
        dose = (m.get("dose") or "").strip()
        if dose:
            doses.append(dose)
        if m.get("interactions"):
            interactions_n += 1
        population = (m.get("population") or "").strip()
        if population:
            populations.add(population.lower())
        setting = (m.get("study_setting") or "").strip()
        if setting:
            settings.add(setting.lower())
        design = (m.get("study_design") or "").strip()
        if design == "mechanism_only" or design == "in_vitro":
            mech += 1
        elif design in _OBSERVATIONAL_DESIGNS:
            obs += 1
        elif design == "randomized_trial":
            exp += 1
        elif design == "meta_analysis":
            meta_n += 1
        if m.get("causal_claim") is True:
            causal_total += 1
            if not (m.get("causal_claim_supported_by_design") is True or (m.get("competing_explanations") or [])):
                causal_gap += 1
        coi = (m.get("conflicts_of_interest") or "").strip().lower()
        if coi and coi not in ("none declared", "none_declared", "no", "none"):
            funding_flagged += 1

    unknown_axes = [axis for axis in _LOAD_BEARING_AXES if axis not in declared_axes]
    return TopicScience(
        topic=topic,
        study_count=landscape.base.study_count,
        long_term_followup=long_f,
        short_term_followup=short_f,
        followup_unspecified=unspecified_f,
        cumulative_exposure_studies=cumulative,
        dose_response_documented=dose_resp,
        dose_range_reported=sorted(set(doses)),
        interaction_studies=interactions_n,
        distinct_populations=len(populations),
        distinct_settings=len(settings),
        mechanistic_studies=mech,
        observational_studies=obs,
        experimental_studies=exp,
        meta_analyses_studies=meta_n,
        causal_claims_total=causal_total,
        causal_without_alternatives=causal_gap,
        declared_disagreements=len(landscape.disagreements),
        cross_country_disagreements=landscape.cross_country_disagreements,
        funding_flagged_studies=funding_flagged,
        unknown_axes=unknown_axes,
    )


@dataclass
class InvestigationPolicy:
    policy: str  # answer | investigate | stop | report_uncertainty
    reasons: list
    max_studies_to_examine: int  # the cost bound — depth is capped, not open-ended

    def to_dict(self) -> dict:
        return {
            "policy": self.policy,
            "reasons": self.reasons,
            "max_studies_to_examine": self.max_studies_to_examine,
        }


def investigation_policy(
    science: TopicScience, *, conflict_resolved: bool = False, normative_question: bool = False
) -> InvestigationPolicy:
    """The bounded research-depth decision. Deterministic thresholds; every reason cites
    a number from the TopicScience (evidence-refs in spirit). Keyword-only args."""
    reasons: list[str] = []
    if normative_question:
        return InvestigationPolicy(
            policy="stop",
            reasons=["Frågan är normativ, inte empirisk — mer evidens ändrar inte svaret."],
            max_studies_to_examine=0,
        )
    if science.study_count < 2 or not (science.experimental_studies + science.observational_studies + science.mechanistic_studies + science.meta_analyses_studies):
        reasons.append(f"{science.study_count} studier med deklarerad metadata — otillräcklig grund.")
        if science.unknown_axes:
            reasons.append("Saknade axlar: " + ", ".join(science.unknown_axes) + ".")
        return InvestigationPolicy(policy="report_uncertainty", reasons=reasons, max_studies_to_examine=10)

    unresolved_disagreement = science.declared_disagreements > 0 and not conflict_resolved
    material_quality_signal = science.funding_flagged_studies > 0
    confounding_gap = science.causal_without_alternatives > 0
    # >= 2 (not >= 3): a two-study single-population base already carries a real
    # generalization risk worth naming — the honest threshold is "more than one study".
    single_population = science.distinct_populations <= 1 and science.study_count >= 2

    if unresolved_disagreement or material_quality_signal or confounding_gap:
        if unresolved_disagreement:
            reasons.append(
                f"{science.declared_disagreements} deklarerade motsägelser (länder emellan {science.cross_country_disagreements}) — läs båda sidor innan slutsats."
            )
        if material_quality_signal:
            reasons.append(f"{science.funding_flagged_studies} studier med deklarerad intressekonflikt — granska finansieringens roll, avvisa inte automatiskt.")
        if confounding_gap:
            reasons.append(f"{science.causal_without_alternatives} kausal påståenden utan deklarerade konkurrerande förklaringar — kontrollera konfundering.")
        cap = min(10, max(4, science.study_count))
        return InvestigationPolicy(policy="investigate", reasons=reasons, max_studies_to_examine=cap)

    if science.study_count >= 2:
        reasons.append(
            f"{science.study_count} studier, inga olösta deklarerade motsägelser, {science.experimental_studies} experimentella, {science.meta_analyses_studies} metaanalyser — svara effektivt."
        )
        if conflict_resolved:
            reasons.append("Kvarvarande motsägelser är redan speglade i verdictet conflicting_evidence — vidare grävning ändrar inte beslutet.")
            return InvestigationPolicy(policy="stop", reasons=reasons, max_studies_to_examine=0)
        if single_population:
            reasons.append(f"Endast {science.distinct_populations} population(er) i {science.study_count} studier — generalisera försiktigt.")
        return InvestigationPolicy(policy="answer", reasons=reasons, max_studies_to_examine=2)

    reasons.append("Grund förutsatt otillräcklig.")
    return InvestigationPolicy(policy="report_uncertainty", reasons=reasons, max_studies_to_examine=10)


def format_topic_science_instructions(science: TopicScience, policy: InvestigationPolicy) -> str:
    """The Swedish prompt-layer line: the topic's scientific composition + the bounded
    investigation policy. Neutrality preserved: composition is reported, never ranked."""
    parts = [
        f"VETENSKAPLIG SAMMANSÄTTNING '{science.topic}': {science.study_count} studier "
        f"(experimentella {science.experimental_studies}, observationella {science.observational_studies}, "
        f"mekanistiska {science.mechanistic_studies}); uppföljning lång {science.long_term_followup}/kort "
        f"{science.short_term_followup}/ospecificerad {science.followup_unspecified}; kumulativ exponering "
        f"{science.cumulative_exposure_studies}; dossvar dokumenterat {science.dose_response_documented}; "
        f"interaktioner deklarerade i {science.interaction_studies}; populationer {science.distinct_populations}."
    ]
    if science.causal_claims_total:
        parts.append(
            f" KAUSALT: {science.causal_claims_total} påståenden, varav {science.causal_without_alternatives} utan deklarerade alternativ — kräv uttrycklig evidens för kausalitet."
        )
    if science.unknown_axes:
        parts.append(f" SAKNADE AXLAR: {', '.join(science.unknown_axes)} — rapportera osäkerhet, hitta på inget.")
    parts.append(f" UNDERSÖKNINGSPOLICY: {policy.policy.upper()} ({'; '.join(policy.reasons)} Gräns: {policy.max_studies_to_examine} studier.)")
    return "".join(parts)
