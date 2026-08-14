"""S1C message-source slice (PR #60 provisional proposal, migration 0037) — schema/trigger/RLS/
find-or-create/backfill coverage for `message_source_units`. Real local Postgres, mirroring
tests/backend/rag/test_memory_source_units.py's and test_memory_source_backfill.py's pattern
(RLS is exercised for real, not mocked) for the document-chunk subtype.

Not covered here (out of scope for this pass, no ChatGPT-format adapter exists yet per the
founder's explicit bootstrap mandate): ChatGPT-export-specific ingestion. This file covers the
migration + model + find-or-create + backfill slice for `Message` as a `memory_source_units`
subtype, plus the founder's required "no message is silently lost, duplicates converge, a
crash/restart continues" scenario chain applied to messages specifically.
"""

import threading
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import SessionLocal
from app.models.conversation import Conversation, Message, MessageRole
from app.models.memory_source_unit import LifecycleStatus, MemorySourceUnit, MessageSourceUnit, SnapshotStatus, SourceKind, SourceRole
from app.models.user import User, UserRole
from app.rag.backfill.message_source import backfill_message_source_units, candidate_message_ids, count_messages_without_source_unit
from app.rag.message_source import (
    MessageSourceIdentityConflict,
    MessageSourceLocator,
    compute_content_hash,
    get_or_create_message_source_unit,
    message_source_identity_key,
    source_role_for_message_role,
)
from app.request_context import current_user_id as current_user_id_var
from app.security import hash_password


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_user(session, email="msgsrc-owner@example.com", *, role=UserRole.founder) -> User:
    user = User(email=email, password_hash=hash_password("Sup3rS3cret!"), role=role, email_verified=True)
    session.add(user)
    session.commit()
    return user


def _make_conversation(session, owner_id, *, title="Konversation") -> Conversation:
    _set_rls_user(session, owner_id)
    conversation = Conversation(user_id=owner_id, title=title)
    session.add(conversation)
    session.commit()
    return conversation


def _make_message(session, owner_id, conversation_id, *, role=MessageRole.user, content="Hej, hur mar du?") -> Message:
    _set_rls_user(session, owner_id)
    message = Message(conversation_id=conversation_id, role=role, content=content)
    session.add(message)
    session.commit()
    return message


def _locator(owner_id, message: Message, conversation_id, *, content_text=None) -> MessageSourceLocator:
    return MessageSourceLocator(
        owner_id=owner_id,
        message_id=message.id,
        conversation_id=conversation_id,
        role=message.role,
        observed_at=message.created_at or datetime.now(timezone.utc),
        content_text=content_text if content_text is not None else message.content,
    )


# --- role mapping -----------------------------------------------------------------------


def test_source_role_for_message_role_maps_known_authorship():
    assert source_role_for_message_role(MessageRole.user) == SourceRole.founder
    assert source_role_for_message_role(MessageRole.assistant) == SourceRole.assistant
    assert source_role_for_message_role(MessageRole.system) == SourceRole.system


def test_message_source_identity_key_format():
    message_id = uuid.uuid4()
    assert message_source_identity_key(message_id) == f"message:{message_id}"


# --- find-or-create -----------------------------------------------------------------------


def test_get_or_create_message_source_unit_creates_parent_and_subtype():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        conversation = _make_conversation(session, owner.id)
        message = _make_message(session, owner.id, conversation.id, content="Bolaget grundades 2019.")
        _set_rls_user(session, owner.id)

        msu_id = get_or_create_message_source_unit(session, _locator(owner.id, message, conversation.id))
        session.commit()

        msu = session.get(MemorySourceUnit, msu_id)
        msgsu = session.get(MessageSourceUnit, msu_id)
        assert msu is not None
        assert msu.source_kind == SourceKind.message
        assert msu.source_role == SourceRole.founder
        assert msu.source_identity_key == f"message:{message.id}"
        assert msu.snapshot_status == SnapshotStatus.exact
        assert msu.lifecycle_status == LifecycleStatus.active
        assert msu.content_text == "Bolaget grundades 2019."
        content_hash, hash_version = compute_content_hash("Bolaget grundades 2019.")
        assert msu.content_hash == content_hash
        assert msu.content_hash_version == hash_version
        assert msgsu is not None
        assert msgsu.message_id == message.id
        assert msgsu.conversation_id == conversation.id
    finally:
        session.close()


