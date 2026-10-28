"""Shared helpers for the Transport Database Authority examination.

Target: Codex candidate head `460e8ad8cbd40b7fb0d62702f28c14412688e298`. Subject: the DB
authority for push and draft-PR transport effects:

  * mainai_push_grants       — guard restrict_push_grant_mutation (migration 0090); partial
                               unique index one-active-per-task; uq_preflight_once (0093);
                               consume column grant (status,consumed_at,consumed_by_job_id).
  * mainai_draft_pr_grants   — guard restrict_draft_pr_grant_mutation (migration 0091).
  * mainai_remote_effect_attempts — append-only receipt of the actual remote effect, guard
                               restrict_remote_effect_attempt_mutation (0091): the DB record
                               of committed authorization -> remote effect.

The authority graph below is a faithful replica of the product's own
`test_activation_authority_substrate.py::authority_graph` fixture, so every probe runs against
a LEGITIMATELY constructed grant and exercises the real triggers/constraints/indexes/RLS.
Setup is done through the schema-owner session (superuser_db). Read-only examiner; loopback
only; no paid calls.
"""
from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import text

REPO = "d1n095/LifeAI"
BASE = "a" * 40
LOCAL = "b" * 40
ACTIVATION = "c" * 40
INTEGRATION = "mainai/integration-founder-supervised-v1"
CHECKS = [{"name": "backend", "app_slug": "github-actions"},
          {"name": "npm-audit", "app_slug": "github-actions"}]

PUSH_TABLE = "mainai_push_grants"
DRAFT_TABLE = "mainai_draft_pr_grants"
REMOTE_TABLE = "mainai_remote_effect_attempts"


def set_runtime_owner(db, owner_id) -> None:
    from app.request_context import current_user_id

    current_user_id.set(str(owner_id))
    db.execute(text("SET LOCAL app.current_user_id=:owner"), {"owner": str(owner_id)})


def _signed_attestations(now: datetime):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    common = {
        "repository_identity": REPO, "activation_sha": ACTIVATION,
        "integration_branch": INTEGRATION, "base_branch": INTEGRATION, "base_sha": BASE,
        "authority_version": 7, "issued_at": now.isoformat() + "Z",
        "expires_at": (now + timedelta(minutes=10)).isoformat() + "Z",
    }
    result = {}
    for kind in ("runtime_token", "ruleset", "founder_authorization"):
        payload = {**common, "kind": kind}
        if kind == "runtime_token":
            payload["token_identity"] = "github-app:1"
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        result[kind] = json.dumps({
            "key_id": "test-founder-key", "payload": payload,
            "signature": base64.b64encode(key.sign(canonical.encode())).decode(),
        })
    return public, result


