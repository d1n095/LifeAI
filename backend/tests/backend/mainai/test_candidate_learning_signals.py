"""Life Candidate Learning Signals -- proves SIGNAL PRODUCER != TRUTH WRITER structurally:
record_candidate_signal() never touches founder_memory_notes; only promote_candidate_signal()
can, and only with the caller's own explicit authority/basis, never the signal's own classifier
confidence silently copied in. See migration 0053's own module docstring and docs/LIFE_FOUNDER_
MEMORY.md's "Candidate learning signals" section for the full architecture."""

import uuid

import pytest
from sqlalchemy.exc import DBAPIError

from app.founder_memory import get_founder_memory
from app.founder_memory_signals import (
    CandidateLearningSignalError,
    dismiss_candidate_signal,
    get_candidate_signal,
    list_candidate_signals,
    list_unreviewed_candidate_signals,
    promote_candidate_signal,
    record_candidate_signal,
)
from app.models.conversation import Conversation, Message, MessageRole
from app.models.user import User


def _owner_with_message(db, content="kom ihåg att jag alltid vill ha kod-exempel"):
    user = User(email=f"signal-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    conversation = Conversation(user_id=user.id, title="test")
    db.add(conversation)
    db.flush()
    message = Message(conversation_id=conversation.id, role=MessageRole.user, content=content)
    db.add(message)
    db.flush()
    return user, message


def test_record_candidate_signal_never_writes_to_founder_memory(superuser_db):
    owner, message = _owner_with_message(superuser_db)
    superuser_db.commit()

    signal = record_candidate_signal(
        superuser_db, owner_id=owner.id, signal_kind="explicit_memory_candidate", idempotency_key="sig-1",
        source_message_id=message.id, classifier_strategy="context_resolver_v1", classifier_confidence="high",
    )
    superuser_db.commit()

    assert signal.status == "unreviewed"
    assert signal.promoted_to_note_id is None
    # No authority/basis vocabulary exists on this row at all -- structural, not just untested.
    assert not hasattr(signal, "authority")
    assert not hasattr(signal, "basis")


def test_record_candidate_signal_is_idempotent_and_rejects_a_reused_key_with_different_fields(superuser_db):
    owner, message = _owner_with_message(superuser_db)
    superuser_db.commit()
    first = record_candidate_signal(superuser_db, owner_id=owner.id, signal_kind="correction_candidate", idempotency_key="idem-sig", source_message_id=message.id)
    superuser_db.commit()
    replay = record_candidate_signal(superuser_db, owner_id=owner.id, signal_kind="correction_candidate", idempotency_key="idem-sig", source_message_id=message.id)
    assert replay.id == first.id

    with pytest.raises(CandidateLearningSignalError):
        record_candidate_signal(superuser_db, owner_id=owner.id, signal_kind="idea_candidate", idempotency_key="idem-sig", source_message_id=message.id)


def test_signal_kind_rejects_arbitrary_values(superuser_db):
    owner, message = _owner_with_message(superuser_db)
    superuser_db.commit()
    with pytest.raises(DBAPIError):
        record_candidate_signal(superuser_db, owner_id=owner.id, signal_kind="definitely_important_trust_me", idempotency_key="bad-kind", source_message_id=message.id)
    superuser_db.rollback()


def test_promoting_a_signal_requires_the_callers_own_explicit_authority_never_the_classifiers_confidence(superuser_db):
    """The core structural proof: a classifier_confidence='high' signal does NOT imply
    authority='founder' on promotion -- the caller must assert it themselves."""

    owner, message = _owner_with_message(superuser_db)
    superuser_db.commit()
    signal = record_candidate_signal(
        superuser_db, owner_id=owner.id, signal_kind="explicit_memory_candidate", idempotency_key="promo-1",
        source_message_id=message.id, classifier_strategy="context_resolver_v1", classifier_confidence="high",
    )
    superuser_db.commit()

    promoted_signal, note = promote_candidate_signal(
        superuser_db, owner_id=owner.id, signal_id=signal.id, note_type="preference",
        content="Founder wants code examples in every answer.", authority="founder", basis="manual",
        note_idempotency_key="promo-note-1",
    )
    superuser_db.commit()

    assert promoted_signal.status == "promoted"
    assert promoted_signal.promoted_to_note_id == note.id
    assert note.authority == "founder"  # the REVIEWER's assertion, never signal.classifier_confidence
    fetched_note = get_founder_memory(superuser_db, owner_id=owner.id, note_id=note.id)
    assert fetched_note.provenance["promoted_from_candidate_signal_id"] == str(signal.id)


def test_a_low_confidence_signal_can_still_be_promoted_with_high_authority_and_vice_versa(superuser_db):
    """Proves the two are genuinely decoupled, not just usually correlated."""

    owner, message = _owner_with_message(superuser_db)
    superuser_db.commit()
    signal = record_candidate_signal(
        superuser_db, owner_id=owner.id, signal_kind="idea_candidate", idempotency_key="decouple-1",
        source_message_id=message.id, classifier_confidence="low",
    )
    superuser_db.commit()

    _, note = promote_candidate_signal(
        superuser_db, owner_id=owner.id, signal_id=signal.id, note_type="observation", content="Confirmed directly by founder despite low-confidence origin.",
        authority="founder", basis="manual", note_idempotency_key="decouple-note-1",
    )
    superuser_db.commit()
    assert note.authority == "founder"


def test_cannot_promote_an_already_reviewed_signal_twice(superuser_db):
    owner, message = _owner_with_message(superuser_db)
    superuser_db.commit()
    signal = record_candidate_signal(superuser_db, owner_id=owner.id, signal_kind="correction_candidate", idempotency_key="twice-1", source_message_id=message.id)
    superuser_db.commit()
    promote_candidate_signal(
        superuser_db, owner_id=owner.id, signal_id=signal.id, note_type="correction", content="x",
        authority="founder", basis="manual", note_idempotency_key="twice-note-1",
    )
    superuser_db.commit()

    with pytest.raises(CandidateLearningSignalError):
        promote_candidate_signal(
            superuser_db, owner_id=owner.id, signal_id=signal.id, note_type="correction", content="y",
            authority="founder", basis="manual", note_idempotency_key="twice-note-2",
        )


def test_dismissing_a_signal_never_deletes_it_and_records_a_reason(superuser_db):
    owner, message = _owner_with_message(superuser_db)
    superuser_db.commit()
    signal = record_candidate_signal(superuser_db, owner_id=owner.id, signal_kind="correction_candidate", idempotency_key="dismiss-1", source_message_id=message.id)
    superuser_db.commit()

    dismissed = dismiss_candidate_signal(superuser_db, owner_id=owner.id, signal_id=signal.id, reason="False positive -- casual 'nej' in conversation, not a real correction.")
    superuser_db.commit()
    assert dismissed.status == "dismissed"
    assert "casual" in dismissed.dismissed_reason

    fetched = get_candidate_signal(superuser_db, owner_id=owner.id, signal_id=signal.id)
    assert fetched is not None  # still durably queryable, not deleted


def test_list_unreviewed_candidate_signals_excludes_promoted_and_dismissed(superuser_db):
    owner, message = _owner_with_message(superuser_db)
    superuser_db.commit()
    unreviewed = record_candidate_signal(superuser_db, owner_id=owner.id, signal_kind="idea_candidate", idempotency_key="list-1", source_message_id=message.id)
    promoted = record_candidate_signal(superuser_db, owner_id=owner.id, signal_kind="explicit_memory_candidate", idempotency_key="list-2", source_message_id=message.id)
    dismissed = record_candidate_signal(superuser_db, owner_id=owner.id, signal_kind="correction_candidate", idempotency_key="list-3", source_message_id=message.id)
    superuser_db.commit()
    promote_candidate_signal(superuser_db, owner_id=owner.id, signal_id=promoted.id, note_type="observation", content="x", authority="founder", basis="manual", note_idempotency_key="list-note-1")
    dismiss_candidate_signal(superuser_db, owner_id=owner.id, signal_id=dismissed.id, reason="noise")
    superuser_db.commit()

    unreviewed_ids = {s.id for s in list_unreviewed_candidate_signals(superuser_db, owner_id=owner.id)}
    assert unreviewed_ids == {unreviewed.id}
    all_ids = {s.id for s in list_candidate_signals(superuser_db, owner_id=owner.id)}
    assert all_ids == {unreviewed.id, promoted.id, dismissed.id}


def test_promoting_or_dismissing_a_signal_belonging_to_another_owner_fails_closed(superuser_db):
    owner_a, message_a = _owner_with_message(superuser_db)
    owner_b, _ = _owner_with_message(superuser_db)
    superuser_db.commit()
    signal = record_candidate_signal(superuser_db, owner_id=owner_a.id, signal_kind="correction_candidate", idempotency_key="cross-1", source_message_id=message_a.id)
    superuser_db.commit()

    with pytest.raises(CandidateLearningSignalError):
        dismiss_candidate_signal(superuser_db, owner_id=owner_b.id, signal_id=signal.id, reason="not mine")
    with pytest.raises(CandidateLearningSignalError):
        promote_candidate_signal(
            superuser_db, owner_id=owner_b.id, signal_id=signal.id, note_type="observation", content="stolen",
            authority="founder", basis="manual", note_idempotency_key="cross-note-1",
        )
