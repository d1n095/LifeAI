"""Integration intelligence — retrieve relevant PAST integration failures and attach them to a
compact agent task-context package. Reuses app.mainai_execution.lessons (EngineeringLesson); no
parallel memory. Real PostgreSQL for the lesson store; pure for the packet attach.
"""

import uuid
from datetime import datetime

import pytest

from app.mainai_execution import lessons as lessons_mod
from app.mainai_execution.integration_intelligence import (
    integration_lessons_for_task_context,
    retrieve_integration_lessons,
    summarize_integration_lesson,
)
from app.models.mainai_execution import (
    EngineeringLessonConfidence,
    EngineeringLessonSeverity,
    EngineeringLessonStatus,
)
from app.workforce.context_handoff import ContextHandoffError, build_agent_task_context


def _record(db, *, applies_to, affected_component, confidence, problem="symptom X",
            root_cause="root cause Y", fix="validated fix Z", source_ref="unit-test",
            verification_status=None, regression_test=None):
    lesson = lessons_mod.record_lesson(
        db,
        problem=problem,
        root_cause=root_cause,
        affected_component=affected_component,
        severity=EngineeringLessonSeverity.high,
        evidence="observed earlier",
        fix=fix,
        general_rule="general rule",
        applies_to=applies_to,
        source_type="test",
        source_ref=source_ref,
        created_by="test",
        first_seen_at=datetime.utcnow(),
        confidence=confidence,
        regression_test=regression_test,
    )
    if verification_status is not None:
        lesson.verification_status = verification_status  # actual verification state, set post-hoc
    db.commit()
    return lesson


def test_retrieve_lessons_relevant_to_an_integration_context_by_tag(db_session):
    tag = f"app.providers.registry-{uuid.uuid4().hex[:6]}"
    relevant = _record(
        db_session, applies_to=[tag, "openai"], affected_component="providers.registry",
        confidence=EngineeringLessonConfidence.certain,
    )
    _record(
        db_session, applies_to=[f"frontend-{uuid.uuid4().hex[:6]}"], affected_component="frontend",
        confidence=EngineeringLessonConfidence.likely,
    )
    hits = retrieve_integration_lessons(db_session, components=[tag])
    ids = {str(h.id) for h in hits}
    assert str(relevant.id) in ids  # the matching integration context is retrieved
    # providers tag also matches via the same relevance space
    assert {str(h.id) for h in retrieve_integration_lessons(db_session, providers=["openai"])} >= {str(relevant.id)}


def test_summary_distinguishes_symptom_root_cause_and_validated_fix(db_session):
    tag = f"ctx-{uuid.uuid4().hex[:6]}"
    lesson = _record(
        db_session, applies_to=[tag], affected_component="agent_coordination.adapters",
        confidence=EngineeringLessonConfidence.certain,
        problem="single-shot adapter reported as interactive",
        root_cause="adapter protocol not fully implemented",
        fix="treat LocalCLIAdapter as single-shot; report UNSUPPORTED for resume",
    )
    s = summarize_integration_lesson(lesson)
    assert s["symptom"] == "single-shot adapter reported as interactive"
    assert s["root_cause"] == "adapter protocol not fully implemented"
    assert s["fix"].startswith("treat LocalCLIAdapter as single-shot")
    assert s["fix_status"] == "proposed"  # no regression validation yet -> proposed, not validated
    assert s["affected_component"] == "agent_coordination.adapters"


