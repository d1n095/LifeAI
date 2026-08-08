"""Migration 0031 — owner-scoped RLS on `messages`, and specifically what it does to the
machinery that was built while `messages` had no policy at all.

tests/security/test_rls_isolation.py already covers the isolation guarantee itself (read,
write, update, delete, default-deny). This file covers the CONSEQUENCES of turning that policy
on, which is where the real risk in this change lives:

  A. Migration 0030's `messages_assign_sequence_number()` trigger aggregates over
     `public.messages` and is NOT SECURITY DEFINER, so its aggregate became RLS-filtered the
     moment 0031 shipped. If the policy could ever hide a row from that aggregate, the trigger
     would under-count and hand out an ordinal that collides with an existing one — silently
     corrupting the invariant all of S1B rests on. These tests assert it cannot.
  B. The S1B backfill (app/rag/message_sequence_backfill.py) reads and UPDATEs `messages` in
     bulk. It must keep working unchanged, and must still leave other owners untouched.
  C. The delete paths (app/routers/conversations.py's delete_conversation, and
     app/rag/account_erasure.py) delete messages via the restricted role. Both delete messages
     BEFORE the owning conversation — an ordering that is now load-bearing, because the policy
     resolves through that conversation row. `delete_conversation` is covered below;
     account erasure is already covered end-to-end, through the real HTTP path and verified on
     the superuser connection, by tests/account/test_account_deletion.py::
     test_deleted_account_conversations_are_gone, so it is deliberately not duplicated here —
     that test now exercises the policy too, and was confirmed still green under it.
  D. app/rls.py's boot-time self-heal loop can repair `messages_isolation` if it is ever
     dropped, exactly as it can for every other table.

Real local Postgres with the real Alembic-built schema: the policy and the triggers under test
exist only in the database, so a faked DB would test nothing here at all.
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from app.db import migration_engine
from app.models.conversation import Conversation, Message, MessageRole
from app.rag.message_sequence_backfill import (
    backfill_message_sequence_numbers,
    candidate_conversation_ids,
    count_unsequenced_messages,
)
from app.request_context import current_user_id as current_user_id_var
from app.rls import POLICY_DEFINITIONS, apply_rls

# Bootstrapped at app startup from the same env vars tests/conftest.py sets — see
# app/bootstrap.py. The conversations router is founder-only (app/deps.py's require_founder
# pins both role AND id), so the router-level test below has to act as this account.
FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_conversation(session, owner_id, *, title="Samtal") -> Conversation:
    _set_rls_user(session, owner_id)
    conversation = Conversation(user_id=owner_id, title=title)
    session.add(conversation)
    session.commit()
    return conversation


def _add_message(session, conversation_id, *, content="hej") -> Message:
    message = Message(conversation_id=conversation_id, role=MessageRole.user, content=content)
    session.add(message)
    session.commit()
    return message


def _insert_unsequenced(superuser_db, conversation_id, *, content, created_at) -> uuid.UUID:
    """A pre-0030 history row: no ordinal at all. Same helper (and same `SET LOCAL` discipline)
    as tests/backend/test_message_sequence.py — see that file for why replica mode is the only
    honest way to reproduce pre-migration history."""
    message_id = uuid.uuid4()
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
        sa_text(
            "SELECT content, sequence_number FROM messages WHERE conversation_id = :cid "
            "ORDER BY sequence_number NULLS LAST, created_at, id"
        ),
        {"cid": str(conversation_id)},
    ).all()
    return [(r.content, r.sequence_number) for r in rows]


# --- A: the 0030 assignment trigger still sees the whole conversation ------------------------


def test_ordinal_assignment_is_unaffected_by_another_owners_messages(db_session, superuser_db, make_verified_user):
    """The core interaction, stated as bluntly as possible.

    Owner B holds a large conversation. Owner A writes into their own. If 0031's policy could
    somehow narrow the trigger's aggregate, A's ordinals would come out wrong; if the policy
    were somehow absent, B's rows could inflate them. Neither happens: the aggregate is scoped
    to `NEW.conversation_id`, which is entirely inside one owner's visibility."""
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()

    conversation_b = _make_conversation(db_session, owner_b.id, title="B:s stora samtal")
    _set_rls_user(db_session, owner_b.id)
    for i in range(7):
        _add_message(db_session, conversation_b.id, content=f"B{i}")

    conversation_a = _make_conversation(db_session, owner_a.id, title="A:s samtal")
    _set_rls_user(db_session, owner_a.id)
    first = _add_message(db_session, conversation_a.id, content="A0")
    second = _add_message(db_session, conversation_a.id, content="A1")

    # Neither restarted wrongly nor inflated by B's seven rows.
    assert (first.sequence_number, second.sequence_number) == (1, 2)
    assert _sequences(superuser_db, conversation_b.id) == [(f"B{i}", i + 1) for i in range(7)]


