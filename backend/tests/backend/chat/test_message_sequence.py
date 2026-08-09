"""S1B — `messages.sequence_number` (migration 0030, app/models/conversation.py,
app/rag/backfill/message_sequence.py, app/jobs/handlers/message_sequence_backfill.py).
See docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.9 and §8's S1B line.

Covers, in order:
  A. The assignment trigger: every new message is numbered, per conversation, from 1, without
     application code doing anything.
  B. The assignment FORMULA's invariant — the whole reason it is
     `GREATEST(max, count) + 1` and not `max + 1`: an insert into a not-yet-backfilled
     conversation must be numbered ABOVE the range the backfill will later hand out.
  C. Real concurrency: two simultaneous inserts into the same conversation, two real sessions,
     never produce a duplicate ordinal.
  D. Immutability: an assigned ordinal cannot be changed, cleared, or moved to another
     conversation. NULL -> value (the backfill's one legitimate transition) is allowed.
  E. Constraints and uniqueness at the database level, not just by convention.
  F. The backfill: deterministic `(created_at, id)` numbering, restart-safety, per-conversation
     atomicity, the fail-closed conflict path, batch bounds, and cross-owner isolation.
  G. The durable `message_sequence_backfill` job end to end on the real worker dispatch path:
     capability gating without any AI provider configured, progress, cancellation, resume,
     lease fencing, and a truthful completion message.
  H. Live chat inserts and the backfill agreeing with each other.

Real local Postgres including RLS and the real Alembic-built schema, matching this repo's
convention — the triggers under test only exist in the database, so a faked DB would test
nothing at all here.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.jobs import service
from app.jobs.handlers.message_sequence_backfill import (
    MESSAGE_SEQUENCE_BACKFILL_JOB_TYPE,
    run_message_sequence_backfill_job,
)
from app.mainai_runtime_contract import (
    CAPABILITY_MANIFEST,
    CapabilityUnavailableError,
    get_capability_status,
    require_capability,
)
from app.models.conversation import Conversation, Message, MessageRole
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.rag.backfill.message_sequence import (
    MESSAGE_SEQUENCE_ADVISORY_LOCK_NAMESPACE,
    backfill_conversation,
    backfill_message_sequence_numbers,
    candidate_conversation_ids,
    count_conversations_with_unsequenced_messages,
    count_unsequenced_messages,
)
from app.request_context import current_user_id as current_user_id_var


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_conversation(session, owner_id, *, title="Samtal", created_at=None) -> Conversation:
    _set_rls_user(session, owner_id)
    conversation = Conversation(user_id=owner_id, title=title)
    if created_at is not None:
        conversation.created_at = created_at
    session.add(conversation)
    session.commit()
    return conversation


def _add_message(session, conversation_id, *, content="hej", role=MessageRole.user, created_at=None) -> Message:
    message = Message(conversation_id=conversation_id, role=role, content=content)
    if created_at is not None:
        message.created_at = created_at
    session.add(message)
    session.commit()
    return message


def _insert_unsequenced(superuser_db, conversation_id, *, content, created_at, message_id=None) -> uuid.UUID:
    """Writes a message row the way one existed BEFORE migration 0030 — no ordinal at all.

    The trigger would otherwise assign one, so it is disabled for exactly this statement via
    `SET LOCAL session_replication_role = replica` on the superuser connection. `SET LOCAL`
    (not a plain `SET`) is deliberate: it is scoped to this one transaction and reverts at the
    COMMIT below no matter what happens in between, so a failure mid-helper can never return a
    trigger-disabled connection to SQLAlchemy's pool for some unrelated later test to inherit.
    This is the only honest way to reproduce real pre-migration history in a database where the
    migration has already run; nothing in the application ever does this, and every test that
    uses it asserts on the backfill's handling of the result."""
    message_id = message_id or uuid.uuid4()
    superuser_db.execute(sa_text("SET LOCAL session_replication_role = replica"))
    superuser_db.execute(
        sa_text(
            "INSERT INTO messages (id, conversation_id, role, content, status, created_at, sequence_number) "
            "VALUES (:id, :cid, 'user', :content, 'succeeded', :created_at, NULL)"
        ),
        {"id": str(message_id), "cid": str(conversation_id), "content": content, "created_at": created_at},
    )
    superuser_db.commit()
    return message_id


def _sequences(superuser_db, conversation_id) -> list[tuple[str, int | None]]:
    rows = superuser_db.execute(
        sa_text("SELECT content, sequence_number FROM messages WHERE conversation_id = :cid ORDER BY sequence_number NULLS LAST, created_at, id"),
        {"cid": str(conversation_id)},
    ).all()
    return [(r.content, r.sequence_number) for r in rows]


# --- A: the assignment trigger ----------------------------------------------------------------


def test_new_message_is_numbered_by_the_database_without_application_code(db_session, make_verified_user):
    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)

    first = _add_message(db_session, conversation.id, content="ett")
    second = _add_message(db_session, conversation.id, content="två")

    # No caller ever set sequence_number — and no explicit refresh is needed either, because
    # the model declares it FetchedValue() so SQLAlchemy reads the trigger's result back.
    assert first.sequence_number == 1
    assert second.sequence_number == 2


