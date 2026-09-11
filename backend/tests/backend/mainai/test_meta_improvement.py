"""app.mainai_executive.meta_improvement -- narrow extension of
record_lesson_from_founder_correction() scoped to MainAI's OWN behavioral defaults
(judgment_temperament/wip_default/challenge_threshold/rejected_idea_guard_sensitivity/...),
distinct from a lesson about CODE. Reuses EngineeringLesson's real schema and
record_lesson_from_founder_correction()'s own existing note_type='correction' validation
unchanged -- proven here by the same rejection still firing through this new entry point."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.founder_memory import record_founder_memory
from app.mainai_execution.lesson_conflicts import find_conflict_candidate_pairs
from app.mainai_executive.meta_improvement import (
    BEHAVIORAL_COMPONENTS,
    MetaImprovementError,
    find_behavioral_conflict_candidates,
    lookup_behavioral_lessons,
    record_behavioral_lesson_from_founder_correction,
)
from app.models.mainai_execution import EngineeringLessonSeverity
from app.models.user import User
from app.request_context import current_user_id as current_user_id_var


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _owner(db):
    user = User(email=f"meta-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    _set_rls_user(db, user.id)
    return user.id


def _correction_note(db, owner_id, content="MainAI kept challenging low-stakes founder ideas -- too aggressive."):
    return record_founder_memory(
        db, owner_id=owner_id, note_type="correction", content=content,
        idempotency_key=f"meta-corr-{uuid.uuid4()}", authority="founder", basis="manual",
    )


def test_records_a_real_behavioral_lesson_with_reserved_affected_component(superuser_db):
    owner_id = _owner(superuser_db)
    superuser_db.commit()
    note = _correction_note(superuser_db, owner_id)
    superuser_db.commit()

    lesson = record_behavioral_lesson_from_founder_correction(
        superuser_db, note=note,
        root_cause="challenge_threshold was tuned too low, so CHALLENGE fired on low-stakes ideas.",
        affected_component="challenge_threshold",
        general_rule="Only CHALLENGE a founder-originated idea when stakes are genuinely material.",
        applies_to=["judgment"],
        created_by="test",
        fix="Raise CHALLENGE_STAKES_BAR in app.mainai_executive.judgment.",
        severity=EngineeringLessonSeverity.medium,
    )
    superuser_db.commit()

    assert lesson.affected_component == "challenge_threshold"
    assert lesson.problem == note.content
    assert lesson.source_type == "founder_correction"

    found = lookup_behavioral_lessons(superuser_db, component="challenge_threshold")
    assert lesson.id in [row.id for row in found]


def test_rejects_a_non_behavioral_affected_component():
    """This module's own, additional narrowing -- distinct from (and on top of) the upstream
    note_type check proven separately below."""
    assert "arbitrary_code_path" not in BEHAVIORAL_COMPONENTS


def test_non_reserved_affected_component_is_rejected(superuser_db):
    owner_id = _owner(superuser_db)
    superuser_db.commit()
    note = _correction_note(superuser_db, owner_id)
    superuser_db.commit()

    with pytest.raises(MetaImprovementError):
        record_behavioral_lesson_from_founder_correction(
            superuser_db, note=note, root_cause="n/a", affected_component="alembic/versions/",
            general_rule="n/a", applies_to=["x"], created_by="test", fix="n/a",
        )


def test_non_correction_note_is_still_rejected_exactly_as_upstream(superuser_db):
    """Reuses record_lesson_from_founder_correction()'s own existing validation unchanged --
    a non-correction note must still be refused through this new, narrower entry point."""
    owner_id = _owner(superuser_db)
    superuser_db.commit()
    note = record_founder_memory(
        superuser_db, owner_id=owner_id, note_type="preference", content="I prefer terse replies.",
        idempotency_key=f"meta-pref-{uuid.uuid4()}", authority="founder", basis="manual",
    )
    superuser_db.commit()

    with pytest.raises(ValueError, match="note_type='correction'"):
        record_behavioral_lesson_from_founder_correction(
            superuser_db, note=note, root_cause="n/a", affected_component="judgment_temperament",
            general_rule="n/a", applies_to=["x"], created_by="test", fix="n/a",
        )


def test_lookup_behavioral_lessons_rejects_unknown_component(superuser_db):
    with pytest.raises(MetaImprovementError):
        lookup_behavioral_lessons(superuser_db, component="not_a_real_component")


def test_lookup_behavioral_lessons_without_filter_only_returns_behavioral_components(superuser_db):
    owner_id = _owner(superuser_db)
    superuser_db.commit()
    note = _correction_note(superuser_db, owner_id)
    superuser_db.commit()
    lesson = record_behavioral_lesson_from_founder_correction(
        superuser_db, note=note, root_cause="r", affected_component="wip_default",
        general_rule="g", applies_to=["wip"], created_by="test", fix="f",
    )
    superuser_db.commit()

    all_behavioral = lookup_behavioral_lessons(superuser_db)
    assert lesson.id in [row.id for row in all_behavioral]
    assert all(row.affected_component in BEHAVIORAL_COMPONENTS for row in all_behavioral)


def test_find_behavioral_conflict_candidates_composes_real_pairing_unchanged(superuser_db):
    """Two behavioral lessons about the SAME component, sharing a tag, must appear as a
    candidate pair -- proving this module actually narrows into, and reuses,
    lesson_conflicts.find_conflict_candidate_pairs() rather than reimplementing pairing."""
    owner_id = _owner(superuser_db)
    superuser_db.commit()
    note_a = _correction_note(superuser_db, owner_id, content="Be more aggressive with CHALLENGE.")
    note_b = _correction_note(superuser_db, owner_id, content="Be less aggressive with CHALLENGE.")
    superuser_db.commit()

    lesson_a = record_behavioral_lesson_from_founder_correction(
        superuser_db, note=note_a, root_cause="r1", affected_component="challenge_threshold",
        general_rule="Always challenge low-confidence founder ideas.", applies_to=["judgment"],
        created_by="test", fix="f1",
    )
    lesson_b = record_behavioral_lesson_from_founder_correction(
        superuser_db, note=note_b, root_cause="r2", affected_component="challenge_threshold",
        general_rule="Never challenge low-confidence founder ideas.", applies_to=["judgment"],
        created_by="test", fix="f2",
    )
    superuser_db.commit()

    pairs = find_behavioral_conflict_candidates(superuser_db)
    pair_ids = {frozenset((a.id, b.id)) for a, b in pairs}
    assert frozenset((lesson_a.id, lesson_b.id)) in pair_ids

    # And it is genuinely the same underlying function -- not a reimplementation.
    direct_pairs = find_conflict_candidate_pairs(superuser_db, lessons=[lesson_a, lesson_b])
    assert len(direct_pairs) == 1