def test_confidence_alone_never_marks_a_lesson_verified(db_session):
    """Verification is SEPARATE from confidence: a 'certain' but unverified lesson is NOT verified;
    a low-confidence lesson validated by a regression test IS."""
    tag = f"conf-{uuid.uuid4().hex[:6]}"
    certain_unverified = _record(
        db_session, applies_to=[tag], affected_component="x",
        confidence=EngineeringLessonConfidence.certain,  # author is confident ...
    )  # ... but verification_status defaults to 'unverified'
    likely_validated = _record(
        db_session, applies_to=[tag], affected_component="y",
        confidence=EngineeringLessonConfidence.likely,
        regression_test="tests/backend/test_x.py::test_regression",
        verification_status="verified_by_regression_test",
    )
    by_id = {str(s["lesson_id"]): s for s in integration_lessons_for_task_context(db_session, extra_tags=[tag], max_lessons=10)}
    cu = by_id[str(certain_unverified.id)]
    lv = by_id[str(likely_validated.id)]
    assert cu["confidence"] == "certain" and cu["verification_status"] == "unverified"
    assert cu["verified"] is False and cu["fix_status"] == "proposed"  # confidence != verification
    assert lv["confidence"] == "likely" and lv["verification_status"] == "verified_by_regression_test"
    assert lv["verified"] is True and lv["fix_status"] == "validated_by_regression_test"


def test_regression_validated_but_non_active_lesson_is_not_current_validated_guidance(db_session):
    """A regression-validated lesson that is no longer active must not be surfaced as current,
    validated guidance (requirement 5)."""
    from app.models.mainai_execution import EngineeringLessonStatus

    lesson = _record(
        db_session, applies_to=[f"old-{uuid.uuid4().hex[:6]}"], affected_component="z",
        confidence=EngineeringLessonConfidence.certain,
        regression_test="tests/backend/test_old.py::test_regression",
        verification_status="verified_by_regression_test",
    )
    db_session.refresh(lesson)
    lesson.status = EngineeringLessonStatus.historical
    db_session.commit()
    s = summarize_integration_lesson(lesson)
    assert s["status"] == "historical"
    assert s["verified"] is False  # validated once, but no longer current guidance


def test_obsolete_non_active_lesson_is_not_surfaced_as_current(db_session):
    tag = f"obsolete-{uuid.uuid4().hex[:6]}"
    lesson = _record(db_session, applies_to=[tag], affected_component="z", confidence=EngineeringLessonConfidence.certain)
    assert {str(h.id) for h in retrieve_integration_lessons(db_session, extra_tags=[tag])} == {str(lesson.id)}

    # The earlier fix becomes obsolete after code/dependency/API changes -> no longer active.
    # (NB: the schema's ck_engineering_lessons_superseded_requires_link enforces that a 'superseded'
    # lesson must carry a supersession link; 'historical' is the link-free "no longer current" state.)
    db_session.refresh(lesson)
    lesson.status = EngineeringLessonStatus.historical
    db_session.commit()
    assert retrieve_integration_lessons(db_session, extra_tags=[tag]) == []  # not presented as current


def test_lessons_attach_to_agent_task_context_packet(db_session):
    tag = f"attach-{uuid.uuid4().hex[:6]}"
    lesson = _record(db_session, applies_to=[tag], affected_component="providers.registry",
                     confidence=EngineeringLessonConfidence.certain, source_ref="pr#372")
    summaries = integration_lessons_for_task_context(db_session, extra_tags=[tag])
    assert summaries

    ctx = build_agent_task_context(
        task_id="t1", objective="wire provider egress", acceptance_criteria=["egress gated"],
        repo="d1n095/LifeAI", branch="b", base_sha="deadbeef", trust_zone="INTERNAL",
        integration_lessons=summaries,
    )
    packet = ctx.to_transmittable_dict()
    assert packet["integration_lessons"]
    attached = packet["integration_lessons"][0]
    assert attached["lesson_id"] == str(lesson.id)
    assert attached["source_ref"] == "pr#372"  # explains how history informed the task (req 10)
    # Attaching lessons does not break the dispatch-boundary contract (packet keys are a superset).
    for required in ("task_id", "branch", "base_sha", "acceptance_criteria"):
        assert required in packet


def test_never_egress_marked_lesson_refuses_the_packet(db_session):
    bad_summary = [{"lesson_id": "x", "symptom": "NEVER_EGRESS: secret leaked in a past job"}]
    with pytest.raises(ContextHandoffError):
        build_agent_task_context(
            task_id="t1", objective="o", acceptance_criteria=["a"], repo="r", branch="b",
            base_sha="s", trust_zone="INTERNAL", integration_lessons=bad_summary,
        )