def test_get_or_create_message_source_unit_maps_assistant_and_system_roles():
    session = SessionLocal()
    try:
        owner = _make_user(session, "role-mapping@example.com")
        conversation = _make_conversation(session, owner.id)
        assistant_msg = _make_message(session, owner.id, conversation.id, role=MessageRole.assistant, content="Svar.")
        system_msg = _make_message(session, owner.id, conversation.id, role=MessageRole.system, content="Systemprompt.")
        _set_rls_user(session, owner.id)

        assistant_id = get_or_create_message_source_unit(session, _locator(owner.id, assistant_msg, conversation.id))
        system_id = get_or_create_message_source_unit(session, _locator(owner.id, system_msg, conversation.id))
        session.commit()

        assert session.get(MemorySourceUnit, assistant_id).source_role == SourceRole.assistant
        assert session.get(MemorySourceUnit, system_id).source_role == SourceRole.system
    finally:
        session.close()


def test_get_or_create_message_source_unit_is_idempotent_no_duplicate():
    session = SessionLocal()
    try:
        owner = _make_user(session, "idempotent@example.com")
        conversation = _make_conversation(session, owner.id)
        message = _make_message(session, owner.id, conversation.id)
        _set_rls_user(session, owner.id)

        first_id = get_or_create_message_source_unit(session, _locator(owner.id, message, conversation.id))
        session.commit()
        second_id = get_or_create_message_source_unit(session, _locator(owner.id, message, conversation.id))
        session.commit()

        assert first_id == second_id
        count = session.execute(
            sa_text("SELECT count(*) FROM message_source_units WHERE owner_id = :oid"), {"oid": str(owner.id)}
        ).scalar()
        assert count == 1
    finally:
        session.close()