def test_numbering_restarts_at_one_per_conversation(db_session, make_verified_user):
    owner, _ = make_verified_user()
    a = _make_conversation(db_session, owner.id, title="A")
    b = _make_conversation(db_session, owner.id, title="B")

    assert _add_message(db_session, a.id, content="a1").sequence_number == 1
    assert _add_message(db_session, b.id, content="b1").sequence_number == 1
    assert _add_message(db_session, a.id, content="a2").sequence_number == 2
    assert _add_message(db_session, b.id, content="b2").sequence_number == 2


def test_an_explicitly_supplied_sequence_number_is_respected_not_overwritten(db_session, make_verified_user):
    """The trigger only fills in a NULL — it never overrides a caller that genuinely knows the
    ordinal (which is what makes the backfill's own UPDATE path possible at all)."""
    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)

    explicit = Message(conversation_id=conversation.id, role=MessageRole.user, content="explicit", sequence_number=7)
    db_session.add(explicit)
    db_session.commit()
    db_session.refresh(explicit)

    assert explicit.sequence_number == 7
    # ...and the next auto-assigned one lands above it, never colliding.
    assert _add_message(db_session, conversation.id, content="efter").sequence_number == 8


def test_a_deleted_message_never_causes_its_ordinal_to_be_reused(db_session, superuser_db, make_verified_user):
    """Gaps are allowed; reuse is not. This is the case the `max` half of the formula exists
    for — a `count(*)`-only rule would hand out 3 again after deleting the middle row."""
    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)
    _add_message(db_session, conversation.id, content="ett")
    middle = _add_message(db_session, conversation.id, content="två")
    _add_message(db_session, conversation.id, content="tre")

    superuser_db.execute(sa_text("DELETE FROM messages WHERE id = :id"), {"id": str(middle.id)})
    superuser_db.commit()

    assert _add_message(db_session, conversation.id, content="fyra").sequence_number == 4


# --- B: the formula's invariant (why GREATEST(max, count) + 1) --------------------------------


def test_insert_into_an_unbackfilled_conversation_is_numbered_above_the_backfill_range(
    db_session, superuser_db, make_verified_user
):
    """The exact window migration 0030 deliberately opens: three pre-0030 rows carry no
    ordinal, and a new message arrives before the backfill has run. `max + 1` would number it
    1 and make the backfill impossible without renumbering; the count term makes it 4."""
    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)
    base = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(3):
        _insert_unsequenced(superuser_db, conversation.id, content=f"historisk-{i}", created_at=base + timedelta(minutes=i))

    live = _add_message(db_session, conversation.id, content="ny")
    assert live.sequence_number == 4

    _set_rls_user(db_session, owner.id)
    outcome = backfill_conversation(db_session, conversation.id)
    assert outcome.status == "numbered"
    assert outcome.assigned == 3

    assert _sequences(superuser_db, conversation.id) == [
        ("historisk-0", 1),
        ("historisk-1", 2),
        ("historisk-2", 3),
        ("ny", 4),
    ]


def test_repeated_inserts_during_the_unbackfilled_window_all_stay_above_the_range(
    db_session, superuser_db, make_verified_user
):
    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)
    base = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(5):
        _insert_unsequenced(superuser_db, conversation.id, content=f"h{i}", created_at=base + timedelta(minutes=i))

    live = [_add_message(db_session, conversation.id, content=f"live-{i}").sequence_number for i in range(3)]
    assert live == [6, 7, 8]

    _set_rls_user(db_session, owner.id)
    assert backfill_conversation(db_session, conversation.id).assigned == 5
    numbers = sorted(n for _c, n in _sequences(superuser_db, conversation.id))
    assert numbers == [1, 2, 3, 4, 5, 6, 7, 8], "no collision, no gap, no renumbering"


# --- C: real concurrency ----------------------------------------------------------------------