def build_authority_graph(superuser_db, make_verified_user):
    """Replica of the product's authority_graph: owner -> goal -> envelope -> spend -> plan
    -> task/job/worktree -> signed preflight receipt. Returns a dict of the pieces."""
    from app.execution_envelopes.service import authorize_execution_scope, propose_execution_scope
    from app.mainai_execution import planner
    from app.mainai_execution.activation_security import (
        AuthoritativePreflightFacts,
        create_preflight_receipt,
    )
    from app.mainai_execution.planner import PlannedTaskSpec
    from app.models.mainai_execution import MainAITask, MainAITaskEvent, MainAITaskEventType
    from app.models.mainai_job import MainAIJob, MainAIJobStatus
    from app.models.mainai_recovery import MainAITaskWorktree, MainAITaskWorktreeStatus
    from app.provider_spend.service import authorize_provider_spend
    from app.request_context import current_user_id as current_user_id_var

    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    superuser_db.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    goal = planner.create_goal(superuser_db, owner_id=user.id, title="transport",
                               original_instruction="safe edit", created_by="founder")
    proposal = propose_execution_scope(superuser_db, owner_id=user.id, goal_id=goal.id,
                                       idempotency_key=f"proposal:{uuid.uuid4()}",
                                       repository_identity=REPO)
    _, envelope = authorize_execution_scope(
        superuser_db, owner_id=user.id, proposal_id=proposal.id, authorized_by="founder",
        authorized_paths=["backend/app/"], authorized_capabilities=["path_a", "repo_edit", "push"],
        authorized_risk="low", envelope_idempotency_key=f"envelope:{uuid.uuid4()}",
        repository_identity=REPO, base_branch=INTEGRATION, approved_base_sha=BASE,
        required_check_contexts=CHECKS, authority_version=7,
    )
    spend = authorize_provider_spend(
        superuser_db, owner_id=user.id, goal_id=goal.id, execution_envelope_id=envelope.id,
        authorized_by="founder", max_cost_usd=Decimal("1"), max_requests=1,
        max_cost_per_request_usd=Decimal("1"), allowed_providers=["openai"],
        allowed_models=["gpt-4o-mini"], expires_at=datetime.utcnow() + timedelta(hours=1),
        idempotency_key=f"spend:{uuid.uuid4()}",
    )
    planner.create_plan(superuser_db, goal=goal, rationale="one task", created_by="founder",
                        tasks=[PlannedTaskSpec(description="edit", task_type="repo_edit",
                               target_files=["backend/app/activation_target.py"],
                               verification_plan=[{"kind": "targeted_tests",
                                                   "target": "tests/backend/test_x.py"}])])
    task = superuser_db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    now = datetime.utcnow()
    job = MainAIJob(owner_id=user.id, job_type="task_execution", status=MainAIJobStatus.completed,
                    started_at=now, completed_at=now, idempotency_key=f"job:{uuid.uuid4()}",
                    created_by="founder")
    superuser_db.add(job)
    superuser_db.flush()
    task.mainai_job_id = job.id
    worktree = MainAITaskWorktree(
        task_id=task.id, owner_id=user.id, job_id=job.id, lease_generation=1,
        executor_id="test", repo=REPO, base_sha=BASE, branch=f"claude/mainai-task-{task.id}",
        path="/tmp/not-used", marker_token="marker", current_commit=LOCAL,
        status=MainAITaskWorktreeStatus.active,
    )
    superuser_db.add_all([worktree, MainAITaskEvent(
        task_id=task.id, owner_id=user.id, event_type=MainAITaskEventType.verification_passed,
        detail={"work_result": {"commit_sha": LOCAL}})])
    superuser_db.flush()
    public_key, attestations = _signed_attestations(now)
    facts = AuthoritativePreflightFacts(
        activation_sha=ACTIVATION, repository_identity=REPO, integration_branch=INTEGRATION,
        observed_base_sha=BASE, runtime_config={"write": True, "auto_merge": False},
        runtime_token_identity="github-app:1", runtime_token_attestation_digest="1" * 64,
        ruleset_attestation_digest="2" * 64, founder_attestation_digest="3" * 64,
        negative_authority_confirmed=True, running_code_sha=ACTIVATION,
        signed_attestations=attestations, trusted_attestation_public_key_pem=public_key,
    )
    receipt = create_preflight_receipt(
        superuser_db, owner_id=user.id, goal_id=goal.id, execution_envelope_id=envelope.id,
        provider_spend_authorization_id=spend.id, expected_repository_identity=REPO,
        expected_integration_branch=INTEGRATION, expected_base_branch=INTEGRATION,
        expected_base_sha=BASE, expected_activation_sha=ACTIVATION, expected_authority_version=7,
        facts=facts, expires_at=datetime.utcnow() + timedelta(minutes=10), founder_authorized=True,
    )
    superuser_db.commit()
    return {
        "user": user, "goal": goal, "envelope": envelope, "task": task, "job": job,
        "worktree": worktree, "receipt": receipt, "spend": spend,
    }


