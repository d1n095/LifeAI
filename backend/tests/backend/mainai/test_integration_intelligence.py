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
            root_cause="root cause Y", fix="validated fix Z", source_ref="unit-test"):
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
    )
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
    assert s["validated_fix"].startswith("treat LocalCLIAdapter as single-shot")
    assert s["affected_component"] == "agent_coordination.adapters"


def test_unverified_lesson_is_surfaced_but_not_promoted_to_fact(db_session):
    tag = f"conf-{uuid.uuid4().hex[:6]}"
    likely = _record(db_session, applies_to=[tag], affected_component="x", confidence=EngineeringLessonConfidence.likely)
    certain = _record(db_session, applies_to=[tag], affected_component="y", confidence=EngineeringLessonConfidence.certain)
    by_id = {str(s["lesson_id"]): s for s in integration_lessons_for_task_context(db_session, extra_tags=[tag], max_lessons=10)}
    assert by_id[str(likely.id)]["verified"] is False  # a hypothesis stays a hypothesis
    assert by_id[str(likely.id)]["confidence"] == "likely"
    assert by_id[str(certain.id)]["verified"] is True  # only an explicitly 'certain' lesson is a fact


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