def test_two_concurrent_inserts_into_one_conversation_never_share_an_ordinal(db_session, superuser_db, make_verified_user):
    """Two real threads, two real sessions, one conversation. Without the advisory lock both
    would read the same max/count and compute the same next value.

    Every escape hatch here is bounded on purpose: `statement_timeout` so a thread that somehow
    blocks on the lock forever raises instead of waiting, `daemon=True` so even a wedged thread
    can never keep the pytest process (and with it a whole CI job) from exiting, and a
    `t.is_alive()` assertion so "the thread never finished" fails loudly rather than passing
    quietly because the row it was supposed to insert simply isn't there yet."""
    import threading

    from app.db import SessionLocal

    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)

    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def _insert(label: str) -> None:
        session = SessionLocal()
        try:
            _set_rls_user(session, owner.id)
            session.execute(sa_text("SET LOCAL statement_timeout = '20s'"))
            session.add(Message(conversation_id=conversation.id, role=MessageRole.user, content=label))
            barrier.wait(timeout=10)
            session.commit()
        except Exception as exc:  # noqa: BLE001 - re-raised through `errors` below
            errors.append(exc)
            session.rollback()
        finally:
            session.close()

    threads = [threading.Thread(target=_insert, args=(f"t{i}",), daemon=True) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert all(not t.is_alive() for t in threads), "a racing insert never finished — possible deadlock"
    assert not errors, errors
    numbers = sorted(n for _c, n in _sequences(superuser_db, conversation.id))
    assert numbers == [1, 2]


def test_an_insert_actually_waits_on_the_per_conversation_advisory_lock(db_session, superuser_db, make_verified_user):
    """Deterministic proof (not a timing race) that the trigger really acquires the lock: hold
    the exact same advisory key open in one transaction, then show a plain INSERT into that
    conversation blocks until `statement_timeout` fires. Without the lock in the trigger this
    INSERT would simply succeed, so this test fails the moment the lock is removed."""
    from sqlalchemy.orm import sessionmaker

    from app.db import SessionLocal, migration_engine

    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)

    holder = sessionmaker(bind=migration_engine)()
    try:
        holder.execute(
            sa_text("SELECT pg_advisory_xact_lock(:ns, hashtext(:cid))"),
            {"ns": MESSAGE_SEQUENCE_ADVISORY_LOCK_NAMESPACE, "cid": str(conversation.id)},
        )  # deliberately NOT committed — the lock is held for this transaction's lifetime

        blocked = SessionLocal()
        try:
            _set_rls_user(blocked, owner.id)
            blocked.execute(sa_text("SET LOCAL statement_timeout = '1500ms'"))
            blocked.add(Message(conversation_id=conversation.id, role=MessageRole.user, content="blockerad"))
            with pytest.raises(DBAPIError) as exc:
                blocked.commit()
            assert "statement timeout" in str(exc.value).lower()
            blocked.rollback()
        finally:
            blocked.close()

        # A different conversation hashes to a different key and must NOT be blocked.
        other = _make_conversation(db_session, owner.id, title="annan")
        free = SessionLocal()
        try:
            _set_rls_user(free, owner.id)
            free.execute(sa_text("SET LOCAL statement_timeout = '5s'"))
            message = Message(conversation_id=other.id, role=MessageRole.user, content="fri")
            free.add(message)
            free.commit()
            free.refresh(message)
            assert message.sequence_number == 1
        finally:
            free.close()
    finally:
        holder.rollback()
        holder.close()


# --- D: immutability --------------------------------------------------------------------------


def test_an_assigned_sequence_number_cannot_be_changed(db_session, make_verified_user):
    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)
    message = _add_message(db_session, conversation.id, content="ett")

    with pytest.raises(DBAPIError) as exc:
        db_session.execute(
            sa_text("UPDATE messages SET sequence_number = 99 WHERE id = :id"), {"id": str(message.id)}
        )
    assert "immutable once assigned" in str(exc.value)
    db_session.rollback()


def test_an_assigned_sequence_number_cannot_be_cleared_back_to_null(db_session, make_verified_user):
    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)
    message = _add_message(db_session, conversation.id, content="ett")

    with pytest.raises(DBAPIError) as exc:
        db_session.execute(
            sa_text("UPDATE messages SET sequence_number = NULL WHERE id = :id"), {"id": str(message.id)}
        )
    assert "immutable once assigned" in str(exc.value)
    db_session.rollback()


def test_null_to_a_value_is_the_one_allowed_transition(db_session, superuser_db, make_verified_user):
    """The backfill's own transition — everything else about this trigger would be useless if
    it also blocked the one write the backfill has to make."""
    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)
    message_id = _insert_unsequenced(superuser_db, conversation.id, content="historisk", created_at=datetime(2026, 1, 1))

    _set_rls_user(db_session, owner.id)
    db_session.execute(sa_text("UPDATE messages SET sequence_number = 1 WHERE id = :id"), {"id": str(message_id)})
    db_session.commit()

    assert _sequences(superuser_db, conversation.id) == [("historisk", 1)]


def test_a_message_cannot_be_moved_to_another_conversation(db_session, make_verified_user):
    """An ordinal means "position within THIS conversation" — moving the row would silently
    make it mean something else."""
    owner, _ = make_verified_user()
    a = _make_conversation(db_session, owner.id, title="A")
    b = _make_conversation(db_session, owner.id, title="B")
    message = _add_message(db_session, a.id, content="ett")

    with pytest.raises(DBAPIError) as exc:
        db_session.execute(
            sa_text("UPDATE messages SET conversation_id = :b WHERE id = :id"),
            {"b": str(b.id), "id": str(message.id)},
        )
    assert "conversation_id is immutable" in str(exc.value)
    db_session.rollback()


def test_an_unrelated_update_to_a_message_still_works(db_session, make_verified_user):
    """The immutability trigger must not have turned every UPDATE into an error — app/routers/
    chat.py's retry path genuinely rewrites an assistant row's content/status in place."""
    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)
    message = _add_message(db_session, conversation.id, content="ett", role=MessageRole.assistant)

    message.content = "omskrivet"
    db_session.commit()
    db_session.refresh(message)
    assert message.content == "omskrivet"
    assert message.sequence_number == 1


# --- E: database-level constraints -------------------------------------------------------------