def test_get_or_create_message_source_unit_real_concurrent_insert_converges():
    """Two threads racing to create the SAME message's source unit — mirrors
    test_memory_source_units.py's equivalent concurrency proof — must converge on exactly one
    row, never raise an unhandled IntegrityError to either caller."""
    setup_session = SessionLocal()
    try:
        owner = _make_user(setup_session, "concurrent-msgsrc@example.com")
        conversation = _make_conversation(setup_session, owner.id)
        message = _make_message(setup_session, owner.id, conversation.id, content="Race condition text.")
        owner_id, conversation_id = owner.id, conversation.id
    finally:
        setup_session.close()

    results: dict = {}
    errors: dict = {}

    def _worker(name):
        session = SessionLocal()
        try:
            _set_rls_user(session, owner_id)
            results[name] = get_or_create_message_source_unit(
                session, _locator(owner_id, message, conversation_id)
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001 - captured and asserted on below
            errors[name] = exc
        finally:
            session.close()

    thread_a = threading.Thread(target=_worker, args=("a",))
    thread_b = threading.Thread(target=_worker, args=("b",))
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    assert not thread_a.is_alive() and not thread_b.is_alive()
    assert not errors, f"a worker raised unexpectedly: {errors}"
    assert results["a"] == results["b"]

    verify_session = SessionLocal()
    try:
        _set_rls_user(verify_session, owner_id)
        count = verify_session.execute(
            sa_text("SELECT count(*) FROM message_source_units WHERE owner_id = :oid"), {"oid": str(owner_id)}
        ).scalar()
        assert count == 1
    finally:
        verify_session.close()


def test_get_or_create_message_source_unit_rejects_mismatched_locator():
    session = SessionLocal()
    try:
        owner = _make_user(session, "mismatch@example.com")
        conversation_a = _make_conversation(session, owner.id, title="A")
        conversation_b = _make_conversation(session, owner.id, title="B")
        message = _make_message(session, owner.id, conversation_a.id)
        _set_rls_user(session, owner.id)

        get_or_create_message_source_unit(session, _locator(owner.id, message, conversation_a.id))
        session.commit()

        # Same message_id, but a caller now claims it belongs to a different conversation.
        bad_locator = MessageSourceLocator(
            owner_id=owner.id,
            message_id=message.id,
            conversation_id=conversation_b.id,
            role=message.role,
            observed_at=message.created_at,
            content_text=message.content,
        )
        with pytest.raises(MessageSourceIdentityConflict):
            get_or_create_message_source_unit(session, bad_locator)
    finally:
        session.rollback()
        session.close()


def test_get_or_create_message_source_unit_detects_content_hash_mismatch():
    session = SessionLocal()
    try:
        owner = _make_user(session, "hash-mismatch@example.com")
        conversation = _make_conversation(session, owner.id)
        message = _make_message(session, owner.id, conversation.id, content="Originaltext.")
        _set_rls_user(session, owner.id)

        get_or_create_message_source_unit(session, _locator(owner.id, message, conversation.id))
        session.commit()

        stale_locator = _locator(owner.id, message, conversation.id, content_text="Annan text an originalet.")
        with pytest.raises(MessageSourceIdentityConflict):
            get_or_create_message_source_unit(session, stale_locator)
    finally:
        session.rollback()
        session.close()


# --- DB-level trigger enforcement ----------------------------------------------------------


def test_trigger_rejects_source_role_not_matching_message_role():
    """trg_msgsu_validate_fields must independently re-verify the role mapping — a caller
    bypassing app/rag/message_source.py entirely (raw INSERT with a wrong source_role) must
    still be rejected at commit, exactly like document_source_units' founder-role test."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "trigger-role-mismatch@example.com")
        conversation = _make_conversation(session, owner.id)
        message = _make_message(session, owner.id, conversation.id, role=MessageRole.user)
        _set_rls_user(session, owner.id)

        content_hash, hash_version = compute_content_hash(message.content)
        msu = MemorySourceUnit(
            owner_id=owner.id,
            source_kind=SourceKind.message,
            source_identity_key=f"message:{message.id}",
            source_role=SourceRole.assistant,  # WRONG: message.role is 'user' -> must be founder
            observed_at=message.created_at,
            content_text=message.content,
            content_hash=content_hash,
            content_hash_version=hash_version,
            snapshot_status=SnapshotStatus.exact,
        )
        session.add(msu)
        session.flush()
        session.add(MessageSourceUnit(memory_source_id=msu.id, owner_id=owner.id, source_kind=SourceKind.message, message_id=message.id, conversation_id=conversation.id))
        with pytest.raises((IntegrityError, DBAPIError), match="source_role must be"):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_trigger_rejects_conversation_id_not_matching_message():
    session = SessionLocal()
    try:
        owner = _make_user(session, "trigger-conv-mismatch@example.com")
        conversation_a = _make_conversation(session, owner.id, title="A")
        conversation_b = _make_conversation(session, owner.id, title="B")
        message = _make_message(session, owner.id, conversation_a.id)
        _set_rls_user(session, owner.id)

        content_hash, hash_version = compute_content_hash(message.content)
        msu = MemorySourceUnit(
            owner_id=owner.id,
            source_kind=SourceKind.message,
            source_identity_key=f"message:{message.id}",
            source_role=SourceRole.founder,
            observed_at=message.created_at,
            content_text=message.content,
            content_hash=content_hash,
            content_hash_version=hash_version,
            snapshot_status=SnapshotStatus.exact,
        )
        session.add(msu)
        session.flush()
        session.add(MessageSourceUnit(memory_source_id=msu.id, owner_id=owner.id, source_kind=SourceKind.message, message_id=message.id, conversation_id=conversation_b.id))
        with pytest.raises((IntegrityError, DBAPIError), match="does not match message"):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_trigger_rejects_exact_snapshot_content_text_mismatch():
    session = SessionLocal()
    try:
        owner = _make_user(session, "trigger-content-mismatch@example.com")
        conversation = _make_conversation(session, owner.id)
        message = _make_message(session, owner.id, conversation.id, content="Riktigt innehall.")
        _set_rls_user(session, owner.id)

        wrong_hash, hash_version = compute_content_hash("Fel innehall som inte matchar.")
        msu = MemorySourceUnit(
            owner_id=owner.id,
            source_kind=SourceKind.message,
            source_identity_key=f"message:{message.id}",
            source_role=SourceRole.founder,
            observed_at=message.created_at,
            content_text="Fel innehall som inte matchar.",
            content_hash=wrong_hash,
            content_hash_version=hash_version,
            snapshot_status=SnapshotStatus.exact,
        )
        session.add(msu)
        session.flush()
        session.add(MessageSourceUnit(memory_source_id=msu.id, owner_id=owner.id, source_kind=SourceKind.message, message_id=message.id, conversation_id=conversation.id))
        with pytest.raises((IntegrityError, DBAPIError), match="content_text does not match"):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_direct_update_of_message_source_unit_fields_rejected():
    session = SessionLocal()
    try:
        owner = _make_user(session, "immutable-update@example.com")
        conversation = _make_conversation(session, owner.id)
        message = _make_message(session, owner.id, conversation.id)
        _set_rls_user(session, owner.id)

        msu_id = get_or_create_message_source_unit(session, _locator(owner.id, message, conversation.id))
        session.commit()

        other_message = _make_message(session, owner.id, conversation.id, content="Ett annat meddelande.")
        msgsu = session.get(MessageSourceUnit, msu_id)
        msgsu.message_id = other_message.id
        # No match= regex, mirroring test_memory_source_units.py's identical direct-update
        # tests: depending on test execution order, mainai_app's privileges on this table may
        # already be narrowed to SELECT+INSERT by an earlier module's own privilege-policy
        # fixture, in which case Postgres rejects the UPDATE with "permission denied" before
        # the guard trigger ever runs -- an equally valid rejection, just a different layer.
        with pytest.raises((IntegrityError, DBAPIError)):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_direct_delete_of_message_source_unit_rejected_outside_erasure():
    session = SessionLocal()
    try:
        owner = _make_user(session, "immutable-delete@example.com")
        conversation = _make_conversation(session, owner.id)
        message = _make_message(session, owner.id, conversation.id)
        _set_rls_user(session, owner.id)

        msu_id = get_or_create_message_source_unit(session, _locator(owner.id, message, conversation.id))
        session.commit()

        msgsu = session.get(MessageSourceUnit, msu_id)
        session.delete(msgsu)
        # No match= regex -- see test_direct_update_of_message_source_unit_fields_rejected's
        # identical comment: a narrowed-privilege ordering may reject this with "permission
        # denied" before the guard trigger runs, which is an equally valid rejection.
        with pytest.raises((IntegrityError, DBAPIError)):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_exact_one_subtype_required_for_message_source_kind():
    """S1A's exclusive-arc guarantee (migration 0019's deferred constraint trigger) applies
    identically to the 'message' arm: a memory_source_units row with source_kind='message' but
    no matching message_source_units row must be rejected at commit."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "exclusive-arc@example.com")
        conversation = _make_conversation(session, owner.id)
        message = _make_message(session, owner.id, conversation.id)
        _set_rls_user(session, owner.id)

        content_hash, hash_version = compute_content_hash(message.content)
        msu = MemorySourceUnit(
            owner_id=owner.id,
            source_kind=SourceKind.message,
            source_identity_key=f"message:{message.id}",
            source_role=SourceRole.founder,
            observed_at=message.created_at,
            content_text=message.content,
            content_hash=content_hash,
            content_hash_version=hash_version,
            snapshot_status=SnapshotStatus.exact,
        )
        session.add(msu)
        # No MessageSourceUnit row added -- the exclusive-arc deferred trigger must fire.
        with pytest.raises((IntegrityError, DBAPIError)):
            session.commit()
    finally:
        session.rollback()
        session.close()


