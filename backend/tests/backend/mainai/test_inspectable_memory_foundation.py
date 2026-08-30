"""Stage A — Canonical Inspectable Memory Foundation.

Proves: projection over existing tables, truth-claim receipts with verify-against-reality,
supersession history, no authority invention, registry widen for linkable kinds.
"""

import uuid

import pytest
from sqlalchemy import select

from app.active_context.service import SUPPORTED_TYPES
from app.inspectable_memory import (
    InspectableMemoryError,
    MemoryTruthState,
    founder_add_memory_note,
    founder_correct_memory_note,
    get_inspectable_memory_history,
    list_inspectable_memory,
    list_truth_claim_violations,
    record_truth_claim,
    verify_truth_claim,
)
from app.models.mainai_execution import EngineeringLesson, EngineeringLessonConfidence, EngineeringLessonSeverity, EngineeringLessonStatus
from app.models.memory_truth_claim import MemoryTruthClaim
from app.models.user import User


def _owner(db):
    user = User(email=f"insp-mem-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def test_registry_includes_memory_frontier_kinds():
    for kind in ("candidate_learning_signal", "work_candidate", "project_entity"):
        assert kind in SUPPORTED_TYPES


def test_founder_add_persists_note_and_verified_stored_claim(superuser_db):
    owner = _owner(superuser_db)
    note, claim = founder_add_memory_note(
        superuser_db,
        owner_id=owner.id,
        content="Jag vill ha korta svar.",
        note_type="preference",
        idempotency_key="add-1",
    )
    superuser_db.commit()

    assert note.content.startswith("Jag vill")
    assert claim.claimed_state == MemoryTruthState.STORED.value
    assert claim.target_id == note.id
    assert claim.verified_result is True

    items = list_inspectable_memory(superuser_db, owner_id=owner.id, kind="founder_memory_note")
    assert any(i.id == note.id and i.truth_state == MemoryTruthState.STORED for i in items)


def test_correction_preserves_history_chain(superuser_db):
    owner = _owner(superuser_db)
    original, _ = founder_add_memory_note(
        superuser_db,
        owner_id=owner.id,
        content="Använd MongoDB.",
        note_type="decision",
        idempotency_key="dec-1",
    )
    superuser_db.commit()
    corrected, _ = founder_correct_memory_note(
        superuser_db,
        owner_id=owner.id,
        note_id=original.id,
        content="Använd Postgres.",
        idempotency_key="dec-1-fix",
    )
    superuser_db.commit()

    history = get_inspectable_memory_history(superuser_db, owner_id=owner.id, item_id=corrected.id)
    assert [h.normalized_interpretation for h in history] == ["Använd MongoDB.", "Använd Postgres."]
    assert history[0].factual_status == "superseded"
    assert history[1].factual_status == "active"


def test_false_claim_is_durable_violation(superuser_db):
    owner = _owner(superuser_db)
    missing = uuid.uuid4()
    claim = record_truth_claim(
        superuser_db,
        owner_id=owner.id,
        claim_text="I've saved that preference",
        claimed_state=MemoryTruthState.STORED.value,
        target_kind="founder_memory_note",
        target_id=missing,
        idempotency_key="false-1",
        verify_now=True,
    )
    superuser_db.commit()
    assert claim.verified_result is False
    violations = list_truth_claim_violations(superuser_db, owner_id=owner.id)
    assert any(v.id == claim.id for v in violations)


def test_claim_built_from_returned_row_verifies_true(superuser_db):
    owner = _owner(superuser_db)
    note, _ = founder_add_memory_note(
        superuser_db,
        owner_id=owner.id,
        content="Explicit preference text.",
        note_type="preference",
        idempotency_key="pref-claim",
    )
    # Second claim from returned row attributes (calling convention).
    claim = record_truth_claim(
        superuser_db,
        owner_id=owner.id,
        claim_text=f"stored note {note.id}: {note.content}",
        claimed_state=MemoryTruthState.STORED.value,
        target_kind="founder_memory_note",
        target_id=note.id,
        idempotency_key="pref-claim-2",
        verify_now=True,
    )
    superuser_db.commit()
    assert claim.verified_result is True


def test_said_without_target_is_allowed_and_self_verified(superuser_db):
    owner = _owner(superuser_db)
    claim = record_truth_claim(
        superuser_db,
        owner_id=owner.id,
        claim_text="founder said: få med det här med",
        claimed_state=MemoryTruthState.SAID.value,
        target_kind="candidate_learning_signal",
        target_id=None,
        idempotency_key="said-1",
        verify_now=True,
    )
    superuser_db.commit()
    assert claim.verified_result is True


def test_non_said_requires_target(superuser_db):
    owner = _owner(superuser_db)
    with pytest.raises(InspectableMemoryError):
        record_truth_claim(
            superuser_db,
            owner_id=owner.id,
            claim_text="stored something",
            claimed_state=MemoryTruthState.STORED.value,
            target_kind="founder_memory_note",
            target_id=None,
            idempotency_key="bad-stored",
        )


def test_engineering_lesson_verification_status_defaults_unverified(superuser_db):
    lesson = EngineeringLesson(
        status=EngineeringLessonStatus.active,
        problem="lease race",
        root_cause="missing fence",
        affected_component="operator",
        severity=EngineeringLessonSeverity.high,
        evidence="test",
        fix="add fence",
        regression_test="test_operator_lease",
        general_rule="fence at effect time",
        applies_to=["lease"],
        source_type="test",
        source_ref="stage-a",
        first_seen_at=__import__("datetime").datetime.utcnow(),
        confidence=EngineeringLessonConfidence.likely,
        created_by="cursor-stage-a",
    )
    superuser_db.add(lesson)
    superuser_db.flush()
    superuser_db.commit()
    fetched = superuser_db.get(EngineeringLesson, lesson.id)
    assert fetched.verification_status == "unverified"

    items = list_inspectable_memory(superuser_db, owner_id=_owner(superuser_db).id, kind="engineering_lesson")
    assert any(i.id == lesson.id and i.verification_status == "unverified" for i in items)


def test_truth_claim_idempotent(superuser_db):
    owner = _owner(superuser_db)
    note, claim1 = founder_add_memory_note(
        superuser_db, owner_id=owner.id, content="x", note_type="observation", idempotency_key="idem-note"
    )
    claim2 = record_truth_claim(
        superuser_db,
        owner_id=owner.id,
        claim_text=claim1.claim_text,
        claimed_state=claim1.claimed_state,
        target_kind=claim1.target_kind,
        target_id=claim1.target_id,
        idempotency_key=claim1.idempotency_key,
        verify_now=True,
    )
    assert claim2.id == claim1.id
    count = superuser_db.execute(
        select(MemoryTruthClaim).where(MemoryTruthClaim.owner_id == owner.id)
    ).scalars().all()
    assert len(count) == 1
    assert note.id == claim1.target_id


def test_reverify_updates_result(superuser_db):
    owner = _owner(superuser_db)
    claim = record_truth_claim(
        superuser_db,
        owner_id=owner.id,
        claim_text="pending said",
        claimed_state=MemoryTruthState.SAID.value,
        target_kind="candidate_learning_signal",
        target_id=None,
        idempotency_key="reverify-1",
        verify_now=False,
    )
    superuser_db.commit()
    assert claim.verified_result is None
    updated = verify_truth_claim(superuser_db, owner_id=owner.id, claim_id=claim.id)
    superuser_db.commit()
    assert updated.verified_result is True
    assert updated.verified_at is not None