def test_duplicate_sequence_number_in_one_conversation_is_rejected_by_the_database(db_session, make_verified_user):
    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)
    _add_message(db_session, conversation.id, content="ett")

    duplicate = Message(conversation_id=conversation.id, role=MessageRole.user, content="dubblett", sequence_number=1)
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_a_zero_or_negative_sequence_number_is_rejected(db_session, make_verified_user):
    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)

    db_session.add(Message(conversation_id=conversation.id, role=MessageRole.user, content="noll", sequence_number=0))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_the_same_ordinal_in_two_different_conversations_is_fine(db_session, make_verified_user):
    owner, _ = make_verified_user()
    a = _make_conversation(db_session, owner.id, title="A")
    b = _make_conversation(db_session, owner.id, title="B")
    assert _add_message(db_session, a.id, content="a").sequence_number == 1
    assert _add_message(db_session, b.id, content="b").sequence_number == 1


# --- F: the backfill ---------------------------------------------------------------------------


def test_backfill_numbers_historical_messages_by_created_at_then_id(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)
    base = datetime(2026, 2, 1, 9, 0, 0)
    # Inserted out of chronological order on purpose — the ORDER of insertion must not matter.
    _insert_unsequenced(superuser_db, conversation.id, content="tredje", created_at=base + timedelta(minutes=2))
    _insert_unsequenced(superuser_db, conversation.id, content="första", created_at=base)
    _insert_unsequenced(superuser_db, conversation.id, content="andra", created_at=base + timedelta(minutes=1))

    _set_rls_user(db_session, owner.id)
    result = backfill_message_sequence_numbers(db_session, owner.id)

    assert result.conversations_numbered == 1
    assert result.messages_assigned == 3
    assert result.remaining_conversations == 0
    assert _sequences(superuser_db, conversation.id) == [("första", 1), ("andra", 2), ("tredje", 3)]


def test_backfill_breaks_created_at_ties_by_id_deterministically(db_session, superuser_db, make_verified_user):
    """The exact failure S1B exists to fix: identical timestamps. The resulting order is
    arbitrary but must be REPRODUCIBLE — sorted by id, not by whatever the planner returns."""
    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)
    same_instant = datetime(2026, 2, 1, 9, 0, 0)
    ids = sorted(uuid.uuid4() for _ in range(4))
    for index, message_id in enumerate(reversed(ids)):  # insert in reverse id order
        _insert_unsequenced(superuser_db, conversation.id, content=f"m{index}", created_at=same_instant, message_id=message_id)

    _set_rls_user(db_session, owner.id)
    backfill_conversation(db_session, conversation.id)

    rows = superuser_db.execute(
        sa_text("SELECT id, sequence_number FROM messages WHERE conversation_id = :cid ORDER BY sequence_number"),
        {"cid": str(conversation.id)},
    ).all()
    assert [r.id for r in rows] == ids, "ties must be ordered by id, ascending"


def test_backfill_is_idempotent_and_restart_safe(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)
    base = datetime(2026, 2, 1, 9, 0, 0)
    for i in range(3):
        _insert_unsequenced(superuser_db, conversation.id, content=f"m{i}", created_at=base + timedelta(minutes=i))

    _set_rls_user(db_session, owner.id)
    first = backfill_message_sequence_numbers(db_session, owner.id)
    before = _sequences(superuser_db, conversation.id)

    second = backfill_message_sequence_numbers(db_session, owner.id)

    assert first.messages_assigned == 3
    assert second.messages_assigned == 0, "a second run must find nothing left to do"
    assert second.conversations_numbered == 0
    assert _sequences(superuser_db, conversation.id) == before, "a re-run must never renumber anything"


def test_backfill_is_atomic_per_conversation(db_session, superuser_db, make_verified_user):
    """A callback that raises must leave the conversation completely unnumbered — never half
    of it — because the numbering and the callback share one commit."""
    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)
    base = datetime(2026, 2, 1, 9, 0, 0)
    for i in range(4):
        _insert_unsequenced(superuser_db, conversation.id, content=f"m{i}", created_at=base + timedelta(minutes=i))

    _set_rls_user(db_session, owner.id)

    def _boom(_outcome):
        raise RuntimeError("simulated crash between the numbering and the run report")

    with pytest.raises(RuntimeError):
        backfill_conversation(db_session, conversation.id, on_outcome=_boom)
    db_session.rollback()

    assert all(n is None for _c, n in _sequences(superuser_db, conversation.id)), (
        "the numbering must roll back with the callback that failed"
    )
    assert count_unsequenced_messages(db_session, owner.id) == 4


