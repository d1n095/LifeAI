"""Full publication authorization+execution sequence — executable adversarial tests (Priority 1).

Subject: security base 7fbe6771 (Codex verified bundle). Drives the REAL
claim_review_publication_grant + execute_review_publication with a local git-bundle artifact and
FULLY MOCKED external effects (fake observer / token provider / push / PR transport). No real GitHub,
no credentials, no network. Converts the previously code-reviewed claim/execute controls to executed.
first_cycle_go=false.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

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


def _real_artifact(tmp: Path, repo=REPO):
    """Build a real git repo + bundle + schema-v1 manifest; return (artifact, artifact_root)."""
    from app.founder_authority.review_publication import VerifiedReviewArtifact
    src = tmp / "src"; src.mkdir(parents=True)
    _git(src, "init", "-q"); _git(src, "config", "user.email", "e@x.com"); _git(src, "config", "user.name", "e")
    (src / "a.txt").write_text("base\n"); _git(src, "add", "a.txt"); _git(src, "commit", "-q", "-m", "base")
    base_sha = _git(src, "rev-parse", "HEAD")
    (src / "a.txt").write_text("candidate\n"); _git(src, "add", "a.txt"); _git(src, "commit", "-q", "-m", "cand")
    cand_sha = _git(src, "rev-parse", "HEAD")
    cand_tree = _git(src, "rev-parse", "HEAD^{tree}")
    branch = _git(src, "symbolic-ref", "--short", "HEAD")
    root = tmp / "review"; root.mkdir()
    _git(src, "bundle", "create", str(root / "x.bundle"), branch)
    bundle_sha = hashlib.sha256((root / "x.bundle").read_bytes()).hexdigest()
    manifest = {"schema_version": 1, "source_repository_identity": repo,
                "candidate_ref": f"refs/heads/{branch}", "candidate_sha": cand_sha,
                "candidate_tree_sha": cand_tree, "base_sha": base_sha, "bundle_file": "x.bundle",
                "bundle_sha256": bundle_sha, "changed_files": ["a.txt"], "evidence_files": []}
    raw = json.dumps(manifest).encode(); (root / "x.json").write_bytes(raw)
    art = VerifiedReviewArtifact(
        source_repository_identity=repo, candidate_ref=f"refs/heads/{branch}", candidate_sha=cand_sha,
        candidate_tree_sha=cand_tree, base_sha=base_sha, bundle_sha256=bundle_sha,
        manifest_sha256=hashlib.sha256(raw).hexdigest(), bundle_relative_path="x.bundle",
        manifest_relative_path="x.json", changed_files=("a.txt",), evidence_files=())
    return art, root, base_sha, cand_sha


def _issue(sdb, founder, art, effect, *, dest="review/cand-1", ttl_min=4):
    from app.founder_authority.service import create_step_up_receipt
    from app.founder_authority.review_publication import issue_review_publication_grant, PURPOSE_BY_EFFECT
    jti = _session(sdb, founder)
    receipt = create_step_up_receipt(sdb, user=founder, password=FOUNDER_PW, access_jti=jti, purpose=PURPOSE_BY_EFFECT[effect])
    return issue_review_publication_grant(
        sdb, user=founder, step_up_receipt_id=receipt.id, effect=effect, artifact=art,
        destination_repository_identity=REPO, destination_branch=dest, base_branch="main",
        installation_id=12345, github_app_id="app-1", expires_at=datetime.utcnow() + timedelta(minutes=ttl_min),
        idempotency_key=f"idem-{uuid.uuid4()}", branch_prefix=PREFIX, protected_branches={"main", "master"})


# ---- fakes (all external effects mocked) --------------------------------------------------------

def _lease(grant, perms=None, effect=None, token="ghs_FAKE"):
    from app.mainai_execution.remote_effect import InstallationTokenLease
    from app.founder_authority.review_publication import PUSH_EFFECT
    eff = effect or ("push_branch" if grant.effect == PUSH_EFFECT else "open_pr")
    return InstallationTokenLease(repository=grant.destination_repository_identity, effect=eff,
                                  app_id=grant.github_app_id, installation_id=grant.installation_id,
                                  account_login="d1n095", permissions=perms or {"contents": "write"},
                                  expires_at=grant.expires_at, token=token)


class FakeProvider:
    def __init__(self, sdb, grant_id, lease_perms=None, lease_effect=None):
        self.sdb, self.grant_id = sdb, grant_id
        self.lease_perms, self.lease_effect = lease_perms, lease_effect
        self.mint_calls = 0; self.revoke_calls = 0; self.consumed_at_mint = None
    async def mint(self, *, repository, effect, expires_before):
        self.mint_calls += 1
        from app.models.founder_authority import FounderReviewPublicationGrant
        g = self.sdb.get(FounderReviewPublicationGrant, self.grant_id)
        self.consumed_at_mint = (g.status == "consumed")   # token minted only after the grant is consumed+committed
        return _lease(g, self.lease_perms, self.lease_effect)
    async def revoke(self, token):
        self.revoke_calls += 1


class FakeObserver:
    def __init__(self, base_sha, head_sha=None, cand_sha=None):
        self.base_sha, self._head, self.cand_sha = base_sha, head_sha, cand_sha
        self.pushed = False
    async def get_ref(self, repo, ref):
        if ref == "main":
            return self.base_sha
        return self.cand_sha if self.pushed else self._head
    async def get_pull_request(self, repo, number):
        return {"draft": True, "head": {"sha": self.cand_sha, "ref": "review/cand-1"}, "base": {"ref": "main"}}


def _mk_push(observer, log):
    async def _push(*, artifact_root, artifact, repository, branch, token):
        log.append(("push", repository, branch)); observer.pushed = True
    return _push


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# PE1/PE2/PE7 — happy PUSH: token minted only AFTER commit; exactly one effect; audit
# ---------------------------------------------------------------------------

def test_PE1_push_success_token_after_commit_single_effect(superuser_db, tmp_path):
    from app.founder_authority.review_transport import execute_review_publication, PUSH_EFFECT
    f = _pinned_founder(superuser_db)
    art, root, base, cand = _real_artifact(tmp_path)
    grant = _issue(superuser_db, f, art, PUSH_EFFECT)
    prov = FakeProvider(superuser_db, grant.id)
    obs = FakeObserver(base_sha=base, head_sha=None, cand_sha=cand)
    log = []
    res = _run(execute_review_publication(superuser_db, owner_id=f.id, grant_id=grant.id, artifact_root=root,
                                          observer=obs, token_provider=prov, push_transport=_mk_push(obs, log)))
    assert res["outcome"] == "observed_succeeded" and res["observed_sha"] == cand
    assert prov.mint_calls == 1 and prov.consumed_at_mint is True   # token minted AFTER grant consumed+committed
    assert log == [("push", REPO, "review/cand-1")]                 # exactly one remote effect


def test_PE3_replayed_claim_mints_no_token_no_second_effect(superuser_db, tmp_path):
    from app.founder_authority.review_transport import execute_review_publication, PUSH_EFFECT
    from app.founder_authority.review_publication import ReviewPublicationError
    f = _pinned_founder(superuser_db)
    art, root, base, cand = _real_artifact(tmp_path)
    grant = _issue(superuser_db, f, art, PUSH_EFFECT)
    obs = FakeObserver(base_sha=base, head_sha=None, cand_sha=cand); log = []
    prov1 = FakeProvider(superuser_db, grant.id)
    _run(execute_review_publication(superuser_db, owner_id=f.id, grant_id=grant.id, artifact_root=root,
                                    observer=obs, token_provider=prov1, push_transport=_mk_push(obs, log)))
    # replay the SAME consumed grant
    prov2 = FakeProvider(superuser_db, grant.id); log2 = []
    obs2 = FakeObserver(base_sha=base, head_sha=cand, cand_sha=cand)
    with pytest.raises(ReviewPublicationError):
        _run(execute_review_publication(superuser_db, owner_id=f.id, grant_id=grant.id, artifact_root=root,
                                        observer=obs2, token_provider=prov2, push_transport=_mk_push(obs2, log2)))
    assert prov2.mint_calls == 0 and log2 == []     # no token, no second effect on a consumed grant


def test_PE4_base_moved_blocks_before_mint(superuser_db, tmp_path):
    from app.founder_authority.review_transport import execute_review_publication, PUSH_EFFECT
    from app.founder_authority.review_publication import ReviewPublicationError
    f = _pinned_founder(superuser_db)
    art, root, base, cand = _real_artifact(tmp_path)
    grant = _issue(superuser_db, f, art, PUSH_EFFECT)
    prov = FakeProvider(superuser_db, grant.id); log = []
    obs = FakeObserver(base_sha="9" * 40, head_sha=None, cand_sha=cand)   # base moved since authorization
    with pytest.raises(ReviewPublicationError):
        _run(execute_review_publication(superuser_db, owner_id=f.id, grant_id=grant.id, artifact_root=root,
                                        observer=obs, token_provider=prov, push_transport=_mk_push(obs, log)))
    assert prov.mint_calls == 0 and log == []


def test_PE5_existing_branch_blocks_push_before_mint(superuser_db, tmp_path):
    from app.founder_authority.review_transport import execute_review_publication, PUSH_EFFECT
    from app.founder_authority.review_publication import ReviewPublicationError
    f = _pinned_founder(superuser_db)
    art, root, base, cand = _real_artifact(tmp_path)
    grant = _issue(superuser_db, f, art, PUSH_EFFECT)
    prov = FakeProvider(superuser_db, grant.id); log = []
    obs = FakeObserver(base_sha=base, head_sha="dead" + "0" * 36, cand_sha=cand)  # branch already exists
    with pytest.raises(ReviewPublicationError):
        _run(execute_review_publication(superuser_db, owner_id=f.id, grant_id=grant.id, artifact_root=root,
                                        observer=obs, token_provider=prov, push_transport=_mk_push(obs, log)))
    assert prov.mint_calls == 0 and log == []


def test_PE6_bundle_substitution_blocks_before_mint(superuser_db, tmp_path):
    from app.founder_authority.review_transport import execute_review_publication, PUSH_EFFECT
    from app.founder_authority.review_publication import ReviewPublicationError
    f = _pinned_founder(superuser_db)
    art, root, base, cand = _real_artifact(tmp_path)
    grant = _issue(superuser_db, f, art, PUSH_EFFECT)
    (root / "x.bundle").write_bytes(b"substituted-bundle-bytes")   # tamper after authorization
    prov = FakeProvider(superuser_db, grant.id); log = []
    obs = FakeObserver(base_sha=base, head_sha=None, cand_sha=cand)
    with pytest.raises(ReviewPublicationError):
        _run(execute_review_publication(superuser_db, owner_id=f.id, grant_id=grant.id, artifact_root=root,
                                        observer=obs, token_provider=prov, push_transport=_mk_push(obs, log)))
    assert prov.mint_calls == 0 and log == []


def test_PE7_concurrent_oneuse_grant_single_consumption(superuser_db, tmp_path):
    from app.founder_authority.review_publication import claim_review_publication_grant, ReviewPublicationError, PUSH_EFFECT
    f = _pinned_founder(superuser_db)
    art, root, base, cand = _real_artifact(tmp_path)
    grant = _issue(superuser_db, f, art, PUSH_EFFECT)
    c1 = claim_review_publication_grant(superuser_db, owner_id=f.id, grant_id=grant.id, effect=PUSH_EFFECT,
                                        artifact_root=root, observed_base_sha=base, observed_head_sha=None)
    assert c1[0].status == "consumed"
    with pytest.raises(ReviewPublicationError):   # second claim of the one-use grant
        claim_review_publication_grant(superuser_db, owner_id=f.id, grant_id=grant.id, effect=PUSH_EFFECT,
                                       artifact_root=root, observed_base_sha=base, observed_head_sha=None)


def test_PE8_stop_between_issue_and_execute_blocks_before_mint(superuser_db, tmp_path):
    from app.founder_authority.review_transport import execute_review_publication, PUSH_EFFECT
    from app.founder_authority.review_publication import ReviewPublicationError
    from app.workforce.kill_switch import activate_kill_switch
    f = _pinned_founder(superuser_db)
    art, root, base, cand = _real_artifact(tmp_path)
    grant = _issue(superuser_db, f, art, PUSH_EFFECT)
    activate_kill_switch(superuser_db, owner_id=f.id, reason="stop before execute")
    prov = FakeProvider(superuser_db, grant.id); log = []
    obs = FakeObserver(base_sha=base, head_sha=None, cand_sha=cand)
    with pytest.raises(ReviewPublicationError):
        _run(execute_review_publication(superuser_db, owner_id=f.id, grant_id=grant.id, artifact_root=root,
                                        observer=obs, token_provider=prov, push_transport=_mk_push(obs, log)))
    assert prov.mint_calls == 0 and log == []


def test_PE9_bad_lease_revokes_and_applies_no_effect(superuser_db, tmp_path):
    """A lease carrying unrelated write authority is rejected AFTER mint -> token revoked, NO push,
    grant stays consumed (not retried with new authority)."""
    from app.founder_authority.review_transport import execute_review_publication, PUSH_EFFECT
    from app.founder_authority.review_publication import ReviewPublicationError
    f = _pinned_founder(superuser_db)
    art, root, base, cand = _real_artifact(tmp_path)
    grant = _issue(superuser_db, f, art, PUSH_EFFECT)
    prov = FakeProvider(superuser_db, grant.id, lease_perms={"contents": "write", "administration": "write"})
    obs = FakeObserver(base_sha=base, head_sha=None, cand_sha=cand); log = []
    with pytest.raises(ReviewPublicationError):
        _run(execute_review_publication(superuser_db, owner_id=f.id, grant_id=grant.id, artifact_root=root,
                                        observer=obs, token_provider=prov, push_transport=_mk_push(obs, log)))
    assert prov.mint_calls == 1 and prov.revoke_calls == 1 and log == []   # minted, rejected, revoked, no effect


def test_PE10_audit_receipts_are_runtime_append_only(db_session, superuser_db):
    """founder_authority_audit_receipts is SELECT-only for the runtime role (append-only; privileged
    writes) — the runtime cannot delete or forge audit evidence."""
    from sqlalchemy import text as sa_text
    from sqlalchemy.exc import ProgrammingError, InternalError
    with pytest.raises((ProgrammingError, InternalError)) as ei:
        db_session.execute(sa_text("DELETE FROM founder_authority_audit_receipts"))
        db_session.flush()
    assert "denied" in str(ei.value).lower()
    db_session.rollback()