def test_the_formula_invariant_still_holds_under_rls(db_session, superuser_db, make_verified_user):
    """Migration 0030's `GREATEST(max, count) + 1` proof depends on `count(*)` being the TRUE
    number of rows in the conversation, including still-unnumbered history. RLS filtering that
    aggregate down would break the proof directly: a new message would be numbered inside the
    `1..N` range the backfill is going to hand out, and the two would collide.

    Here the conversation holds three pre-0030 rows (all NULL). A live insert must be numbered
    4 — strictly above the 1..3 the backfill will later assign — not 1."""
    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)

    for i in range(3):
        _insert_unsequenced(superuser_db, conversation.id, content=f"historisk{i}", created_at=datetime(2026, 1, 1 + i))

    _set_rls_user(db_session, owner.id)
    live = _add_message(db_session, conversation.id, content="ny")
    assert live.sequence_number == 4

    # And the backfill then fills 1..3 underneath it without colliding.
    _set_rls_user(db_session, owner.id)
    backfill_message_sequence_numbers(db_session, owner.id)
    assert _sequences(superuser_db, conversation.id) == [
        ("historisk0", 1),
        ("historisk1", 2),
        ("historisk2", 3),
        ("ny", 4),
    ]


def test_an_insert_into_another_owners_conversation_never_reaches_the_trigger(db_session, make_verified_user):
    """Why the aggregate can never be evaluated by a session that cannot see the conversation:
    WITH CHECK rejects the row first. This is what makes "the count is always the true one"
    hold as a proof rather than as a hope."""
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()
    conversation_b = _make_conversation(db_session, owner_b.id)

    _set_rls_user(db_session, owner_a.id)
    db_session.add(Message(conversation_id=conversation_b.id, role=MessageRole.user, content="fel agare"))
    with pytest.raises(ProgrammingError, match="row-level security policy"):
        db_session.commit()
    db_session.rollback()


def test_sequence_number_immutability_still_enforced_under_rls(db_session, make_verified_user):
    """0030's second trigger is a BEFORE UPDATE row trigger comparing OLD/NEW — no table scan,
    so RLS cannot affect it. Asserted rather than assumed, because it guards the ordinals S1C
    and S2 will reference."""
    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)
    _set_rls_user(db_session, owner.id)
    message = _add_message(db_session, conversation.id, content="ett")

    _set_rls_user(db_session, owner.id)
    with pytest.raises(DBAPIError, match="immutable once assigned"):
        db_session.execute(
            sa_text("UPDATE messages SET sequence_number = 99 WHERE id = :id"), {"id": str(message.id)}
        )
    db_session.rollback()


# --- B: the S1B backfill under RLS ------------------------------------------------------------


def test_backfill_numbers_own_history_and_leaves_another_owner_untouched(
    db_session, superuser_db, make_verified_user
):
    """The backfill was written against a table with no policy. It must behave identically with
    one — and the other owner's history must remain not merely un-numbered but invisible."""
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()

    conversation_a = _make_conversation(db_session, owner_a.id, title="A")
    conversation_b = _make_conversation(db_session, owner_b.id, title="B")

    for i in range(3):
        _insert_unsequenced(superuser_db, conversation_a.id, content=f"a{i}", created_at=datetime(2026, 1, 1 + i))
    for i in range(2):
        _insert_unsequenced(superuser_db, conversation_b.id, content=f"b{i}", created_at=datetime(2026, 1, 1 + i))

    _set_rls_user(db_session, owner_a.id)
    result = backfill_message_sequence_numbers(db_session, owner_a.id)

    assert result.conversations_numbered == 1
    assert result.messages_assigned == 3
    assert _sequences(superuser_db, conversation_a.id) == [("a0", 1), ("a1", 2), ("a2", 3)]
    # B's history is untouched — verified through the superuser connection so that "untouched"
    # is distinguishable from "merely hidden".
    assert _sequences(superuser_db, conversation_b.id) == [("b0", None), ("b1", None)]


def test_count_unsequenced_messages_counts_only_the_named_owner(db_session, superuser_db, make_verified_user):
    """The VERIFY step of S1B's expand/contract plan. It already JOINed `conversations` and
    filtered by owner explicitly; under 0031 the policy independently enforces the same bound,
    so the number the CONTRACT decision will be based on cannot be inflated by another
    account's rows."""
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()

    conversation_a = _make_conversation(db_session, owner_a.id, title="A")
    conversation_b = _make_conversation(db_session, owner_b.id, title="B")
    _insert_unsequenced(superuser_db, conversation_a.id, content="a0", created_at=datetime(2026, 1, 1))
    for i in range(4):
        _insert_unsequenced(superuser_db, conversation_b.id, content=f"b{i}", created_at=datetime(2026, 1, 1 + i))

    _set_rls_user(db_session, owner_a.id)
    assert count_unsequenced_messages(db_session, owner_a.id) == 1
    assert candidate_conversation_ids(db_session, owner_a.id, 10) == [conversation_a.id]