# --- RLS isolation -----------------------------------------------------------------------


def test_rls_owner_isolation_on_message_source_units():
    session = SessionLocal()
    try:
        owner_a = _make_user(session, "rls-a@example.com")
        owner_b = _make_user(session, "rls-b@example.com")
        conversation_a = _make_conversation(session, owner_a.id)
        message_a = _make_message(session, owner_a.id, conversation_a.id)
        _set_rls_user(session, owner_a.id)
        get_or_create_message_source_unit(session, _locator(owner_a.id, message_a, conversation_a.id))
        session.commit()

        _set_rls_user(session, owner_b.id)
        count = session.execute(sa_text("SELECT count(*) FROM message_source_units")).scalar()
        assert count == 0
    finally:
        session.rollback()
        session.close()


def test_rls_default_deny_without_current_user_id():
    session = SessionLocal()
    try:
        owner = _make_user(session, "rls-default-deny@example.com")
        conversation = _make_conversation(session, owner.id)
        message = _make_message(session, owner.id, conversation.id)
        _set_rls_user(session, owner.id)
        get_or_create_message_source_unit(session, _locator(owner.id, message, conversation.id))
        session.commit()

        current_user_id_var.set(None)
        session.execute(sa_text("RESET app.current_user_id"))
        count = session.execute(sa_text("SELECT count(*) FROM message_source_units")).scalar()
        assert count == 0
    finally:
        session.rollback()
        session.close()


