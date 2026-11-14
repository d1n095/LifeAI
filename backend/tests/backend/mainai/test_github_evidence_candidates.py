"""Life MainAI Intelligence program -- GitHub evidence staging. Proves the same structural
rule app.work_candidates already proves one level up: a GitHub PR/issue existing (or looking
urgent -- ci_red, stale) never implies authorization. record_github_evidence_candidates()
never touches mainai_goals; only authorize_github_evidence_candidate() can, and only with the
caller's own explicit authorized_by. See migration 0069_github_evidence_staging's own module
docstring, and app.models.github_evidence.GithubEvidenceCandidate's docstring, for why this is
a separate table from app.work_candidates rather than a reuse of it."""

import uuid

import pytest

from app.github_evidence import (
    GithubEvidenceError,
    authorize_github_evidence_candidate,
    candidate_idempotency_key,
    dismiss_github_evidence_candidate,
    get_github_evidence_candidate,
    list_github_evidence_candidates,
    record_github_evidence_candidates,
)
from app.mainai_execution.planner import get_goal
from app.models.user import User
from app.safe_planner.github_work_queue import WorkQueueItem


def _owner(db) -> User:
    user = User(email=f"ghe-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def _item(number: int, *, kind: str = "open_pull_request", signals: tuple[str, ...] = ()) -> WorkQueueItem:
    return WorkQueueItem(
        kind=kind,
        number=number,
        title=f"PR {number}",
        html_url=f"https://github.com/d1n095/LifeAI/pull/{number}",
        updated_at="2026-11-14T00:00:00Z",
        signals=signals,
    )


def test_record_github_evidence_candidates_never_writes_to_mainai_goals(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()

    rows = record_github_evidence_candidates(superuser_db, owner_id=owner.id, source_repo="d1n095/LifeAI", items=[_item(1)])
    superuser_db.commit()

    assert len(rows) == 1
    assert rows[0].status == "unreviewed"
    assert rows[0].authorized_goal_id is None


def test_recording_the_same_pr_again_updates_in_place_rather_than_duplicating(superuser_db):
    """Unlike app.work_candidates' stricter idempotency (reject a reused key with different
    fields), live GitHub signals for the SAME PR are expected to change between scans -- a
    re-scan must update signals in place, never create a second row."""
    owner = _owner(superuser_db)
    superuser_db.commit()

    first = record_github_evidence_candidates(
        superuser_db, owner_id=owner.id, source_repo="d1n095/LifeAI", items=[_item(1, signals=("draft",))]
    )
    superuser_db.commit()

    second = record_github_evidence_candidates(
        superuser_db, owner_id=owner.id, source_repo="d1n095/LifeAI", items=[_item(1, signals=("ci_red", "stale_7d"))]
    )
    superuser_db.commit()

    assert second[0].id == first[0].id
    assert second[0].signals == ["ci_red", "stale_7d"]
    all_rows = list_github_evidence_candidates(superuser_db, owner_id=owner.id)
    assert len(all_rows) == 1


def test_recording_an_issue_and_a_pr_with_the_same_number_are_distinct_rows(superuser_db):
    """source_kind is part of identity -- PR #1 and issue #1 in the same repo must never
    collide on idempotency_key."""
    owner = _owner(superuser_db)
    superuser_db.commit()

    rows = record_github_evidence_candidates(
        superuser_db,
        owner_id=owner.id,
        source_repo="d1n095/LifeAI",
        items=[_item(1, kind="open_pull_request"), _item(1, kind="open_issue")],
    )
    superuser_db.commit()

    assert len({r.id for r in rows}) == 2
    assert {r.idempotency_key for r in rows} == {
        candidate_idempotency_key(source_repo="d1n095/LifeAI", source_kind="open_pull_request", source_number=1),
        candidate_idempotency_key(source_repo="d1n095/LifeAI", source_kind="open_issue", source_number=1),
    }


def test_authorizing_a_candidate_requires_the_callers_own_explicit_authorized_by(superuser_db):
    """The core structural proof: a ci_red/stale-signaled candidate does NOT imply
    authorization -- the caller must assert authorized_by themselves, and the resulting
    MainAIGoal's created_by is exactly that assertion."""
    owner = _owner(superuser_db)
    superuser_db.commit()
    rows = record_github_evidence_candidates(
        superuser_db, owner_id=owner.id, source_repo="d1n095/LifeAI", items=[_item(1, signals=("ci_red", "stale_7d"))]
    )
    superuser_db.commit()

    authorized, goal = authorize_github_evidence_candidate(
        superuser_db, owner_id=owner.id, candidate_id=rows[0].id, authorized_by="founder"
    )
    superuser_db.commit()

    assert authorized.status == "authorized"
    assert authorized.authorized_goal_id == goal.id
    assert goal.created_by == "founder"
    fetched_goal = get_goal(superuser_db, goal.id)
    assert fetched_goal.id == goal.id


def test_cannot_authorize_an_already_reviewed_candidate_twice(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    rows = record_github_evidence_candidates(superuser_db, owner_id=owner.id, source_repo="d1n095/LifeAI", items=[_item(1)])
    superuser_db.commit()
    authorize_github_evidence_candidate(superuser_db, owner_id=owner.id, candidate_id=rows[0].id, authorized_by="founder")
    superuser_db.commit()

    with pytest.raises(GithubEvidenceError):
        authorize_github_evidence_candidate(superuser_db, owner_id=owner.id, candidate_id=rows[0].id, authorized_by="founder")


def test_dismissing_a_candidate_never_deletes_it_and_records_a_reason(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    rows = record_github_evidence_candidates(superuser_db, owner_id=owner.id, source_repo="d1n095/LifeAI", items=[_item(1)])
    superuser_db.commit()

    dismissed = dismiss_github_evidence_candidate(
        superuser_db, owner_id=owner.id, candidate_id=rows[0].id, reason="Already superseded by a later PR."
    )
    superuser_db.commit()
    assert dismissed.status == "dismissed"
    assert "superseded" in dismissed.dismissed_reason

    fetched = get_github_evidence_candidate(superuser_db, owner_id=owner.id, candidate_id=rows[0].id)
    assert fetched is not None


def test_a_rescan_never_overwrites_an_already_authorized_or_dismissed_candidates_status(superuser_db):
    """The recorder is a signal producer, never a truth writer -- re-recording a PR after the
    founder authorized or dismissed it must refresh observation fields only, never flip
    status back to unreviewed or touch authorized_goal_id/dismissed_reason."""
    owner = _owner(superuser_db)
    superuser_db.commit()
    rows = record_github_evidence_candidates(superuser_db, owner_id=owner.id, source_repo="d1n095/LifeAI", items=[_item(1)])
    superuser_db.commit()
    authorized, goal = authorize_github_evidence_candidate(
        superuser_db, owner_id=owner.id, candidate_id=rows[0].id, authorized_by="founder"
    )
    superuser_db.commit()

    rescanned = record_github_evidence_candidates(
        superuser_db, owner_id=owner.id, source_repo="d1n095/LifeAI", items=[_item(1, signals=("ci_green",))]
    )
    superuser_db.commit()

    assert rescanned[0].id == authorized.id
    assert rescanned[0].status == "authorized"
    assert rescanned[0].authorized_goal_id == goal.id
    assert rescanned[0].signals == ["ci_green"]  # observation DID refresh


def test_list_github_evidence_candidates_filters_by_status(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    rows = record_github_evidence_candidates(
        superuser_db, owner_id=owner.id, source_repo="d1n095/LifeAI", items=[_item(1), _item(2), _item(3)]
    )
    superuser_db.commit()
    authorize_github_evidence_candidate(superuser_db, owner_id=owner.id, candidate_id=rows[1].id, authorized_by="founder")
    dismiss_github_evidence_candidate(superuser_db, owner_id=owner.id, candidate_id=rows[2].id, reason="noise")
    superuser_db.commit()

    unreviewed_ids = {c.id for c in list_github_evidence_candidates(superuser_db, owner_id=owner.id, status="unreviewed")}
    assert unreviewed_ids == {rows[0].id}
    all_ids = {c.id for c in list_github_evidence_candidates(superuser_db, owner_id=owner.id)}
    assert all_ids == {r.id for r in rows}


def test_authorizing_or_dismissing_a_candidate_belonging_to_another_owner_fails_closed(superuser_db):
    owner_a = _owner(superuser_db)
    owner_b = _owner(superuser_db)
    superuser_db.commit()
    rows = record_github_evidence_candidates(superuser_db, owner_id=owner_a.id, source_repo="d1n095/LifeAI", items=[_item(1)])
    superuser_db.commit()

    with pytest.raises(GithubEvidenceError):
        dismiss_github_evidence_candidate(superuser_db, owner_id=owner_b.id, candidate_id=rows[0].id, reason="not mine")
    with pytest.raises(GithubEvidenceError):
        authorize_github_evidence_candidate(superuser_db, owner_id=owner_b.id, candidate_id=rows[0].id, authorized_by="founder")


def test_recording_the_same_pr_for_two_different_owners_creates_two_independent_rows(superuser_db):
    """idempotency_key is scoped per-owner (UNIQUE (owner_id, idempotency_key)) -- two
    founders watching the same public repo must each get their own row, never collide."""
    owner_a = _owner(superuser_db)
    owner_b = _owner(superuser_db)
    superuser_db.commit()

    rows_a = record_github_evidence_candidates(superuser_db, owner_id=owner_a.id, source_repo="d1n095/LifeAI", items=[_item(1)])
    rows_b = record_github_evidence_candidates(superuser_db, owner_id=owner_b.id, source_repo="d1n095/LifeAI", items=[_item(1)])
    superuser_db.commit()

    assert rows_a[0].id != rows_b[0].id
    assert rows_a[0].idempotency_key == rows_b[0].idempotency_key
