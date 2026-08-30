"""docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md §5 (missed-thing learning): turns a
confirmed FounderMemoryNote (note_type="correction") into a real EngineeringLesson, reusing
record_lesson() completely unchanged -- no new table. Proves the MISS -> LESSON pipeline is
real (retrievable via lookup_lessons(), exactly like a verification-failure-sourced lesson
already is), and that a non-correction note is refused rather than silently accepted."""

import uuid
from datetime import datetime

import pytest

from app.founder_memory import record_founder_memory
from app.mainai_execution.lessons import lookup_lessons, record_lesson_from_founder_correction
from app.models.mainai_execution import EngineeringLessonConfidence, EngineeringLessonSeverity
from app.models.user import User
from app.request_context import current_user_id as current_user_id_var
from sqlalchemy import text as sa_text


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _owner(db):
    user = User(email=f"lesson-correction-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    _set_rls_user(db, user.id)
    return user.id


def test_record_lesson_from_founder_correction_creates_a_real_retrievable_lesson(superuser_db):
    owner_id = _owner(superuser_db)
    superuser_db.commit()

    note = record_founder_memory(
        superuser_db, owner_id=owner_id, note_type="correction",
        content="Why didn't you think of the migration downgrade path when you built the upgrade?",
        idempotency_key="correction-1", authority="founder", basis="manual",
    )
    superuser_db.commit()

    lesson = record_lesson_from_founder_correction(
        superuser_db, note=note,
        root_cause="New migrations were written and tested for upgrade() only; downgrade() was an afterthought or skipped.",
        affected_component="alembic/versions/",
        general_rule="Every new migration must have a real, tested downgrade() before it's considered done, not just upgrade().",
        applies_to=["migration"],
        created_by="test",
        fix="Always write and test the downgrade() path in the same review pass as upgrade().",
        severity=EngineeringLessonSeverity.medium,
        confidence=EngineeringLessonConfidence.likely,
    )
    superuser_db.commit()

    assert lesson.problem == note.content  # quoted verbatim from the note, never paraphrased
    assert lesson.source_type == "founder_correction"
    assert lesson.source_ref == str(note.id)
    assert lesson.evidence == f"founder_memory_notes:{note.id}"
    assert lesson.first_seen_at == note.observed_at

    # Retrievable exactly like any other lesson -- the whole point of turning a miss into a
    # structured, generalized, RETRIEVABLE lesson, not just a logged remark.
    found = lookup_lessons(superuser_db, applies_to_any=["migration"])
    assert lesson.id in [row.id for row in found]


def test_record_lesson_from_founder_correction_rejects_a_non_correction_note(superuser_db):
    owner_id = _owner(superuser_db)
    superuser_db.commit()

    note = record_founder_memory(
        superuser_db, owner_id=owner_id, note_type="preference",
        content="I prefer concise answers.", idempotency_key="pref-1", authority="founder", basis="manual",
    )
    superuser_db.commit()

    with pytest.raises(ValueError, match="note_type='correction'"):
        record_lesson_from_founder_correction(
            superuser_db, note=note, root_cause="n/a", affected_component="n/a",
            general_rule="n/a", applies_to=["x"], created_by="test", fix="n/a",
        )


def test_record_lesson_from_founder_correction_defaults_severity_to_medium(superuser_db):
    owner_id = _owner(superuser_db)
    superuser_db.commit()
    note = record_founder_memory(
        superuser_db, owner_id=owner_id, note_type="correction", content="We forgot the RLS test for this table.",
        idempotency_key="correction-2", authority="founder", basis="manual",
    )
    superuser_db.commit()

    lesson = record_lesson_from_founder_correction(
        superuser_db, note=note, root_cause="New table PR didn't include an RLS isolation test.",
        affected_component="tests/security/", general_rule="Every new RLS-protected table needs its own isolation test in the same PR.",
        applies_to=["new_table"], created_by="test", fix="Add an RLS isolation test checklist item to the new-table review.",
    )
    superuser_db.commit()

    assert lesson.severity.value == "medium"