# --- backfill: founder's exact required scenario chain --------------------------------------
#
# "upload corpus -> originals lasta -> allt inventoryat -> inget tappas -> crash/restart
# fortsatter -> duplicates raknas -> parser failures syns -> unsupported sparas -> exact N/N
# completeness bevisas -> ingen AI behovs for detta."
#
# For message ingestion specifically, "allt inventoryat / inget tappas / crash-restart
# fortsatter" is what this section proves: every existing message ends up with exactly one
# message_source_units row, a bounded/interrupted run picks up exactly where it left off next
# time, and no provider/AI call is ever made.


def test_backfill_creates_source_unit_for_every_existing_message_nothing_lost():
    session = SessionLocal()
    try:
        owner = _make_user(session, "backfill-nothing-lost@example.com")
        conversation = _make_conversation(session, owner.id)
        for i in range(5):
            _make_message(session, owner.id, conversation.id, content=f"Meddelande {i}")
        _set_rls_user(session, owner.id)

        assert count_messages_without_source_unit(session, owner.id) == 5

        result = backfill_message_source_units(session, owner.id)

        assert result.processed == 5
        assert result.created == 5
        assert count_messages_without_source_unit(session, owner.id) == 0
        total = session.execute(
            sa_text("SELECT count(*) FROM message_source_units WHERE owner_id = :oid"), {"oid": str(owner.id)}
        ).scalar()
        assert total == 5
    finally:
        session.rollback()
        session.close()


def test_backfill_rerun_is_idempotent_no_duplicate_and_no_new_work():
    session = SessionLocal()
    try:
        owner = _make_user(session, "backfill-idempotent@example.com")
        conversation = _make_conversation(session, owner.id)
        _make_message(session, owner.id, conversation.id, content="Enda meddelandet.")
        _set_rls_user(session, owner.id)

        first = backfill_message_source_units(session, owner.id)
        assert first.created == 1

        second = backfill_message_source_units(session, owner.id)
        assert second.processed == 0
        assert second.created == 0

        total = session.execute(
            sa_text("SELECT count(*) FROM message_source_units WHERE owner_id = :oid"), {"oid": str(owner.id)}
        ).scalar()
        assert total == 1
    finally:
        session.rollback()
        session.close()


