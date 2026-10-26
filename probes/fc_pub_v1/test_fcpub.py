"""Founder-authorized GitHub App review-publication boundary — independent examination.

Subject: security base 7fbe677166864f398ef2b32cc374487cc7034503 (from Codex's verified bundle; not on
GitHub). Drives the REAL app.founder_authority.review_publication + service step-up.

Adversarial, read-only. Founder-authority writes (step-up receipts, grants) are DB-denied to the
ordinary runtime role (PUB0), so the privileged schema-owner session drives the application-logic
tests — the authz controls (identity pin, single-use, effect separation, STOP, exact-SHA) are
app-level and independent of the DB role. The happy-path push/draft-PR (token mint + network) is NOT
executed (no live GitHub App); that leg is code-reviewed in the verdict. first_cycle_go=false.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from _guards import egress  # noqa: F401
from _harness import _privileges, owner_id  # noqa: F401

FOUNDER_PW = "CorrectHorseBattery9!"
REPO = "d1n095/LifeAI"
PREFIX = "review/"
SHA_A = "a" * 40


def _pinned_founder(superuser_db):
    from app.founder import FOUNDER_USER_ID
    from app.models.user import User
    from app.security import hash_password, utcnow_seconds_baseline
    now = utcnow_seconds_baseline()
    u = superuser_db.get(User, FOUNDER_USER_ID)
    if u is None:
        u = User(id=FOUNDER_USER_ID, email="founder@lifeos.local", password_hash=hash_password(FOUNDER_PW),
                 role="founder", email_verified=True, email_verified_at=now, sessions_valid_after=now)
        superuser_db.add(u); superuser_db.commit()
    return u


def _session(superuser_db, user) -> str:
    from app.models.refresh_token import RefreshToken
    jti = str(uuid.uuid4())
    superuser_db.add(RefreshToken(
        id=uuid.uuid4(), user_id=user.id, family_id=uuid.uuid4(),
        token_hash=hashlib.sha256(jti.encode()).hexdigest(), access_jti=jti,
        csrf_token=uuid.uuid4().hex, expires_at=datetime.utcnow() + timedelta(hours=1)))
    superuser_db.commit()
    return jti


def _stepup(sdb, user, jti, purpose):
    from app.founder_authority.service import create_step_up_receipt
    return create_step_up_receipt(sdb, user=user, password=FOUNDER_PW, access_jti=jti, purpose=purpose)


def _artifact(repo=REPO, sha=SHA_A):
    from app.founder_authority.review_publication import VerifiedReviewArtifact
    return VerifiedReviewArtifact(
        source_repository_identity=repo, candidate_ref="refs/heads/x", candidate_sha=sha,
        candidate_tree_sha="b" * 40, base_sha="c" * 40, bundle_sha256="d" * 64, manifest_sha256="e" * 64,
        bundle_relative_path="x.bundle", manifest_relative_path="x.json",
        changed_files=("f.py",), evidence_files=("EVIDENCE.md",))


def _issue(sdb, user, receipt, effect, *, repo=REPO, dest="review/cand-1", ttl_min=4, idem=None, artifact=None):
    from app.founder_authority.review_publication import issue_review_publication_grant
    return issue_review_publication_grant(
        sdb, user=user, step_up_receipt_id=receipt.id, effect=effect, artifact=artifact or _artifact(repo=repo),
        destination_repository_identity=repo, destination_branch=dest, base_branch="main",
        installation_id=12345, github_app_id="app-1",
        expires_at=datetime.utcnow() + timedelta(minutes=ttl_min), idempotency_key=idem or f"idem-{uuid.uuid4()}",
        branch_prefix=PREFIX, protected_branches={"main", "master"})


def _effects():
    from app.founder_authority.review_publication import PUSH_EFFECT, DRAFT_PR_EFFECT, PURPOSE_BY_EFFECT
    return PUSH_EFFECT, DRAFT_PR_EFFECT, PURPOSE_BY_EFFECT


# ---------------------------------------------------------------------------
# PUB0 — DB privilege boundary: ordinary runtime cannot write founder-authority state
# ---------------------------------------------------------------------------

def test_PUB0_runtime_cannot_write_step_up_receipt(db_session, superuser_db):
    from app.founder_authority.service import create_step_up_receipt
    from sqlalchemy.exc import ProgrammingError, InternalError
    founder = _pinned_founder(superuser_db); jti = _session(superuser_db, founder)
    with pytest.raises((ProgrammingError, InternalError)) as ei:
        create_step_up_receipt(db_session, user=founder, password=FOUNDER_PW, access_jti=jti, purpose="FOUNDER_REVIEW_PUSH")
    assert "denied" in str(ei.value).lower()
    db_session.rollback()


# ---------------------------------------------------------------------------
# PUB1 — step-up requires pinned Founder + password + live session
# ---------------------------------------------------------------------------

def test_PUB1_stepup_requires_pinned_founder_password_and_session(db_session, superuser_db, make_verified_user):
    from app.founder_authority.service import create_step_up_receipt, FounderAuthorityError
    member, pw = make_verified_user(role="member")
    with pytest.raises(FounderAuthorityError):                     # not the pinned Founder
        create_step_up_receipt(db_session, user=member, password=pw, access_jti="x", purpose="FOUNDER_REVIEW_PUSH")
    founder = _pinned_founder(superuser_db)
    with pytest.raises(Exception):                                 # no access session
        create_step_up_receipt(superuser_db, user=founder, password=FOUNDER_PW, access_jti=None, purpose="FOUNDER_REVIEW_PUSH")
    jti = _session(superuser_db, founder)
    with pytest.raises(Exception):                                 # wrong password
        create_step_up_receipt(superuser_db, user=founder, password="wrong", access_jti=jti, purpose="FOUNDER_REVIEW_PUSH")


# ---------------------------------------------------------------------------
# PUB2 — grant issuance requires Founder and binds TTL / repo / branch
# ---------------------------------------------------------------------------

def test_PUB2_issue_requires_founder(superuser_db, make_verified_user):
    from app.founder_authority.review_publication import issue_review_publication_grant
    from app.founder_authority.service import FounderAuthorityError
    PUSH, _, _ = _effects()
    member, _ = make_verified_user(role="member")
    with pytest.raises(FounderAuthorityError):
        issue_review_publication_grant(superuser_db, user=member, step_up_receipt_id=uuid.uuid4(), effect=PUSH,
                                       artifact=_artifact(), destination_repository_identity=REPO,
                                       destination_branch="review/x", base_branch="main", installation_id=1,
                                       github_app_id="a", expires_at=datetime.utcnow() + timedelta(minutes=5),
                                       idempotency_key="k", branch_prefix=PREFIX, protected_branches={"main"})


def test_PUB2b_issue_bounds_ttl_repo_and_branch(superuser_db):
    from app.founder_authority.review_publication import issue_review_publication_grant, ReviewPublicationError
    PUSH, _, PBE = _effects()
    f = _pinned_founder(superuser_db)
    with pytest.raises(ReviewPublicationError):                    # TTL > 10 min
        _issue(superuser_db, f, _stepup(superuser_db, f, _session(superuser_db, f), PBE[PUSH]), PUSH, ttl_min=30)
    with pytest.raises(ReviewPublicationError):                    # cross-repository
        issue_review_publication_grant(
            superuser_db, user=f, step_up_receipt_id=_stepup(superuser_db, f, _session(superuser_db, f), PBE[PUSH]).id,
            effect=PUSH, artifact=_artifact(repo="attacker/evil"), destination_repository_identity=REPO,
            destination_branch="review/x", base_branch="main", installation_id=1, github_app_id="a",
            expires_at=datetime.utcnow() + timedelta(minutes=5), idempotency_key=f"k-{uuid.uuid4()}",
            branch_prefix=PREFIX, protected_branches={"main"})
    with pytest.raises(ReviewPublicationError):                    # protected branch as destination
        _issue(superuser_db, f, _stepup(superuser_db, f, _session(superuser_db, f), PBE[PUSH]), PUSH, dest="main")
    with pytest.raises(ReviewPublicationError):                    # destination not under review prefix
        _issue(superuser_db, f, _stepup(superuser_db, f, _session(superuser_db, f), PBE[PUSH]), PUSH, dest="feature/x")


def test_PUB3_effect_separation_purpose_binding(superuser_db):
    from app.founder_authority.review_publication import ReviewPublicationError
    PUSH, DRAFT, PBE = _effects()
    f = _pinned_founder(superuser_db)
    push_receipt = _stepup(superuser_db, f, _session(superuser_db, f), PBE[PUSH])
    with pytest.raises(ReviewPublicationError):                    # PUSH step-up cannot authorize DRAFT_PR
        _issue(superuser_db, f, push_receipt, DRAFT)


def test_PUB4_stepup_is_single_use(superuser_db):
    from app.founder_authority.review_publication import ReviewPublicationError, PUSH_EFFECT
    _, _, PBE = _effects()
    f = _pinned_founder(superuser_db)
    receipt = _stepup(superuser_db, f, _session(superuser_db, f), PBE[PUSH_EFFECT])
    _issue(superuser_db, f, receipt, PUSH_EFFECT, dest="review/first")
    with pytest.raises(ReviewPublicationError):
        _issue(superuser_db, f, receipt, PUSH_EFFECT, dest="review/second")


# ---------------------------------------------------------------------------
# PUB5/6 — consume (claim) guards fire before any token mint / network
# ---------------------------------------------------------------------------

def test_PUB5_cancelled_grant_cannot_be_claimed(superuser_db):
    from app.founder_authority.review_publication import (
        cancel_review_publication_grant, claim_review_publication_grant, ReviewPublicationError, PUSH_EFFECT)
    _, _, PBE = _effects()
    f = _pinned_founder(superuser_db)
    grant = _issue(superuser_db, f, _stepup(superuser_db, f, _session(superuser_db, f), PBE[PUSH_EFFECT]), PUSH_EFFECT)
    cancel_review_publication_grant(superuser_db, user=f, grant_id=grant.id)
    with pytest.raises(ReviewPublicationError):
        claim_review_publication_grant(superuser_db, owner_id=f.id, grant_id=grant.id, effect=PUSH_EFFECT,
                                       artifact_root=Path("/nonexistent"), observed_base_sha="c" * 40,
                                       observed_head_sha=None)


def test_PUB6_stop_blocks_claim_before_token_mint(superuser_db):
    from app.founder_authority.review_publication import (
        claim_review_publication_grant, ReviewPublicationError, PUSH_EFFECT)
    from app.workforce.kill_switch import activate_kill_switch
    _, _, PBE = _effects()
    f = _pinned_founder(superuser_db)
    grant = _issue(superuser_db, f, _stepup(superuser_db, f, _session(superuser_db, f), PBE[PUSH_EFFECT]), PUSH_EFFECT)
    activate_kill_switch(superuser_db, owner_id=f.id, reason="founder stop before publish")
    with pytest.raises(ReviewPublicationError) as ei:
        claim_review_publication_grant(superuser_db, owner_id=f.id, grant_id=grant.id, effect=PUSH_EFFECT,
                                       artifact_root=Path("/nonexistent"), observed_base_sha="c" * 40,
                                       observed_head_sha=None)
    assert "stop" in str(ei.value).lower()


# ---------------------------------------------------------------------------
# PUB7 — artifact bundle bytes must match the signed manifest digest
# ---------------------------------------------------------------------------

def test_PUB7_artifact_bundle_binding(tmp_path):
    from app.founder_authority.review_publication import inspect_review_artifact, ReviewPublicationError
    root = tmp_path / "review"; root.mkdir()
    (root / "x.bundle").write_bytes(b"not-the-real-bundle")
    manifest = {"schema_version": "lifeai.review.v1", "source_repository_identity": REPO,
                "candidate_ref": "refs/heads/x", "candidate_sha": SHA_A, "candidate_tree_sha": "b" * 40,
                "base_sha": "c" * 40, "bundle_file": "x.bundle", "bundle_sha256": "f" * 64,  # wrong digest
                "manifest_sha256": "ignored", "changed_files": [], "evidence_files": []}
    (root / "x.json").write_text(json.dumps(manifest))
    with pytest.raises(ReviewPublicationError):   # bundle bytes != signed manifest digest
        inspect_review_artifact(artifact_root=root, bundle_relative_path="x.bundle", manifest_relative_path="x.json")
    with pytest.raises(Exception):                # path escape must not read outside the review root
        inspect_review_artifact(artifact_root=root, bundle_relative_path="../x.bundle", manifest_relative_path="x.json")


# ---------------------------------------------------------------------------
# PUB8 — transport least privilege: the installation token may carry no unrelated write scope
# ---------------------------------------------------------------------------

def _lease(grant, permissions, *, effect="push_branch", repo=REPO, app_id="app-1", inst=12345):
    from app.mainai_execution.remote_effect import InstallationTokenLease
    return InstallationTokenLease(repository=repo, effect=effect, app_id=app_id, installation_id=inst,
                                  account_login="d1n095", permissions=permissions,
                                  expires_at=datetime.utcnow() + timedelta(minutes=5), token="ghs_FAKE_TEST_TOKEN")


def test_PUB8_transport_lease_least_privilege(superuser_db):
    from app.founder_authority import review_transport as rt
    from app.founder_authority.review_publication import ReviewPublicationError, PUSH_EFFECT
    _, _, PBE = _effects()
    f = _pinned_founder(superuser_db)
    grant = _issue(superuser_db, f, _stepup(superuser_db, f, _session(superuser_db, f), PBE[PUSH_EFFECT]), PUSH_EFFECT)
    # exact least-privilege lease for a PUSH grant is accepted
    rt._assert_lease(grant, _lease(grant, {"contents": "write"}), transport_effect="push_branch")
    # an extra unrelated write scope is rejected
    with pytest.raises(ReviewPublicationError):
        rt._assert_lease(grant, _lease(grant, {"contents": "write", "administration": "write"}),
                         transport_effect="push_branch")
    # wrong repository is rejected
    with pytest.raises(ReviewPublicationError):
        rt._assert_lease(grant, _lease(grant, {"contents": "write"}, repo="attacker/evil"),
                         transport_effect="push_branch")
    # effect mismatch (PUSH grant, open_pr token) is rejected
    with pytest.raises(ReviewPublicationError):
        rt._assert_lease(grant, _lease(grant, {"contents": "write"}, effect="open_pr"),
                         transport_effect="open_pr")


def test_PUB9_lease_token_excluded_from_repr(superuser_db):
    """Credential protection: the short-lived token must not appear in repr/logs."""
    f = _pinned_founder(superuser_db)
    lease = _lease(None, {"contents": "write"})
    assert "ghs_FAKE_TEST_TOKEN" not in repr(lease)
