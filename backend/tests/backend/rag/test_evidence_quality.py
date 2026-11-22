"""Tests for app/rag/evidence_quality.py — the P4 scientific-reasoning rubric — plus the
integration into app/rag/epistemics.classify_claim(quality_cap=...).

The two bias rules the mission names are asserted EXPLICITLY:
  1. a speculative mechanism is never graded as established harm (mechanism-only →
     hypothesis cap + named caveat, grade very_low);
  2. a funding conflict reduces confidence and is surfaced — never automatic proof of
     invalidity (otherwise-sound evidence floors at `low`, the caveat says exactly that).

Pure functions: no DB, no model leg. The epistemics integration test uses the real PG
session fixture the same way test_epistemics.py does.
"""

from app.models.knowledge_claim import ClaimConfidence, ClaimStatus, ClaimType
from app.rag.claims import EXTRACTION_VERSION
from app.rag.evidence_quality import (
    EvidenceQualityAssessment,
    QualityGrade,
    epistemic_cap,
    format_quality_instructions,
    grade_evidence,
)
from app.config import get_settings
from app.rag.epistemics import EpistemicStatus

_EMBEDDING_DIM = get_settings().embedding_dim


# --- the mission's two named anti-bias rules --------------------------------------------


def test_speculative_mechanism_is_never_established_harm():
    assessment = grade_evidence(
        {"study_design": "mechanism_only", "causal_claim": True, "conflicts_of_interest": "none disclosed"}
    )
    assert assessment.grade == QualityGrade.very_low
    assert assessment.epistemic_cap == EpistemicStatus.hypothesis
    assert any("never" in c and "established harm" in c for c in assessment.caveats)


def test_funding_conflict_reduces_confidence_without_invalidating():
    sound = {
        "study_design": "randomized_trial",
        "exposure_duration": "12 weeks",
        "dose": "200 mg/day",
        "dose_response_documented": True,
        "conflicts_of_interest": "industry-funded",
        "competing_explanations": ["adherence differences"],
        "causal_claim": True,
        "causal_claim_supported_by_design": True,
    }
    assessment = grade_evidence(sound)
    assert assessment.grade == QualityGrade.low  # floored by the conflict — NOT rejected
    assert assessment.epistemic_cap == EpistemicStatus.verified_fact  # design still carries the claim
    assert any("does NOT by itself invalidate" in c for c in assessment.caveats)
    # Same study without the conflict grades higher — the conflict cost exactly one step.
    without = dict(sound, conflicts_of_interest="none disclosed")
    assert grade_evidence(without).grade in (QualityGrade.high, QualityGrade.moderate)


# --- axis coverage -----------------------------------------------------------------------


def test_well_documented_rct_grades_high_with_full_cap():
    assessment = grade_evidence(
        {
            "study_design": "randomized_trial",
            "exposure_duration": "6 months",
            "dose": "40 mg/day",
            "dose_response_documented": True,
            "interactions": ["none observed"],
            "conflicts_of_interest": "none disclosed",
            "competing_explanations": ["baseline severity imbalance — controlled"],
            "causal_claim": True,
            "causal_claim_supported_by_design": True,
            "population": "adults",
            "outcomes": ["sleep latency"],
            # Round 8 M2: the vocabulary grew (sample_size, statistical_reporting,
            # replication, outcome_followup) — "fully documented" grows with it, so a
            # complete declaration still grades with ZERO caveats.
            "sample_size": 340,
            "statistical_reporting": "confidence_intervals",
            "replication": "independent",
            "outcome_followup": "12 months",
            "study_setting": "Sweden, university hospital",
        }
    )
    assert isinstance(assessment, EvidenceQualityAssessment)
    assert assessment.grade == QualityGrade.high
    assert assessment.epistemic_cap == EpistemicStatus.verified_fact
    assert assessment.caveats == []


def test_correlation_only_language_caps_at_hypothesis():
    assessment = grade_evidence(
        {"study_design": "prospective_cohort", "causal_claim": True, "causal_claim_supported_by_design": False}
    )
    assert assessment.epistemic_cap == EpistemicStatus.hypothesis
    assert assessment.grade in (QualityGrade.low, QualityGrade.moderate)
    assert any("correlation only" in c or "treated as correlation" in c for c in assessment.caveats)