def test_backfill_crash_between_batches_resumes_without_losing_or_duplicating():
    """The founder's exact "crash/restart fortsatter" requirement: a run that only gets through
    part of the candidates (simulated here via a small batch_size/max_batches cap, exactly like
    app/rag/backfill/memory_source.py's own bounded-batch discipline) must leave the remaining
    messages as valid candidates for the NEXT call — no message silently skipped, none double-
    counted."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "backfill-crash-restart@example.com")
        conversation = _make_conversation(session, owner.id)
        for i in range(7):
            _make_message(session, owner.id, conversation.id, content=f"Restart-meddelande {i}")
        _set_rls_user(session, owner.id)

        # Simulated crash/restart: only ONE bounded batch of 3 completes before "the process
        # dies" (in the test, this is simply not calling again yet) -- 4 candidates remain.
        first = backfill_message_source_units(session, owner.id, batch_size=3, max_batches=1)
        assert first.created == 3
        assert count_messages_without_source_unit(session, owner.id) == 4

        # "Restart": a fresh call, no special resume argument needed -- the candidate query
        # (message has no message_source_units row yet) IS the resume marker, identical to
        # app/rag/backfill/message_sequence.py's own reasoning.
        second = backfill_message_source_units(session, owner.id, batch_size=3, max_batches=10)
        assert second.created == 4
        assert count_messages_without_source_unit(session, owner.id) == 0

        total = session.execute(
            sa_text("SELECT count(*) FROM message_source_units WHERE owner_id = :oid"), {"oid": str(owner.id)}
        ).scalar()
        assert total == 7  # nothing lost, nothing duplicated across the two runs
    finally:
        session.rollback()
        session.close()


def test_backfill_two_concurrent_workers_converge_without_duplicates():
    setup_session = SessionLocal()
    try:
        owner = _make_user(setup_session, "backfill-concurrent@example.com")
        conversation = _make_conversation(setup_session, owner.id)
        for i in range(6):
            _make_message(setup_session, owner.id, conversation.id, content=f"Concurrent-meddelande {i}")
        owner_id = owner.id
    finally:
        setup_session.close()

    results: dict = {}
    errors: dict = {}

    def _worker(name):
        session = SessionLocal()
        try:
            _set_rls_user(session, owner_id)
            results[name] = backfill_message_source_units(session, owner_id, batch_size=10)
        except Exception as exc:  # noqa: BLE001
            errors[name] = exc
        finally:
            session.close()

    thread_a = threading.Thread(target=_worker, args=("a",))
    thread_b = threading.Thread(target=_worker, args=("b",))
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    assert not thread_a.is_alive() and not thread_b.is_alive()
    assert not errors, f"a worker raised unexpectedly: {errors}"

    verify_session = SessionLocal()
    try:
        _set_rls_user(verify_session, owner_id)
        total = verify_session.execute(
            sa_text("SELECT count(*) FROM message_source_units WHERE owner_id = :oid"), {"oid": str(owner_id)}
        ).scalar()
        assert total == 6, "no duplicate message_source_units rows despite two concurrent backfill workers"
    finally:
        verify_session.close()


def test_backfill_candidate_ids_scoped_to_owner_and_ordered():
    session = SessionLocal()
    try:
        owner_a = _make_user(session, "candidates-a@example.com")
        owner_b = _make_user(session, "candidates-b@example.com")
        conversation_a = _make_conversation(session, owner_a.id)
        conversation_b = _make_conversation(session, owner_b.id)
        message_a = _make_message(session, owner_a.id, conversation_a.id)
        _make_message(session, owner_b.id, conversation_b.id)
        _set_rls_user(session, owner_a.id)

        ids = candidate_message_ids(session, owner_a.id, limit=10)
        assert ids == [message_a.id]
    finally:
        session.rollback()
        session.close()


def test_backfill_never_calls_any_provider(monkeypatch):
    from app.providers.openai_provider import OpenAIProvider

    async def _fail_if_called(self, messages, model, **kwargs):
        raise AssertionError("message-source backfill must never call a provider — no AI needed for this")

    monkeypatch.setattr(OpenAIProvider, "chat", _fail_if_called)

    session = SessionLocal()
    try:
        owner = _make_user(session, "no-provider@example.com")
        conversation = _make_conversation(session, owner.id)
        _make_message(session, owner.id, conversation.id)
        _set_rls_user(session, owner.id)

        result = backfill_message_source_units(session, owner.id)
        assert result.created == 1
    finally:
        session.rollback()
        session.close()


def test_backfill_fence_failure_rolls_back_the_in_flight_batch():
    """The job fence runs after inserts but before their commit. A lease loss at that exact
    point must leave no source rows behind; checking only before a batch would leave a stale
    worker window during the batch itself."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "backfill-mid-batch-fence@example.com")
        _set_rls_user(session, owner.id)
        conversation = Conversation(user_id=owner.id, title="Fence")
        session.add(conversation)
        session.commit()
        session.add(Message(conversation_id=conversation.id, role=MessageRole.user, content="Uncommitted"))
        session.commit()

        def _lose_lease_before_commit():
            raise RuntimeError("simulated lease reclaim")

        with pytest.raises(RuntimeError, match="simulated lease reclaim"):
            backfill_message_source_units(
                session,
                owner.id,
                max_batches=1,
                before_batch_commit=_lose_lease_before_commit,
            )
        session.rollback()
        assert count_messages_without_source_unit(session, owner.id) == 1
    finally:
        session.rollback()
        session.close()
