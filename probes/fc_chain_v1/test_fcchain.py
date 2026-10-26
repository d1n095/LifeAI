"""Priority 2 — full authority-chain composition (publication + STOP-clear + FA-PRIV).

Subject: FA-PRIV tip 078312d7 (contains all three subsystems + the 7fbe6771 publication boundary).
Targets composition/concurrency risks NOT covered by fc_kernel_v1 (7) or fc_pub/fc_pubexec (publication).
Read-only; mocked external effects; no network/credentials. first_cycle_go=false.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text

from _guards import egress  # noqa: F401
from _harness import _privileges, owner_id  # noqa: F401

FOUNDER_PW = "CorrectHorseBattery9!"
REPO = "d1n095/LifeAI"
PREFIX = "review/"


def _pinned_founder(sdb):
    from app.founder import FOUNDER_USER_ID
    from app.models.user import User
    from app.security import hash_password, utcnow_seconds_baseline
    now = utcnow_seconds_baseline()
    u = sdb.get(User, FOUNDER_USER_ID)
    if u is None:
        u = User(id=FOUNDER_USER_ID, email="founder@lifeos.local", password_hash=hash_password(FOUNDER_PW),
                 role="founder", email_verified=True, email_verified_at=now, sessions_valid_after=now)
        sdb.add(u); sdb.commit()
    return u


def _session(sdb, user):
    from app.models.refresh_token import RefreshToken
    jti = str(uuid.uuid4())
    sdb.add(RefreshToken(id=uuid.uuid4(), user_id=user.id, family_id=uuid.uuid4(),
                         token_hash=hashlib.sha256(jti.encode()).hexdigest(), access_jti=jti,
                         csrf_token=uuid.uuid4().hex, expires_at=datetime.utcnow() + timedelta(hours=1)))
    sdb.commit()
    return jti


def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def _real_artifact(tmp: Path):
    from app.founder_authority.review_publication import VerifiedReviewArtifact
    src = tmp / "src"; src.mkdir(parents=True)
    _git(src, "init", "-q"); _git(src, "config", "user.email", "e@x.com"); _git(src, "config", "user.name", "e")
    (src / "a.txt").write_text("base\n"); _git(src, "add", "a.txt"); _git(src, "commit", "-q", "-m", "b")
    base = _git(src, "rev-parse", "HEAD")
    (src / "a.txt").write_text("cand\n"); _git(src, "add", "a.txt"); _git(src, "commit", "-q", "-m", "c")
    cand = _git(src, "rev-parse", "HEAD"); tree = _git(src, "rev-parse", "HEAD^{tree}")
    br = _git(src, "symbolic-ref", "--short", "HEAD")
    root = tmp / "review"; root.mkdir(); _git(src, "bundle", "create", str(root / "x.bundle"), br)
    bsha = hashlib.sha256((root / "x.bundle").read_bytes()).hexdigest()
    man = {"schema_version": 1, "source_repository_identity": REPO, "candidate_ref": f"refs/heads/{br}",
           "candidate_sha": cand, "candidate_tree_sha": tree, "base_sha": base, "bundle_file": "x.bundle",
           "bundle_sha256": bsha, "changed_files": ["a.txt"], "evidence_files": []}
    raw = json.dumps(man).encode(); (root / "x.json").write_bytes(raw)
    art = VerifiedReviewArtifact(source_repository_identity=REPO, candidate_ref=f"refs/heads/{br}",
                                 candidate_sha=cand, candidate_tree_sha=tree, base_sha=base, bundle_sha256=bsha,
                                 manifest_sha256=hashlib.sha256(raw).hexdigest(), bundle_relative_path="x.bundle",
                                 manifest_relative_path="x.json", changed_files=("a.txt",), evidence_files=())
    return art, root, base


def _issue(sdb, f, art):
    from app.founder_authority.service import create_step_up_receipt
    from app.founder_authority.review_publication import issue_review_publication_grant, PURPOSE_BY_EFFECT, PUSH_EFFECT
    jti = _session(sdb, f)
    rec = create_step_up_receipt(sdb, user=f, password=FOUNDER_PW, access_jti=jti, purpose=PURPOSE_BY_EFFECT[PUSH_EFFECT])
    return issue_review_publication_grant(sdb, user=f, step_up_receipt_id=rec.id, effect=PUSH_EFFECT, artifact=art,
                                          destination_repository_identity=REPO, destination_branch="review/c1",
                                          base_branch="main", installation_id=1, github_app_id="a",
                                          expires_at=datetime.utcnow() + timedelta(minutes=4),
                                          idempotency_key=f"i-{uuid.uuid4()}", branch_prefix=PREFIX,
                                          protected_branches={"main"})


def _owner_epoch(sdb, owner) -> int:
    row = sdb.execute(sa_text("SELECT epoch FROM workforce_authority_epoch WHERE scope_key=:k"),
                      {"k": str(owner)}).mappings().first()
    return int(row["epoch"]) if row else -1


# ---------------------------------------------------------------------------
# CH1 — a STOP blocks the publication claim on the COMPOSED FA-PRIV tip (cross-boundary fence)
# ---------------------------------------------------------------------------

def test_CH1_stop_fence_blocks_publication_on_composed_tip(superuser_db, tmp_path):
    from app.founder_authority.review_publication import claim_review_publication_grant, ReviewPublicationError, PUSH_EFFECT
    from app.workforce.kill_switch import activate_kill_switch
    f = _pinned_founder(superuser_db)
    art, root, base = _real_artifact(tmp_path)
    grant = _issue(superuser_db, f, art)
    activate_kill_switch(superuser_db, owner_id=f.id, reason="stop")
    with pytest.raises(ReviewPublicationError) as ei:
        claim_review_publication_grant(superuser_db, owner_id=f.id, grant_id=grant.id, effect=PUSH_EFFECT,
                                       artifact_root=root, observed_base_sha=base, observed_head_sha=None)
    assert "stop" in str(ei.value).lower()   # publication honors the FA-PRIV/kernel STOP fence post-rework


# ---------------------------------------------------------------------------
# CH3 — uniform FA-PRIV runtime write-denial across ALL THREE authority tables
# ---------------------------------------------------------------------------

def test_CH3_runtime_cannot_write_any_authority_table(db_session):
    from sqlalchemy.exc import ProgrammingError, InternalError
    for stmt in (
        "UPDATE workforce_authority_epoch SET stopped=false WHERE scope_key='GLOBAL'",
        "DELETE FROM founder_stop_clear_authorizations",
        "DELETE FROM founder_review_publication_grants",
    ):
        with pytest.raises((ProgrammingError, InternalError)) as ei:
            db_session.execute(sa_text(stmt)); db_session.flush()
        assert "denied" in str(ei.value).lower(), stmt
        db_session.rollback()


# ---------------------------------------------------------------------------
# CH4 — STOP-clear is epoch-bound: a stale expected_epoch is rejected (untested by fc_kernel)
# ---------------------------------------------------------------------------

def test_CH4_stop_clear_rejects_stale_epoch(superuser_db):
    from app.workforce.kill_switch import activate_kill_switch
    from app.founder_authority.stop_clear import issue_stop_clear_authorization, StopClearError
    f = _pinned_founder(superuser_db); jti = _session(superuser_db, f)
    activate_kill_switch(superuser_db, owner_id=f.id, reason="stop")
    current = _owner_epoch(superuser_db, f.id)
    with pytest.raises(StopClearError):   # wrong (stale) epoch cannot authorize a clear
        issue_stop_clear_authorization(superuser_db, user=f, password=FOUNDER_PW, access_jti=jti,
                                       scope="owner", target_owner_id=f.id, expected_epoch=current - 1,
                                       operation_id=uuid.uuid4())


# ---------------------------------------------------------------------------
# CH5 — the authority epoch is monotonic across STOP cycles (replay resistance)
# ---------------------------------------------------------------------------

def test_CH5_epoch_is_monotonic_across_stop_cycles(superuser_db):
    from app.workforce.kill_switch import activate_kill_switch
    f = _pinned_founder(superuser_db)
    activate_kill_switch(superuser_db, owner_id=f.id, reason="stop-1")
    e1 = _owner_epoch(superuser_db, f.id)
    activate_kill_switch(superuser_db, owner_id=f.id, reason="stop-2")
    e2 = _owner_epoch(superuser_db, f.id)
    assert e2 > e1, (e1, e2)   # generation number strictly increases; a stale epoch can never be replayed


# ---------------------------------------------------------------------------
# CH2 — credential isolation: the two subsystems do not share a one proof
# ---------------------------------------------------------------------------

def test_CH2_review_stepup_cannot_authorize_stop_clear(superuser_db):
    """A review-publication step-up receipt is not a STOP-clear authorization: STOP-clear consumes a
    FounderStopClearAuthorization id, never a review step-up receipt — no cross-subsystem proof reuse."""
    from app.founder_authority.service import create_step_up_receipt
    from app.founder_authority.review_publication import PURPOSE_BY_EFFECT, PUSH_EFFECT
    from app.founder_authority.stop_clear import clear_stop_with_authorization, StopClearError
    f = _pinned_founder(superuser_db); jti = _session(superuser_db, f)
    review_receipt = create_step_up_receipt(superuser_db, user=f, password=FOUNDER_PW, access_jti=jti,
                                            purpose=PURPOSE_BY_EFFECT[PUSH_EFFECT])
    # feeding the review receipt id where STOP-clear expects its own authorization id must be rejected
    with pytest.raises(StopClearError):
        clear_stop_with_authorization(superuser_db, user=f, authorization_id=review_receipt.id,
                                      operation_id=uuid.uuid4(), scope="owner", target_owner_id=f.id,
                                      expected_epoch=0, access_jti=jti)
