"""Row-Level Security, exercised directly at the database layer through the restricted
runtime role (mainai_app), for `candidate_learning_signals` (migration 0053). Mirrors
tests/security/test_rls_isolation_cognition_foundation.py's own established pattern exactly --
applying the discipline docs/LIFE_COGNITION_FOUNDATION_REVIEW_2026-08-18.md's Finding 3
established: every new owner-scoped table gets a real behavioral proof, not just a structural
one, from the moment it is added."""

from sqlalchemy import text

from app.founder_memory_signals import record_candidate_signal
from app.models.candidate_learning_signal import CandidateLearningSignal
from app.models.conversation import Conversation, Message, MessageRole


def _set_rls_user(session, user_id) -> None:
    session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user_id)})


def _message_for(session, owner_id):
    conversation = Conversation(user_id=owner_id, title="rls test")
    session.add(conversation)
    session.flush()
    message = Message(conversation_id=conversation.id, role=MessageRole.user, content="test")
    session.add(message)
    session.flush()
    return message


def test_user_never_reads_another_users_candidate_learning_signals(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    message_a = _message_for(db_session, user_a.id)
    db_session.commit()
    _set_rls_user(db_session, user_a.id)
    record_candidate_signal(db_session, owner_id=user_a.id, signal_kind="correction_candidate", idempotency_key="rls-sig-a", source_message_id=message_a.id)
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    message_b = _message_for(db_session, user_b.id)
    db_session.commit()
    _set_rls_user(db_session, user_b.id)
    record_candidate_signal(db_session, owner_id=user_b.id, signal_kind="correction_candidate", idempotency_key="rls-sig-b", source_message_id=message_b.id)
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    visible_to_a = db_session.query(CandidateLearningSignal).all()
    assert len(visible_to_a) == 1
    assert visible_to_a[0].owner_id == user_a.id

    _set_rls_user(db_session, user_b.id)
    visible_to_b = db_session.query(CandidateLearningSignal).all()
    assert len(visible_to_b) == 1
    assert visible_to_b[0].owner_id == user_b.id


def test_cannot_write_a_candidate_learning_signal_for_another_user(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    db_session.add(CandidateLearningSignal(owner_id=user_b.id, signal_kind="correction_candidate", idempotency_key="rls-cross-write"))
    try:
        db_session.commit()
        assert False, "insert should have been rejected by candidate_learning_signals_isolation's WITH CHECK"
    except Exception:
        db_session.rollback()