def test_backfill_for_an_owner_it_is_not_scoped_to_finds_nothing(db_session, superuser_db, make_verified_user):
    """Defense in depth made visible: even if a future caller passed the WRONG owner_id while
    the session is scoped to someone else, the policy means the statement finds nothing rather
    than numbering (and thereby writing to) another account's history."""
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()

    conversation_b = _make_conversation(db_session, owner_b.id, title="B")
    _insert_unsequenced(superuser_db, conversation_b.id, content="b0", created_at=datetime(2026, 1, 1))

    # Session is A; the caller mistakenly asks for B's work.
    _set_rls_user(db_session, owner_a.id)
    result = backfill_message_sequence_numbers(db_session, owner_b.id)

    assert result.conversations_numbered == 0
    assert result.messages_assigned == 0
    assert _sequences(superuser_db, conversation_b.id) == [("b0", None)]


# --- C: the delete paths ----------------------------------------------------------------------


def test_delete_conversation_still_removes_its_messages(client, superuser_db):
    """app/routers/conversations.py deletes messages and then the conversation. That order is
    now load-bearing: the policy resolves through the conversation row, so deleting the parent
    first would leave the children unreachable. (It would also raise a foreign-key violation
    rather than silently orphaning them — loud, not silent — but the order is asserted here so
    a future reorder is caught by a test rather than by production.)

    Driven through the REAL router as the real founder, on the restricted runtime role, so the
    DELETE is subject to `messages_isolation` exactly as it is in production — the whole point
    being that a policy that made this statement match zero rows would leak a deleted account's
    message content into the future."""
    login = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert login.status_code == 200, login.text
    csrf = login.json()["csrf_token"]
    founder_id = superuser_db.execute(
        sa_text("SELECT id FROM users WHERE email = :email"), {"email": FOUNDER_EMAIL}
    ).scalar_one()

    conversation_id = uuid.uuid4()
    superuser_db.execute(
        sa_text(
            "INSERT INTO conversations (id, user_id, title, created_at, updated_at) "
            "VALUES (:id, :uid, 'Att radera', now(), now())"
        ),
        {"id": str(conversation_id), "uid": str(founder_id)},
    )
    superuser_db.execute(
        sa_text(
            "INSERT INTO messages (id, conversation_id, role, content, status, created_at) "
            "VALUES (:id, :cid, 'user', 'ska bort', 'succeeded', now())"
        ),
        {"id": str(uuid.uuid4()), "cid": str(conversation_id)},
    )
    superuser_db.commit()

    def _remaining() -> int:
        return superuser_db.execute(
            sa_text("SELECT count(*) FROM messages WHERE conversation_id = :cid"),
            {"cid": str(conversation_id)},
        ).scalar_one()

    assert _remaining() == 1

    deleted = client.delete(f"/api/conversations/{conversation_id}", headers={"X-CSRF-Token": csrf})
    assert deleted.status_code == 200, deleted.text
    assert _remaining() == 0


# --- D: app/rls.py's self-heal loop -----------------------------------------------------------


def test_messages_policy_is_in_the_registry_and_is_self_healing(db_session, make_verified_user):
    """apply_rls() must be able to recreate `messages_isolation` if it is ever dropped —
    the same guarantee every other table has. A table left FORCE-RLS with no policy is a
    silent, total default-deny, which is why tests/backend/test_rls_policy_registry.py checks
    that the two lists cover each other; this test proves the repair actually runs.

    The DROP is inside try/finally on purpose: this is the one test in the suite that removes a
    security policy from the shared, session-scoped database. If an assertion between the DROP
    and the repair failed, an unguarded version would leave `messages` FORCE-RLS with no policy
    for every subsequent test in the session, turning one real failure into a cascade of
    unrelated confusing ones. `apply_rls()` is idempotent, so running it in `finally` is safe
    even on the path where the test body already called it."""
    assert any(p["table"] == "messages" and p["name"] == "messages_isolation" for p in POLICY_DEFINITIONS)

    owner, _ = make_verified_user()
    conversation = _make_conversation(db_session, owner.id)
    _set_rls_user(db_session, owner.id)
    _add_message(db_session, conversation.id, content="finns")
    db_session.commit()

    try:
        with migration_engine.begin() as conn:
            conn.execute(sa_text("DROP POLICY messages_isolation ON messages"))
            remaining = conn.execute(
                sa_text("SELECT count(*) FROM pg_policies WHERE tablename = 'messages'")
            ).scalar_one()
        assert remaining == 0

        # With RLS still FORCEd and no policy at all, the owner now sees nothing — the
        # availability gap the registry test exists to prevent shipping for a new table.
        _set_rls_user(db_session, owner.id)
        assert db_session.query(Message).all() == []
        db_session.rollback()

        apply_rls(migration_engine)

        _set_rls_user(db_session, owner.id)
        assert [m.content for m in db_session.query(Message).all()] == ["finns"]
        db_session.rollback()
    finally:
        db_session.rollback()
        apply_rls(migration_engine)