def make_push_grant(superuser_db, g, **changes):
    from app.mainai_execution.activation_security import create_push_grant
    from app.mainai_execution.publication_guard import PublicationManifest

    values = dict(
        owner_id=g["user"].id, goal_id=g["goal"].id, task_id=g["task"].id, job_id=g["job"].id,
        worktree_id=g["worktree"].id, execution_envelope_id=g["envelope"].id,
        preflight_receipt_id=g["receipt"].id, repository_identity=REPO,
        branch=g["worktree"].branch, base_branch=INTEGRATION, base_sha=BASE, local_commit_sha=LOCAL,
        authority_version=7, expires_at=datetime.utcnow() + timedelta(minutes=5),
        idempotency_key=f"push:{uuid.uuid4()}", authorized_by=g["user"].email, founder_authorized=True,
        publication_manifest_resolver=lambda *_: PublicationManifest(
            BASE, LOCAL, "c" * 40, ("backend/app/activation_target.py",), "d" * 64),
    )
    values.update(changes)
    grant = create_push_grant(superuser_db, **values)
    superuser_db.commit()
    return grant


def make_draft_pr_grant(superuser_db, g, **changes):
    from app.mainai_execution.activation_security import create_draft_pr_grant

    values = dict(
        owner_id=g["user"].id, goal_id=g["goal"].id, task_id=g["task"].id, job_id=g["job"].id,
        preflight_receipt_id=g["receipt"].id, repository_identity=REPO,
        head_branch=g["worktree"].branch, base_branch=INTEGRATION, head_sha=LOCAL,
        authority_version=7, expires_at=datetime.utcnow() + timedelta(minutes=5),
        idempotency_key=f"draft:{uuid.uuid4()}", authorized_by=g["user"].email, founder_authorized=True,
    )
    values.update(changes)
    grant = create_draft_pr_grant(superuser_db, **values)
    superuser_db.commit()
    return grant


def push_status(superuser_db, grant_id) -> str:
    return superuser_db.execute(
        text(f"SELECT status FROM {PUSH_TABLE} WHERE id=:id"), {"id": grant_id},
    ).scalar_one()


# ===========================================================================
# Cross-boundary extensions: STOP/clear epoch lifecycle, effect-status readers, and a
# Founder session + account-erasure reauth chain (for the session-staleness crossing).
# ===========================================================================
from datetime import timedelta as _timedelta  # noqa: E402

SPEND_TABLE = "provider_spend_authorizations"
ENVELOPE_TABLE = "execution_authorization_envelopes"
DRAFT_GRANT_TABLE = "mainai_draft_pr_grants"


def _init_owner_epoch(superuser_db, owner_id):
    superuser_db.execute(text(
        "INSERT INTO workforce_authority_epoch(scope_key,owner_id,epoch,stopped) "
        "VALUES ('GLOBAL',NULL,0,false) ON CONFLICT(scope_key) DO NOTHING"))
    superuser_db.execute(text(
        "INSERT INTO workforce_authority_epoch(scope_key,owner_id,epoch,stopped) "
        "VALUES (:s,:o,0,false) ON CONFLICT(scope_key) DO NOTHING"), {"s": str(owner_id), "o": owner_id})
    superuser_db.flush()


def stop_owner(superuser_db, owner_id, reason="examiner STOP"):
    """Activate the owner's STOP via the privileged boundary (the AFTER-UPDATE cascade revokes
    the owner's active envelopes / spend / push / draft-PR grants)."""
    _init_owner_epoch(superuser_db, owner_id)
    superuser_db.execute(text(
        "UPDATE workforce_authority_epoch SET stopped=true, epoch=epoch+1, reason=:r, "
        "activated_at=clock_timestamp() WHERE scope_key=:s"), {"r": reason, "s": str(owner_id)})
    superuser_db.commit()


def clear_owner(superuser_db, owner_id):
    """Clear the owner's STOP through the privileged (schema-owner) session. The epoch guard
    that demands an authenticated STOP-clear authorization fires only for mainai_app, so this
    exercises the epoch LIFECYCLE itself; the authenticated-clear authorization is the certified
    STOP-clear control and is deliberately NOT re-tested here. The point under test is only
    whether the clear RESTORES any previously-revoked effect authority."""
    superuser_db.execute(text(
        "UPDATE workforce_authority_epoch SET stopped=false, epoch=epoch+1, "
        "reason='examiner cleared' WHERE scope_key=:s"), {"s": str(owner_id)})
    superuser_db.commit()


def envelope_status(superuser_db, envelope_id):
    return superuser_db.execute(text(f"SELECT status FROM {ENVELOPE_TABLE} WHERE id=:i"),
                                {"i": envelope_id}).scalar_one()