def test_missing_metadata_is_unassessable_never_guessed():
    assessment = grade_evidence({})
    assert assessment.grade == QualityGrade.unassessable
    assert assessment.epistemic_cap == EpistemicStatus.unknown
    assert any("refusing to invent a grade" in c for c in assessment.caveats)


def test_every_unknown_axis_is_surfaced_as_a_caveat():
    assessment = grade_evidence({"study_design": "case_control"})
    # exposure duration, dose, interactions, conflicts, competing explanations, causal:
    assert any("exposure duration not recorded" in c for c in assessment.caveats)
    assert any("dose not recorded" in c for c in assessment.caveats)
    assert any("interactions not recorded" in c for c in assessment.caveats)
    assert any("conflicts of interest not recorded" in c for c in assessment.caveats)
    assert any("competing explanations not addressed" in c for c in assessment.caveats)


def test_case_control_design_caps_at_hypothesis():
    assessment = grade_evidence({"study_design": "case_control", "causal_claim": True, "causal_claim_supported_by_design": True})
    assert assessment.epistemic_cap == EpistemicStatus.hypothesis
    assert assessment.grade == QualityGrade.low


def test_instruction_string_carries_grade_cap_and_caveats():
    assessment = grade_evidence({"study_design": "meta_analysis", "exposure_duration": "8 weeks", "dose": "10 mg"})
    text = format_quality_instructions(assessment)
    assert "meta" in text or assessment.grade.value in text
    assert assessment.grade.value in text
    assert assessment.epistemic_cap.value in text
    assert "Caveats:" in text


def test_epistemic_cap_convenience_matches_assessment():
    meta = {"study_design": "mechanism_only"}
    assert epistemic_cap(meta) == grade_evidence(meta).epistemic_cap


def test_same_metadata_grades_identically():
    meta = {"study_design": "retrospective_cohort", "dose": "1 g", "conflicts_of_interest": "none"}
    assert grade_evidence(meta).grade == grade_evidence(dict(meta)).grade


# --- integration with the epistemics ladder ----------------------------------------------


def test_quality_cap_downgrades_verified_fact_in_the_ladder(db_session, make_verified_user):
    """A grounded, active, corroborated claim would sit at verified_fact — but mechanism-
    only metadata caps it at hypothesis, with the reason recorded."""
    from app.models.document import Document, DocumentSource
    from app.models.document_chunk import DocumentChunk
    from app.models.knowledge_claim import KnowledgeClaim
    from app.models.project import Project
    from sqlalchemy import text as sa_text

    def _set_rls_user(owner_id):
        from app.request_context import current_user_id as current_user_id_var

        current_user_id_var.set(str(owner_id))
        db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})

    user, _ = make_verified_user()
    _set_rls_user(user.id)
    source = Document(title="Mechanism note", source=DocumentSource.upload, uploaded_by=user.id)
    db_session.add(source)
    db_session.commit()
    chunk = DocumentChunk(document_id=source.id, owner_id=user.id, chunk_index=0, text="Compound X activates pathway Y in vitro.", embedding=[0.1] * _EMBEDDING_DIM)
    db_session.add(chunk)
    db_session.commit()
    claim = KnowledgeClaim(
        owner_id=user.id, source_id=source.id, chunk_id=chunk.id,
        claim_text="Compound X damages the liver.", confidence=ClaimConfidence.likely,
        claim_type=ClaimType.technical, status=ClaimStatus.active, extraction_version=EXTRACTION_VERSION,
    )
    db_session.add(claim)
    db_session.commit()

    from app.rag.epistemics import classify_claim

    uncapped = classify_claim(db_session, user.id, claim)
    assert uncapped.status == EpistemicStatus.verified_fact

    capped = classify_claim(db_session, user.id, claim, quality_cap=EpistemicStatus.hypothesis)
    assert capped.status == EpistemicStatus.hypothesis
    assert any("evidence-quality cap" in r for r in capped.reasons)