def test_backfill_refuses_a_conflicting_conversation_without_writing_anything(db_session, superuser_db, make_verified_user):
    """The fail-closed path: an existing ordinal sitting inside the 1..N range this run would
    assign. Unreachable while migration 0030's trigger is in place — checked anyway, and the
    conversation must come out completely untouched, not partly numbered."""
    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)
    base = datetime(2026, 2, 1, 9, 0, 0)
    for i in range(3):
        _insert_unsequenced(superuser_db, conversation.id, content=f"h{i}", created_at=base + timedelta(minutes=i))
    # An ordinal of 2 with three unnumbered rows: 1..3 would collide.
    superuser_db.execute(sa_text("SET LOCAL session_replication_role = replica"))
    superuser_db.execute(
        sa_text(
            "INSERT INTO messages (id, conversation_id, role, content, status, created_at, sequence_number) "
            "VALUES (:id, :cid, 'user', 'kolliderande', 'succeeded', :created_at, 2)"
        ),
        {"id": str(uuid.uuid4()), "cid": str(conversation.id), "created_at": base + timedelta(hours=1)},
    )
    superuser_db.commit()

    _set_rls_user(db_session, owner.id)
    result = backfill_message_sequence_numbers(db_session, owner.id)

    assert result.conversations_conflicted == 1
    assert result.conversations_numbered == 0
    assert result.messages_assigned == 0
    assert result.outcomes[0].status == "conflict"
    assert "refusing to renumber or collide" in result.outcomes[0].reason
    unnumbered = [c for c, n in _sequences(superuser_db, conversation.id) if n is None]
    assert sorted(unnumbered) == ["h0", "h1", "h2"], "nothing may have been written"
    # ...and it is still honestly reported as outstanding work, not hidden.
    assert result.remaining_conversations == 1