def spend_status(superuser_db, spend_id):
    return superuser_db.execute(text(f"SELECT status FROM {SPEND_TABLE} WHERE id=:i"),
                                {"i": spend_id}).scalar_one()


# ---- Founder session + account-erasure reauth (for the session-staleness crossing) ----
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _sha256_hex(s):
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def founder_session():
    """Ensure the Founder + a live session (refresh token with a known access_jti)."""
    import uuid as _uuid
    from datetime import datetime as _dt

    from app.db import SecurityKernelSessionLocal
    from app.founder import FOUNDER_USER_ID
    from app.models.refresh_token import RefreshToken
    from app.models.user import User, UserRole
    from app.security import hash_password

    jti = _uuid.uuid4().hex[:32]
    now = _dt.utcnow()
    with SecurityKernelSessionLocal() as adb:
        u = adb.get(User, FOUNDER_USER_ID)
        if u is None:
            adb.add(User(id=FOUNDER_USER_ID, email="founder@lifeos.local",
                         password_hash=hash_password(FOUNDER_PASSWORD), role=UserRole.founder,
                         is_active=True, email_verified=True, email_verified_at=now,
                         sessions_valid_after=now - _timedelta(seconds=2)))
        else:
            u.role = UserRole.founder; u.is_active = True
            u.sessions_valid_after = now - _timedelta(seconds=2)
        adb.flush()
        adb.add(RefreshToken(user_id=FOUNDER_USER_ID, family_id=_uuid.uuid4(),
                             token_hash=_uuid.uuid4().hex + _uuid.uuid4().hex, access_jti=jti,
                             csrf_token=_uuid.uuid4().hex, created_at=now,
                             expires_at=now + _timedelta(hours=1)))
        adb.commit()
    return FOUNDER_USER_ID, jti


def make_erase_reauth_receipt(owner_id, jti, *, ttl_seconds=240):
    from app.account.reauth import create_account_erasure_reauth_receipt
    from app.db import SecurityKernelSessionLocal
    from app.models.user import User

    with SecurityKernelSessionLocal() as adb:
        adb.execute(text("SET LOCAL app.current_user_id=:o"), {"o": str(owner_id)})
        adb.execute(text("SET LOCAL app.current_access_jti=:j"), {"j": jti})
        user = adb.get(User, owner_id)
        r = create_account_erasure_reauth_receipt(adb, user=user, password=FOUNDER_PASSWORD,
                                                  access_jti=jti, ttl_seconds=ttl_seconds)
        adb.commit()
    return r.receipt_id


def reset_founder_password(db):
    """Invalidate the Founder's current sessions via the privileged reset function (bumps
    sessions_valid_after + revokes refresh tokens). Returns nothing."""
    import uuid as _uuid
    from datetime import datetime as _dt

    from app.db import SecurityKernelSessionLocal
    from app.founder import FOUNDER_USER_ID
    from app.security import hash_password

    plaintext = "z" * 40
    with SecurityKernelSessionLocal() as adb:
        adb.execute(text(
            "INSERT INTO password_reset_tokens (id, user_id, token_hash, created_at, expires_at) "
            "VALUES (:id,:u,:th, now(), :exp)"),
            {"id": _uuid.uuid4(), "u": FOUNDER_USER_ID, "th": _sha256_hex(plaintext),
             "exp": _dt.utcnow() + _timedelta(seconds=300)})
        adb.commit()
    db.execute(text("SELECT reset_founder_password_with_token(:t,:h)"),
               {"t": plaintext, "h": hash_password(FOUNDER_PASSWORD)})
    db.commit()


def erase_begin(db, owner_id, receipt_id, jti):
    db.execute(text("SET LOCAL app.current_user_id=:o"), {"o": str(owner_id)})
    db.execute(text("SET LOCAL app.current_access_jti=:j"), {"j": jti})
    return db.execute(text("SELECT account_erasure_begin_operation(:o,:r)"),
                      {"o": str(owner_id), "r": str(receipt_id)}).scalar_one()