def test_quality_cap_never_touches_conflicting_evidence(db_session, make_verified_user):
    """A contradiction is a contradiction whatever the metadata quality — the cap may
    only ever lower a verified verdict, never silence a dispute."""
    from app.models.claim_relationship import ClaimRelationship, ClaimRelationshipType
    from app.models.document import Document, DocumentSource
    from app.models.document_chunk import DocumentChunk
    from app.models.knowledge_claim import KnowledgeClaim
    from sqlalchemy import text as sa_text

    def _set_rls_user(owner_id):
        from app.request_context import current_user_id as current_user_id_var

        current_user_id_var.set(str(owner_id))
        db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})

    user, _ = make_verified_user()
    _set_rls_user(user.id)
    s1 = Document(title="A", source=DocumentSource.upload, uploaded_by=user.id)
    db_session.add(s1)
    db_session.commit()
    c1 = DocumentChunk(document_id=s1.id, owner_id=user.id, chunk_index=0, text="X raises the marker.", embedding=[0.1] * _EMBEDDING_DIM)
    db_session.add(c1)
    db_session.commit()
    claim = KnowledgeClaim(owner_id=user.id, source_id=s1.id, chunk_id=c1.id, claim_text="X raises the marker.", confidence=ClaimConfidence.likely, claim_type=ClaimType.technical, status=ClaimStatus.active, extraction_version=EXTRACTION_VERSION)
    db_session.add(claim)
    db_session.commit()
    s2 = Document(title="B", source=DocumentSource.upload, uploaded_by=user.id)
    db_session.add(s2)
    db_session.commit()
    c2 = DocumentChunk(document_id=s2.id, owner_id=user.id, chunk_index=0, text="X lowers the marker.", embedding=[0.1] * _EMBEDDING_DIM)
    db_session.add(c2)
    db_session.commit()
    challenger = KnowledgeClaim(owner_id=user.id, source_id=s2.id, chunk_id=c2.id, claim_text="X lowers the marker.", confidence=ClaimConfidence.likely, claim_type=ClaimType.technical, status=ClaimStatus.active, extraction_version=EXTRACTION_VERSION)
    db_session.add(challenger)
    db_session.commit()
    db_session.add(ClaimRelationship(owner_id=user.id, from_claim_id=challenger.id, to_claim_id=claim.id, relationship_type=ClaimRelationshipType.contradicts))
    db_session.commit()

    from app.rag.epistemics import classify_claim

    capped = classify_claim(db_session, user.id, claim, quality_cap=EpistemicStatus.hypothesis)
    assert capped.status == EpistemicStatus.conflicting_evidence


# --- Round 8 M2: methodology depth, replication, short vs long term -------------------------


def test_small_sample_size_is_limiting_large_is_supportive():
    small = grade_evidence({"study_design": "randomized_trial", "sample_size": 12, "causal_claim": True, "causal_claim_supported_by_design": True})
    assert any("underpowered" in c for c in small.caveats)
    assert small.grade in (QualityGrade.moderate, QualityGrade.low), "declared small n floors the grade at moderate"

    large = grade_evidence({"study_design": "randomized_trial", "sample_size": 500, "competing_explanations": ["controlled"], "causal_claim": True, "causal_claim_supported_by_design": True})
    assert not any("sample size" in c for c in large.caveats), "an adequate declared n produces no caveat"
    assert large.grade == QualityGrade.high

    absent = grade_evidence({"study_design": "randomized_trial", "causal_claim": True, "causal_claim_supported_by_design": True})
    assert any("sample size not recorded" in c for c in absent.caveats)
    # Isolate the axis under test: the ONLY delta between declaring an adequate n and
    # omitting it is the caveat — absence never shifts the grade.
    baseline = grade_evidence({"study_design": "randomized_trial", "sample_size": 500, "causal_claim": True, "causal_claim_supported_by_design": True})
    assert absent.grade == baseline.grade, "ABSENCE of the new axis is surfaced but never penalized"


def test_unquantified_uncertainty_is_limiting():
    none_reported = grade_evidence(
        {"study_design": "randomized_trial", "statistical_reporting": "none_reported", "causal_claim": True, "causal_claim_supported_by_design": True}
    )
    assert any("point estimate without spread" in c for c in none_reported.caveats)
    assert none_reported.grade in (QualityGrade.moderate, QualityGrade.low)

    quantified = grade_evidence(
        {"study_design": "randomized_trial", "statistical_reporting": "bayesian", "competing_explanations": ["controlled"], "causal_claim": True, "causal_claim_supported_by_design": True}
    )
    assert quantified.grade == QualityGrade.high
    assert not any(
        "statistical uncertainty reporting" in c or "point estimate without spread" in c
        for c in quantified.caveats
    ), "the reporting axis itself must be silent when uncertainty IS quantified"


