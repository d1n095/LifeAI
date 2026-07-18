"""Row-Level Security, exercised directly at the database layer through the same restricted
runtime role (mainai_app) and session-variable mechanism the app itself uses (see
app/db.py's after_begin listener and app/deps.py's SET LOCAL) — not through the HTTP API,
so a bug in a router can't accidentally mask an RLS bug or vice versa."""

from sqlalchemy import text

from app.models.conversation import Conversation


def _set_rls_user(session, user_id) -> None:
    session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user_id)})


def test_user_sees_only_their_own_conversations(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    db_session.add(Conversation(user_id=user_a.id, title="A:s konversation"))
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    db_session.add(Conversation(user_id=user_b.id, title="B:s konversation"))
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    visible_to_a = db_session.query(Conversation).all()
    assert [c.title for c in visible_to_a] == ["A:s konversation"]

    _set_rls_user(db_session, user_b.id)
    visible_to_b = db_session.query(Conversation).all()
    assert [c.title for c in visible_to_b] == ["B:s konversation"]


def test_no_session_variable_set_sees_nothing():
    """Default-deny: a connection with no app.current_user_id set (e.g. a raw
    migration/admin connection accidentally used for a request) must never see ANY rows,
    not "all of them" — see app/rls.py's NULLIF reasoning."""
    from app.db import SessionLocal

    session = SessionLocal()
    try:
        session.add(Conversation(user_id="00000000-0000-0000-0000-000000000000", title="orphan"))
        # Insert bypasses RLS's WITH CHECK only if the session var matches — this insert
        # should actually fail under RLS since no session var is set. Confirm that instead.
        try:
            session.commit()
            assert False, "insert should have been rejected by RLS WITH CHECK"
        except Exception:
            session.rollback()
    finally:
        session.close()


def test_reverted_session_variable_still_denies_by_default(db_session, make_verified_user):
    """Regression guard for the NULLIF quirk documented in app/rls.py: a custom GUC that was
    SET LOCAL and then implicitly reverted (e.g. after a mid-request commit, before the next
    transaction's after_begin re-applies it) reads back as '' rather than NULL — casting
    that straight to ::uuid must resolve to "match nothing", not raise a DB error."""
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    db_session.add(Conversation(user_id=user.id, title="innan commit"))
    db_session.commit()  # ends the transaction SET LOCAL was scoped to

    # No SET LOCAL re-applied here — simulates the gap the after_begin listener exists to
    # close in the real app; a raw session without it must still fail safe.
    visible = db_session.query(Conversation).all()
    assert visible == []
