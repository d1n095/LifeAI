"""Row-Level Security, exercised directly at the database layer through the restricted
runtime role (mainai_app), for `github_evidence_candidates` (migration
0076_github_evidence_staging (0069 on its own PR branch, renumbered on the 0069-composition
integration branch). Mirrors tests/security/test_rls_isolation_work_candidates.py's
own established pattern exactly -- every new owner-scoped table gets a real behavioral proof,
not just a structural one, from the moment it is added.

Unlike work_candidates, this table has no composite owner-anchored FK to another owner-scoped
table (a GitHub PR/issue is identified by source_repo/source_kind/source_number, not a row this
app owns) -- so there is no third "cross-reference" test here; RLS on the row's own owner_id is
the entire isolation boundary for this table."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.github_evidence import record_github_evidence_candidates
from app.models.github_evidence import GithubEvidenceCandidate
from app.safe_planner.github_work_queue import WorkQueueItem


def _set_rls_user(session, user_id) -> None:
    session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user_id)})


def _item(number: int) -> WorkQueueItem:
    return WorkQueueItem(
        kind="open_pull_request",
        number=number,
        title=f"PR {number}",
        html_url=f"https://github.com/d1n095/LifeAI/pull/{number}",
        updated_at="2026-11-14T00:00:00Z",
    )


def test_user_never_reads_another_users_github_evidence_candidates(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    record_github_evidence_candidates(db_session, owner_id=user_a.id, source_repo="d1n095/LifeAI", items=[_item(1)])
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    record_github_evidence_candidates(db_session, owner_id=user_b.id, source_repo="d1n095/LifeAI", items=[_item(1)])
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    visible_to_a = db_session.query(GithubEvidenceCandidate).all()
    assert len(visible_to_a) == 1
    assert visible_to_a[0].owner_id == user_a.id

    _set_rls_user(db_session, user_b.id)
    visible_to_b = db_session.query(GithubEvidenceCandidate).all()
    assert len(visible_to_b) == 1
    assert visible_to_b[0].owner_id == user_b.id


def test_cannot_write_a_github_evidence_candidate_for_another_user(db_session, superuser_db, make_verified_user):
    """Hardening finding (founder-directed security remediation pass): the previous version
    of this test wrapped BOTH the real write and its own `assert False` inside one
    `try/except Exception`. `AssertionError` is itself an `Exception` subclass, so if RLS ever
    failed to block the cross-owner insert, `db_session.commit()` would succeed, `assert False`
    would raise, and that SAME `except Exception` would silently swallow it -- a green test
    that proves nothing, exactly the class of defect this table's own isolation claim must
    never rest on. Fixed to require a specific DB-level exception at the one statement that
    must raise, with a superuser-session row-count read as the second, independent proof --
    belt and suspenders, not one fragile assertion."""
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    db_session.add(
        GithubEvidenceCandidate(
            owner_id=user_b.id,
            source_repo="d1n095/LifeAI",
            source_kind="open_pull_request",
            source_number=1,
            source_url="https://github.com/d1n095/LifeAI/pull/1",
            title="x",
            idempotency_key="rls-ghe-cross",
        )
    )
    with pytest.raises(DBAPIError):
        db_session.commit()
    db_session.rollback()

    # Independent of whichever exception type/path the ORM surfaced above: the row must
    # genuinely not exist, checked through the unrestricted superuser connection so this
    # assertion cannot itself be fooled by RLS hiding a row that actually got written.
    count = superuser_db.execute(
        text("SELECT count(*) FROM github_evidence_candidates WHERE idempotency_key = :k"), {"k": "rls-ghe-cross"}
    ).scalar()
    assert count == 0


def test_cannot_update_another_users_github_evidence_candidate(db_session, superuser_db, make_verified_user):
    """UPDATE denial, exercised with two genuinely separate DB sessions (db_session as owner
    A, superuser_db re-scoped to owner B via its own SET LOCAL) rather than one session
    switching GUCs back and forth -- proves this isn't an artifact of ORM identity-map reuse
    on a single connection.

    Unlike INSERT/DELETE, a cross-owner UPDATE under this table's single USING+WITH CHECK
    policy (migration 0070) does NOT raise -- the row is simply invisible to A's UPDATE
    statement under the USING clause, so it matches zero rows and returns normally. Verified
    directly: a version of this test asserting `pytest.raises` around the UPDATE failed with
    "DID NOT RAISE", confirming the real behavior before writing the correct assertion below
    -- rowcount == 0 and the title unchanged, not an exception."""
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(superuser_db, user_b.id)
    row = GithubEvidenceCandidate(
        owner_id=user_b.id, source_repo="d1n095/LifeAI", source_kind="open_pull_request", source_number=2,
        source_url="https://github.com/d1n095/LifeAI/pull/2", title="owned by b", idempotency_key="rls-ghe-update-target",
    )
    superuser_db.add(row)
    superuser_db.commit()
    row_id = row.id

    _set_rls_user(db_session, user_a.id)
    result = db_session.execute(
        text("UPDATE github_evidence_candidates SET title = 'hijacked by a' WHERE id = :id"), {"id": str(row_id)}
    )
    assert result.rowcount == 0, "owner A's UPDATE must match zero rows of owner B's data under RLS"
    db_session.commit()

    title = superuser_db.execute(text("SELECT title FROM github_evidence_candidates WHERE id = :id"), {"id": str(row_id)}).scalar()
    assert title == "owned by b"


def test_cannot_delete_another_users_github_evidence_candidate(db_session, superuser_db, make_verified_user):
    """DELETE denial, same two-separate-sessions discipline as the UPDATE test above."""
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(superuser_db, user_b.id)
    row = GithubEvidenceCandidate(
        owner_id=user_b.id, source_repo="d1n095/LifeAI", source_kind="open_pull_request", source_number=3,
        source_url="https://github.com/d1n095/LifeAI/pull/3", title="owned by b too", idempotency_key="rls-ghe-delete-target",
    )
    superuser_db.add(row)
    superuser_db.commit()
    row_id = row.id

    _set_rls_user(db_session, user_a.id)
    with pytest.raises(DBAPIError):
        db_session.execute(text("DELETE FROM github_evidence_candidates WHERE id = :id"), {"id": str(row_id)})
        db_session.commit()
    db_session.rollback()

    count = superuser_db.execute(text("SELECT count(*) FROM github_evidence_candidates WHERE id = :id"), {"id": str(row_id)}).scalar()
    assert count == 1
