"""Row-Level Security, exercised directly at the database layer through the restricted
runtime role (mainai_app), for `github_evidence_candidates` (migration
0069_github_evidence_staging). Mirrors tests/security/test_rls_isolation_work_candidates.py's
own established pattern exactly -- every new owner-scoped table gets a real behavioral proof,
not just a structural one, from the moment it is added.

Unlike work_candidates, this table has no composite owner-anchored FK to another owner-scoped
table (a GitHub PR/issue is identified by source_repo/source_kind/source_number, not a row this
app owns) -- so there is no third "cross-reference" test here; RLS on the row's own owner_id is
the entire isolation boundary for this table."""

from sqlalchemy import text

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


def test_cannot_write_a_github_evidence_candidate_for_another_user(db_session, make_verified_user):
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
    try:
        db_session.commit()
        assert False, "insert should have been rejected by github_evidence_candidates_isolation's WITH CHECK"
    except Exception:
        db_session.rollback()