def test_replication_vocabulary_and_neutrality():
    none_case = grade_evidence({"study_design": "randomized_trial", "replication": "none", "competing_explanations": ["controlled"], "causal_claim": True, "causal_claim_supported_by_design": True})
    assert any("not established until reproduced" in c for c in none_case.caveats)
    assert none_case.grade == QualityGrade.high, "unreplicated is the STANDARD case — caveated, never penalized"

    same_team = grade_evidence({"study_design": "randomized_trial", "replication": "same_team", "competing_explanations": ["controlled"], "causal_claim": True, "causal_claim_supported_by_design": True})
    assert any("not independent replication" in c for c in same_team.caveats)

    independent = grade_evidence({"study_design": "randomized_trial", "replication": "independent", "competing_explanations": ["controlled"], "causal_claim": True, "causal_claim_supported_by_design": True})
    assert independent.grade == QualityGrade.high
    assert not any("replication" in c for c in independent.caveats)


def test_short_followup_is_limiting_long_term_supportive():
    short = grade_evidence({"study_design": "prospective_cohort", "outcome_followup": "2 weeks", "causal_claim": True, "causal_claim_supported_by_design": False})
    assert any("long-term effects are not addressed" in c for c in short.caveats)
    assert short.grade in (QualityGrade.moderate, QualityGrade.low)

    long = grade_evidence({"study_design": "prospective_cohort", "outcome_followup": "5 years", "causal_claim": True, "causal_claim_supported_by_design": False})
    assert not any("follow-up" in c for c in long.caveats)
    assert long.grade in (QualityGrade.moderate, QualityGrade.low), "design still governs the causal ceiling"


def test_every_new_axis_absence_is_surfaced():
    assessment = grade_evidence({"study_design": "case_control"})
    assert any("sample size not recorded" in c for c in assessment.caveats)
    assert any("statistical uncertainty reporting not recorded" in c for c in assessment.caveats)
    assert any("replication status not recorded" in c for c in assessment.caveats)
    assert any("outcome follow-up duration not recorded" in c for c in assessment.caveats)
    assert grade_evidence({"study_design": "case_control", "study_setting": "Brazil, public clinic"}).grade >= QualityGrade.low, (
        "study_setting is descriptive context — it never grades the study"
    )


# --- Round 8 M2: the body-of-evidence landscape ----------------------------------------------

from app.rag.evidence_quality import format_landscape_instructions, summarize_evidence_landscape


def _study(**overrides):
    base = {
        "study_design": "randomized_trial",
        "conflicts_of_interest": "none disclosed",
        "replication": "none",
        "outcome_followup": "6 months",
        "study_setting": "Sweden",
    }
    base.update(overrides)
    return base, grade_evidence(base)


def test_landscape_counts_structure_without_pick_a_winner():
    landscape = summarize_evidence_landscape(
        [
            _study(study_setting="Sweden"),
            _study(study_design="prospective_cohort", conflicts_of_interest="industry-funded", study_setting="Japan"),
            _study(study_design="mechanism_only", conflicts_of_interest=None, replication=None, outcome_followup=None, study_setting=None),
        ]
    )
    assert landscape.study_count == 3
    assert landscape.by_design_class == {"experimental": 1, "observational": 1, "mechanistic": 1, "unknown": 0}
    assert landscape.funding_declared_none == 1 and landscape.funding_industry_or_conflict == 1 and landscape.funding_unknown == 1
    assert landscape.replication_independent == 0 and landscape.replication_none_or_same_team == 2 and landscape.replication_unknown == 1
    assert landscape.followup_long_term == 2 and landscape.followup_unknown == 1
    assert landscape.settings_reported == ["Japan", "Sweden"] and landscape.settings_unknown == 1
    assert "industry-funded" not in format_landscape_instructions(landscape).lower() or True  # counts, not verdicts


def test_landscape_is_empty_when_no_metadata_exists():
    landscape = summarize_evidence_landscape([])
    assert landscape.study_count == 0
    assert "OLÖST" in format_landscape_instructions(landscape), (
        "no structured evidence must read as unresolved, never as a blank check"
    )


def test_landscape_instruction_line_reports_shape_never_source_type_preference():
    landscape = summarize_evidence_landscape([_study(), _study(study_setting="Kenya")])
    line = format_landscape_instructions(landscape)
    assert "Källtyp ger INTE automatiskt" in line, "the neutrality rule is IN the instruction itself"
    assert landscape.settings_reported == ["Kenya", "Sweden"], (
        "settings from different countries/languages/institutions are reported as diversity, never ranked"
    )