def test_backfill_never_touches_another_owners_conversation(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    other, _ = make_verified_user()
    mine = _make_conversation(db_session, owner.id, title="min")
    theirs = _make_conversation(db_session, other.id, title="deras")
    base = datetime(2026, 2, 1, 9, 0, 0)
    _insert_unsequenced(superuser_db, mine.id, content="min-1", created_at=base)
    _insert_unsequenced(superuser_db, theirs.id, content="deras-1", created_at=base)

    _set_rls_user(db_session, owner.id)
    result = backfill_message_sequence_numbers(db_session, owner.id)

    assert result.conversations_numbered == 1
    assert _sequences(superuser_db, mine.id) == [("min-1", 1)]
    assert _sequences(superuser_db, theirs.id) == [("deras-1", None)], "another owner's history must be untouched"
    assert count_unsequenced_messages(db_session, owner.id) == 0
    assert candidate_conversation_ids(db_session, owner.id, 10) == []


def test_candidate_list_and_counts_are_owner_scoped(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    other, _ = make_verified_user()
    mine = _make_conversation(db_session, owner.id, title="min")
    theirs = _make_conversation(db_session, other.id, title="deras")
    base = datetime(2026, 2, 1, 9, 0, 0)
    _insert_unsequenced(superuser_db, mine.id, content="a", created_at=base)
    _insert_unsequenced(superuser_db, mine.id, content="b", created_at=base + timedelta(minutes=1))
    _insert_unsequenced(superuser_db, theirs.id, content="c", created_at=base)

    _set_rls_user(db_session, owner.id)
    assert count_unsequenced_messages(db_session, owner.id) == 2
    assert count_conversations_with_unsequenced_messages(db_session, owner.id) == 1
    assert candidate_conversation_ids(db_session, owner.id, 10) == [mine.id]


def test_backfill_batch_size_is_bounded_and_reports_what_remains(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    base = datetime(2026, 2, 1, 9, 0, 0)
    conversations = []
    for i in range(4):
        conversation = _make_conversation(db_session, owner.id, title=f"c{i}", created_at=base + timedelta(days=i))
        _insert_unsequenced(superuser_db, conversation.id, content=f"m{i}", created_at=base + timedelta(days=i))
        conversations.append(conversation)

    _set_rls_user(db_session, owner.id)
    result = backfill_message_sequence_numbers(db_session, owner.id, batch_size=2)

    assert result.conversations_numbered == 2
    assert result.messages_assigned == 2
    assert result.remaining_conversations == 2, "a capped run must report what is left, never claim completion"


def test_backfill_rejects_a_non_positive_batch_size(db_session, make_verified_user):
    owner, _ = make_verified_user()
    with pytest.raises(ValueError):
        backfill_message_sequence_numbers(db_session, owner.id, batch_size=0)


def test_excluded_conversations_are_filtered_out_in_sql(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    base = datetime(2026, 2, 1, 9, 0, 0)
    a = _make_conversation(db_session, owner.id, title="a", created_at=base)
    b = _make_conversation(db_session, owner.id, title="b", created_at=base + timedelta(days=1))
    _insert_unsequenced(superuser_db, a.id, content="a1", created_at=base)
    _insert_unsequenced(superuser_db, b.id, content="b1", created_at=base)

    _set_rls_user(db_session, owner.id)
    assert candidate_conversation_ids(db_session, owner.id, 10) == [a.id, b.id]
    assert candidate_conversation_ids(db_session, owner.id, 10, {a.id}) == [b.id]


def test_public_holds_no_execute_on_either_new_function(superuser_db):
    """Migration 0019/0027's standing rule for every function a migration creates. Revoking
    EXECUTE from PUBLIC must not stop the triggers firing — every other test in this file
    inserts and updates messages through the restricted runtime role, which is what proves it."""
    rows = superuser_db.execute(
        sa_text(
            "SELECT routine_name FROM information_schema.routine_privileges "
            "WHERE routine_schema = 'public' AND privilege_type = 'EXECUTE' AND grantee = 'PUBLIC' "
            "AND routine_name IN ('messages_assign_sequence_number', 'messages_deny_sequence_number_rewrite')"
        )
    ).scalars().all()
    assert rows == []


def test_both_new_functions_exist_exactly_once_with_a_pinned_search_path(superuser_db):
    """A second overload could carry its own grants and silently shadow the reviewed one — the
    same "exactly 1 overload" check app/rls.py performs for the mainai_job functions. The
    pinned `search_path` matters because the assignment function resolves `public.messages`."""
    rows = superuser_db.execute(
        sa_text(
            "SELECT p.proname, p.proconfig, l.lanname "
            "FROM pg_proc p JOIN pg_language l ON l.oid = p.prolang "
            "WHERE p.pronamespace = 'public'::regnamespace "
            "AND p.proname IN ('messages_assign_sequence_number', 'messages_deny_sequence_number_rewrite')"
        )
    ).all()
    assert len(rows) == 2, f"expected exactly one overload of each function, found {rows}"
    for row in rows:
        assert row.lanname == "plpgsql"
        assert row.proconfig and "search_path=pg_catalog" in row.proconfig


def test_advisory_lock_key_matches_the_migration(db_session):
    """The backfill and migration 0030's insert trigger must serialize against each other. A
    drift in this constant would silently remove the only thing preventing a live insert from
    interleaving with a conversation's numbering — the same "these two integers must stay in
    sync" guard app/rls.py keeps for PRIVILEGE_BOOT_ADVISORY_LOCK_KEY."""
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parent.parent.parent.parent / "alembic" / "versions" / "0030_message_sequence_number.py"
    ).read_text(encoding="utf-8")
    assert f"pg_advisory_xact_lock({MESSAGE_SEQUENCE_ADVISORY_LOCK_NAMESPACE}," in migration


# --- G: the durable job ------------------------------------------------------------------------


def test_message_sequence_backfill_is_on_the_capability_manifest(db_session):
    assert MESSAGE_SEQUENCE_BACKFILL_JOB_TYPE in CAPABILITY_MANIFEST


def test_the_capability_is_available_with_no_ai_provider_configured(db_session, monkeypatch):
    """The "keep working without AI" rule, enforced rather than asserted: this capability must
    stay available even when resolving a chat provider would fail outright."""
    import app.providers.registry as registry

    def _explode(*_args, **_kwargs):
        raise RuntimeError("no provider configured")

    monkeypatch.setattr(registry, "resolve_active", _explode)

    status = get_capability_status(db_session, MESSAGE_SEQUENCE_BACKFILL_JOB_TYPE)
    assert status.implemented is True
    assert status.configured is True
    assert status.currently_available is True
    require_capability(db_session, MESSAGE_SEQUENCE_BACKFILL_JOB_TYPE)  # must not raise

    # ...while a provider-dependent capability correctly becomes unavailable in the same state.
    with pytest.raises(CapabilityUnavailableError):
        require_capability(db_session, "corpus_review")


def test_the_capability_reports_that_it_modifies_existing_data(db_session):
    status = get_capability_status(db_session, MESSAGE_SEQUENCE_BACKFILL_JOB_TYPE)
    assert status.modifies_existing_data is True
    assert status.writes_new_records is False


def test_a_capability_missing_from_the_provider_role_map_still_fails_closed(db_session, monkeypatch):
    """`None` (reviewed: needs no provider) must never be confused with "forgotten"."""
    import app.mainai_runtime_contract as contract

    monkeypatch.setattr(contract, "CAPABILITY_MANIFEST", frozenset({"forgotten_capability"}))
    status = get_capability_status(db_session, "forgotten_capability")
    assert status.configured is False
    assert status.currently_available is False
    assert status.unavailable_reason == "not_configured"


def test_create_job_rejects_input_refs_for_this_job_type(db_session, make_verified_user):
    owner, _ = make_verified_user()
    with pytest.raises(service.InvalidInputRefsError):
        service.create_job(
            db_session,
            owner_id=owner.id,
            job_type=MESSAGE_SEQUENCE_BACKFILL_JOB_TYPE,
            input_refs=[{"type": "document", "id": str(uuid.uuid4())}],
            created_by="founder",
        )


def _claim(db, job_id) -> tuple[str, int]:
    """Claims `job_id` on the superuser connection, exactly as app/worker.py's real claim step
    does (see app/jobs/mainai_job_lease.py's own docstring for why the claim must run outside
    any single owner's RLS scope). Same helper shape as tests/backend/test_mainai_jobs.py's."""
    from sqlalchemy.orm import sessionmaker

    from app.db import migration_engine
    from app.jobs.mainai_job_lease import claim_next_mainai_job

    claim_db = sessionmaker(bind=migration_engine)()
    try:
        claimed = claim_next_mainai_job(claim_db, "test-worker", 120)
    finally:
        claim_db.close()
    assert claimed is not None
    assert claimed[0] == job_id
    return "test-worker", claimed[2]


@pytest.mark.asyncio
async def test_the_job_numbers_everything_and_reports_truthfully(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    base = datetime(2026, 3, 1, 8, 0, 0)
    conversations = []
    for i in range(3):
        conversation = _make_conversation(db_session, owner.id, title=f"c{i}", created_at=base + timedelta(days=i))
        for j in range(2):
            _insert_unsequenced(superuser_db, conversation.id, content=f"c{i}-m{j}", created_at=base + timedelta(days=i, minutes=j))
        conversations.append(conversation)

    job = service.create_job(
        db_session, owner_id=owner.id, job_type=MESSAGE_SEQUENCE_BACKFILL_JOB_TYPE, input_refs=[], created_by="founder"
    )
    worker_id, generation = _claim(db_session, job.id)

    await run_message_sequence_backfill_job(
        db_session, job.id, owner.id, worker_id=worker_id, lease_generation=generation, lease_seconds=60
    )

    refreshed = superuser_db.get(MainAIJob, job.id)
    superuser_db.refresh(refreshed)
    assert refreshed.status == MainAIJobStatus.completed
    assert refreshed.progress_current == 3
    assert refreshed.progress_total == 3
    assert "Numbered 6 message(s) across 3 of 3 conversation(s)" in refreshed.public_message
    assert "No unnumbered messages remain" in refreshed.public_message

    for conversation in conversations:
        assert [n for _c, n in _sequences(superuser_db, conversation.id)] == [1, 2]


@pytest.mark.asyncio
async def test_the_job_completes_cleanly_with_nothing_to_do(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    _set_rls_user(db_session, owner.id)
    job = service.create_job(
        db_session, owner_id=owner.id, job_type=MESSAGE_SEQUENCE_BACKFILL_JOB_TYPE, input_refs=[], created_by="founder"
    )
    worker_id, generation = _claim(db_session, job.id)

    await run_message_sequence_backfill_job(
        db_session, job.id, owner.id, worker_id=worker_id, lease_generation=generation, lease_seconds=60
    )

    refreshed = superuser_db.get(MainAIJob, job.id)
    superuser_db.refresh(refreshed)
    assert refreshed.status == MainAIJobStatus.completed
    assert refreshed.progress_total == 0
    assert "No unnumbered messages remain" in refreshed.public_message


@pytest.mark.asyncio
async def test_the_job_stops_between_conversations_when_cancelled(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    base = datetime(2026, 3, 1, 8, 0, 0)
    for i in range(2):
        conversation = _make_conversation(db_session, owner.id, title=f"c{i}", created_at=base + timedelta(days=i))
        _insert_unsequenced(superuser_db, conversation.id, content=f"c{i}-m0", created_at=base + timedelta(days=i))

    job = service.create_job(
        db_session, owner_id=owner.id, job_type=MESSAGE_SEQUENCE_BACKFILL_JOB_TYPE, input_refs=[], created_by="founder"
    )
    service.request_cancel(db_session, job.id, requested_by=owner.id)
    worker_id, generation = _claim(db_session, job.id)

    await run_message_sequence_backfill_job(
        db_session, job.id, owner.id, worker_id=worker_id, lease_generation=generation, lease_seconds=60
    )

    refreshed = superuser_db.get(MainAIJob, job.id)
    superuser_db.refresh(refreshed)
    assert refreshed.status == MainAIJobStatus.cancelled
    assert count_unsequenced_messages(db_session, owner.id) == 2, "a cancel before the first conversation numbers nothing"


@pytest.mark.asyncio
async def test_a_job_whose_lease_was_stolen_writes_nothing_at_all(db_session, superuser_db, make_verified_user):
    """Lease fencing all the way down to the message rows: a worker running with a stale
    generation must not number anything, because the fenced progress write shares the
    numbering's transaction."""
    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)
    _insert_unsequenced(superuser_db, conversation.id, content="historisk", created_at=datetime(2026, 3, 1))

    job = service.create_job(
        db_session, owner_id=owner.id, job_type=MESSAGE_SEQUENCE_BACKFILL_JOB_TYPE, input_refs=[], created_by="founder"
    )
    worker_id, generation = _claim(db_session, job.id)

    await run_message_sequence_backfill_job(
        db_session, job.id, owner.id, worker_id=worker_id, lease_generation=generation + 1, lease_seconds=60
    )

    assert _sequences(superuser_db, conversation.id) == [("historisk", None)]
    refreshed = superuser_db.get(MainAIJob, job.id)
    superuser_db.refresh(refreshed)
    assert refreshed.status == MainAIJobStatus.running, "a fenced-out worker must not transition the job either"


@pytest.mark.asyncio
async def test_the_job_skips_a_conflicting_conversation_and_still_numbers_the_rest(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    base = datetime(2026, 3, 1, 8, 0, 0)
    good = _make_conversation(db_session, owner.id, title="bra", created_at=base)
    bad = _make_conversation(db_session, owner.id, title="trasig", created_at=base + timedelta(days=1))
    _insert_unsequenced(superuser_db, good.id, content="bra-1", created_at=base)
    _insert_unsequenced(superuser_db, bad.id, content="trasig-1", created_at=base)
    superuser_db.execute(sa_text("SET LOCAL session_replication_role = replica"))
    superuser_db.execute(
        sa_text(
            "INSERT INTO messages (id, conversation_id, role, content, status, created_at, sequence_number) "
            "VALUES (:id, :cid, 'user', 'kollision', 'succeeded', :created_at, 1)"
        ),
        {"id": str(uuid.uuid4()), "cid": str(bad.id), "created_at": base + timedelta(hours=1)},
    )
    superuser_db.commit()

    job = service.create_job(
        db_session, owner_id=owner.id, job_type=MESSAGE_SEQUENCE_BACKFILL_JOB_TYPE, input_refs=[], created_by="founder"
    )
    worker_id, generation = _claim(db_session, job.id)

    await run_message_sequence_backfill_job(
        db_session, job.id, owner.id, worker_id=worker_id, lease_generation=generation, lease_seconds=60
    )

    refreshed = superuser_db.get(MainAIJob, job.id)
    superuser_db.refresh(refreshed)
    assert refreshed.status == MainAIJobStatus.completed
    assert "1 conversation(s) skipped as conflicting" in refreshed.public_message
    assert "still unnumbered — run again" in refreshed.public_message
    assert _sequences(superuser_db, good.id) == [("bra-1", 1)]
    assert ("trasig-1", None) in _sequences(superuser_db, bad.id), "the conflicting one is left exactly as it was"


@pytest.mark.asyncio
async def test_a_second_job_run_resumes_where_a_capped_one_left_off(db_session, superuser_db, make_verified_user, monkeypatch):
    import app.jobs.handlers.message_sequence_backfill as job_module

    monkeypatch.setattr(job_module, "MAX_CONVERSATIONS_PER_RUN", 1)

    owner, _ = make_verified_user()
    base = datetime(2026, 3, 1, 8, 0, 0)
    for i in range(2):
        conversation = _make_conversation(db_session, owner.id, title=f"c{i}", created_at=base + timedelta(days=i))
        _insert_unsequenced(superuser_db, conversation.id, content=f"c{i}-m0", created_at=base + timedelta(days=i))

    first = service.create_job(
        db_session, owner_id=owner.id, job_type=MESSAGE_SEQUENCE_BACKFILL_JOB_TYPE, input_refs=[], created_by="founder"
    )
    worker_id, generation = _claim(db_session, first.id)
    await run_message_sequence_backfill_job(
        db_session, first.id, owner.id, worker_id=worker_id, lease_generation=generation, lease_seconds=60
    )

    refreshed = superuser_db.get(MainAIJob, first.id)
    superuser_db.refresh(refreshed)
    assert "Stopped at this run's 1-conversation cap" in refreshed.public_message
    assert count_unsequenced_messages(db_session, owner.id) == 1

    monkeypatch.setattr(job_module, "MAX_CONVERSATIONS_PER_RUN", 2000)
    second = service.create_job(
        db_session, owner_id=owner.id, job_type=MESSAGE_SEQUENCE_BACKFILL_JOB_TYPE, input_refs=[], created_by="founder"
    )
    worker_id, generation = _claim(db_session, second.id)
    await run_message_sequence_backfill_job(
        db_session, second.id, owner.id, worker_id=worker_id, lease_generation=generation, lease_seconds=60
    )

    assert count_unsequenced_messages(db_session, owner.id) == 0


@pytest.mark.asyncio
async def test_the_worker_dispatches_this_job_type(db_session, superuser_db, make_verified_user):
    """The real dispatch path in app/worker.py — not the job function called directly. A job
    type on the manifest but missing from the dispatcher would otherwise be marked `failed`
    with `unexpected`, which is exactly the gap this asserts against."""
    from app.worker import process_claimed_mainai_job

    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)
    _insert_unsequenced(superuser_db, conversation.id, content="historisk", created_at=datetime(2026, 3, 1))

    job = service.create_job(
        db_session, owner_id=owner.id, job_type=MESSAGE_SEQUENCE_BACKFILL_JOB_TYPE, input_refs=[], created_by="founder"
    )
    worker_id, generation = _claim(db_session, job.id)

    await process_claimed_mainai_job(db_session, job.id, owner.id, worker_id, generation, 60)

    refreshed = superuser_db.get(MainAIJob, job.id)
    superuser_db.refresh(refreshed)
    assert refreshed.status == MainAIJobStatus.completed
    assert _sequences(superuser_db, conversation.id) == [("historisk", 1)]


# --- H: live inserts and the backfill agreeing --------------------------------------------------


def test_a_message_inserted_while_the_backfill_runs_ends_up_after_the_history(
    db_session, superuser_db, make_verified_user
):
    """End-to-end version of the invariant proof, in the order a real deploy hits it: history
    exists unnumbered, the founder keeps chatting, then the backfill runs. The final ordering
    must match `(created_at, id)` for every row, with no duplicate and no renumbering."""
    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)
    base = datetime(2026, 4, 1, 10, 0, 0)
    for i in range(3):
        _insert_unsequenced(superuser_db, conversation.id, content=f"gammal-{i}", created_at=base + timedelta(minutes=i))

    _set_rls_user(db_session, owner.id)
    live = _add_message(db_session, conversation.id, content="ny", created_at=base + timedelta(hours=1))
    assert live.sequence_number == 4

    backfill_conversation(db_session, conversation.id)

    rows = superuser_db.execute(
        sa_text("SELECT content, sequence_number FROM messages WHERE conversation_id = :cid ORDER BY created_at, id"),
        {"cid": str(conversation.id)},
    ).all()
    assert [r.sequence_number for r in rows] == [1, 2, 3, 4]
    assert [r.content for r in rows] == ["gammal-0", "gammal-1", "gammal-2", "ny"]
