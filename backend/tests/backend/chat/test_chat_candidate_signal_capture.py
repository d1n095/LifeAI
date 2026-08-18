"""Proves `app/routers/chat.py`'s resolver-to-candidate-signal wiring: SIGNAL PRODUCER != TRUTH
WRITER end to end at the router layer, not just inside app.founder_memory_signals.service in
isolation. Tests the helper function directly (no provider/HTTP mocking needed) since it is a
pure DB-side-effect function, matching the same "purely observational, testable without a
provider key" doctrine app.context.resolver's own module docstring already establishes."""

import uuid

from app.context.resolver import CONFIDENCE_HIGH, INTENT_CORRECTION, INTENT_EXPLICIT_MEMORY, INTENT_NEW_TOPIC, ContextResolution
from app.founder_memory_signals import list_candidate_signals
from app.models.candidate_learning_signal import CandidateLearningSignal
from app.models.conversation import Conversation, Message, MessageRole
from app.models.user import User
from app.routers.chat import _record_candidate_signal_if_worth_noticing


def _owner_with_message(db):
    user = User(email=f"chat-signal-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    conversation = Conversation(user_id=user.id, title="test")
    db.add(conversation)
    db.flush()
    message = Message(conversation_id=conversation.id, role=MessageRole.user, content="kom ihåg att jag hatar långa svar")
    db.add(message)
    db.flush()
    return user, message


def test_an_explicit_memory_intent_produces_a_candidate_signal_never_a_founder_memory_note(superuser_db):
    owner, message = _owner_with_message(superuser_db)
    superuser_db.commit()

    resolution = ContextResolution(intent=INTENT_EXPLICIT_MEMORY, confidence=CONFIDENCE_HIGH, reasoning="Explicit minnesmarkör: 'kom ihåg'.")
    _record_candidate_signal_if_worth_noticing(superuser_db, owner_id=owner.id, user_message=message, context_resolution=resolution)

    signals = list_candidate_signals(superuser_db, owner_id=owner.id)
    assert len(signals) == 1
    assert signals[0].signal_kind == "explicit_memory_candidate"
    assert signals[0].classifier_strategy == "context_resolver_v1"
    assert signals[0].classifier_confidence == "high"
    assert signals[0].source_message_id == message.id
    assert signals[0].status == "unreviewed"  # never auto-promoted


def test_an_uninteresting_intent_produces_no_signal_at_all(superuser_db):
    owner, message = _owner_with_message(superuser_db)
    superuser_db.commit()

    resolution = ContextResolution(intent=INTENT_NEW_TOPIC, confidence=CONFIDENCE_HIGH, reasoning="Ny konversation.")
    _record_candidate_signal_if_worth_noticing(superuser_db, owner_id=owner.id, user_message=message, context_resolution=resolution)

    assert list_candidate_signals(superuser_db, owner_id=owner.id) == []


def test_a_correction_intent_maps_to_correction_candidate(superuser_db):
    owner, message = _owner_with_message(superuser_db)
    superuser_db.commit()

    resolution = ContextResolution(intent=INTENT_CORRECTION, confidence=CONFIDENCE_HIGH, reasoning="Korrigeringsmarkör: 'nej,'.")
    _record_candidate_signal_if_worth_noticing(superuser_db, owner_id=owner.id, user_message=message, context_resolution=resolution)

    signals = list_candidate_signals(superuser_db, owner_id=owner.id)
    assert len(signals) == 1
    assert signals[0].signal_kind == "correction_candidate"


def test_replaying_the_same_message_never_creates_a_duplicate_signal(superuser_db):
    owner, message = _owner_with_message(superuser_db)
    superuser_db.commit()
    resolution = ContextResolution(intent=INTENT_EXPLICIT_MEMORY, confidence=CONFIDENCE_HIGH, reasoning="x")

    _record_candidate_signal_if_worth_noticing(superuser_db, owner_id=owner.id, user_message=message, context_resolution=resolution)
    _record_candidate_signal_if_worth_noticing(superuser_db, owner_id=owner.id, user_message=message, context_resolution=resolution)

    assert len(list_candidate_signals(superuser_db, owner_id=owner.id)) == 1


def test_a_failure_recording_the_signal_never_raises_into_the_caller(superuser_db):
    """The chat response must never break because signal capture failed -- same "core behavior
    doesn't depend on it" doctrine as resolve_context() itself. A nonexistent owner_id
    violates the real users(id) FK; this must be swallowed, not propagated."""

    owner, message = _owner_with_message(superuser_db)
    superuser_db.commit()
    resolution = ContextResolution(intent=INTENT_EXPLICIT_MEMORY, confidence=CONFIDENCE_HIGH, reasoning="x")

    _record_candidate_signal_if_worth_noticing(superuser_db, owner_id=uuid.uuid4(), user_message=message, context_resolution=resolution)
    # No exception raised above is the assertion itself. Confirm nothing was written either.
    assert superuser_db.query(CandidateLearningSignal).filter_by(source_message_id=message.id).count() == 0
