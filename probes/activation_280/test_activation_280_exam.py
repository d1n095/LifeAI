"""Independent examiner probes for #280 (activation authority substrate, 37bca08).

Every probe asserts the SECURE expectation (spec 8a5bdaf + assignment). A failing probe is a
reproduced GAP; a passing probe is a HOLD. Nothing here performs a network effect.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import SessionLocal
from app.execution_envelopes.service import ExecutionEnvelopeError, authorize_execution_scope, propose_execution_scope
from app.founder import FOUNDER_USER_ID
from app.mainai_execution import planner
from app.mainai_execution.activation_security import (
    ActivationAuthorityError,
    AuthoritativePreflightFacts,
    PushGrantConsumed,
    PushGrantExpired,
    cancel_push_grant,
    consume_push_grant,
    create_preflight_receipt,
    create_push_grant,
)
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_activation import MainAIActivationPreflightReceipt, MainAIPushGrant
from app.models.mainai_execution import MainAITask, MainAITaskEvent, MainAITaskEventType
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.mainai_recovery import MainAITaskWorktree, MainAITaskWorktreeStatus
from app.provider_spend.service import authorize_provider_spend, revoke_provider_spend
from app.request_context import current_user_id as current_user_id_var
from tests.backend.test_account_erasure import (  # noqa: F401  (module-scoped autouse privilege fixture)
    _issue_erasure_access_session,
    _narrow_privileges_before_this_module,
)
from tests.backend.test_execution_envelopes_api import _login as _founder_login

REPO = "d1n095/LifeAI"
BASE = "a" * 40
LOCAL = "b" * 40
ACTIVATION = "c" * 40
INTEGRATION = "mainai/integration-founder-supervised-v1"
CHECKS = [{"name": "backend", "app_slug": "github-actions"}]
PASSWORD = "CorrectHorseBattery9!"


def _rls(db, owner_id):
    current_user_id_var.set(str(owner_id))
    db.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _facts(**over):
    values = dict(
        activation_sha=ACTIVATION, repository_identity=REPO, integration_branch=INTEGRATION,
        observed_base_sha=BASE, runtime_config={"write": True, "auto_merge": False},
        runtime_token_identity="github-app:1", runtime_token_attestation_digest="1" * 64,
        ruleset_attestation_digest="2" * 64, founder_attestation_digest="3" * 64,
        negative_authority_confirmed=True,
    )
    values.update(over)
    return AuthoritativePreflightFacts(**values)


def _spec():
    """#276 requires structured target_files for repo_edit; #280's base (#272) has no such field."""
    import dataclasses

    kw = dict(description="edit", task_type="repo_edit", verification_plan=[{"kind": "targeted_tests", "target": "tests/x.py"}])
    if "target_files" in {f.name for f in dataclasses.fields(PlannedTaskSpec)}:
        kw["target_files"] = ["backend/app/x.py"]
    return PlannedTaskSpec(**kw)


def _graph(db, owner_id, *, worktree_branch=INTEGRATION, spend_expires=timedelta(hours=1), receipt=True, verified_sha=LOCAL):
    _rls(db, owner_id)
    goal = planner.create_goal(db, owner_id=owner_id, title="activation", original_instruction="safe edit", created_by="founder")
    proposal = propose_execution_scope(db, owner_id=owner_id, goal_id=goal.id, idempotency_key=f"p:{uuid.uuid4()}", repository_identity=REPO)
    _, envelope = authorize_execution_scope(
        db, owner_id=owner_id, proposal_id=proposal.id, authorized_by="founder",
        authorized_paths=["backend/app/"], authorized_capabilities=["path_a", "repo_edit", "push"],
        authorized_risk="low", envelope_idempotency_key=f"e:{uuid.uuid4()}",
        repository_identity=REPO, base_branch="main", approved_base_sha=BASE,
        required_check_contexts=CHECKS, authority_version=7,
    )
    spend = authorize_provider_spend(
        db, owner_id=owner_id, goal_id=goal.id, execution_envelope_id=envelope.id, authorized_by="founder",
        max_cost_usd=Decimal("1"), max_requests=1, max_cost_per_request_usd=Decimal("1"),
        allowed_providers=["openai"], allowed_models=["gpt-4o-mini"],
        expires_at=datetime.utcnow() + spend_expires, idempotency_key=f"s:{uuid.uuid4()}",
    )
    planner.create_plan(db, goal=goal, rationale="one", created_by="founder", tasks=[_spec()])
    task = db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    now = datetime.utcnow()
    job = MainAIJob(owner_id=owner_id, job_type="task_execution", status=MainAIJobStatus.completed, started_at=now, completed_at=now, idempotency_key=f"j:{uuid.uuid4()}", created_by="founder")
    db.add(job)
    db.flush()
    task.mainai_job_id = job.id
    worktree = MainAITaskWorktree(
        task_id=task.id, owner_id=owner_id, job_id=job.id, lease_generation=1, executor_id="t", repo=REPO,
        base_sha=BASE, branch=worktree_branch, path="/nonexistent/examiner-280", marker_token="m",
        current_commit=LOCAL, status=MainAITaskWorktreeStatus.active,
    )
    db.add(worktree)
    if verified_sha is not None:
        db.add(MainAITaskEvent(task_id=task.id, owner_id=owner_id, event_type=MainAITaskEventType.verification_passed, detail={"sha": verified_sha}))
    db.flush()
    rec = None
    if receipt:
        rec = create_preflight_receipt(
            db, owner_id=owner_id, goal_id=goal.id, execution_envelope_id=envelope.id,
            provider_spend_authorization_id=spend.id, expected_repository_identity=REPO,
            expected_integration_branch=INTEGRATION, expected_base_branch="main", expected_base_sha=BASE,
            expected_activation_sha=ACTIVATION, expected_authority_version=7, facts=_facts(),
            expires_at=datetime.utcnow() + timedelta(minutes=14), founder_authorized=True,
        )
    db.commit()
    _rls(db, owner_id)
    return dict(owner=owner_id, goal=goal, envelope=envelope, spend=spend, task=task, job=job, worktree=worktree, receipt=rec)


def _grant_kwargs(g, **over):
    values = dict(
        owner_id=g["owner"], goal_id=g["goal"].id, task_id=g["task"].id, job_id=g["job"].id,
        worktree_id=g["worktree"].id, execution_envelope_id=g["envelope"].id,
        preflight_receipt_id=g["receipt"].id, repository_identity=REPO, branch=g["worktree"].branch,
        base_branch="main", base_sha=BASE, local_commit_sha=LOCAL, authority_version=7,
        expires_at=datetime.utcnow() + timedelta(minutes=5), idempotency_key=f"g:{uuid.uuid4()}",
        authorized_by="founder", founder_authorized=True,
    )
    values.update(over)
    return values


@pytest.fixture
def user(make_verified_user):
    u, _ = make_verified_user()
    return u


# ------------------------------------------------------------------ Founder identity / API


def test_F1_founder_api_preflight_without_configured_facts_is_no_go_and_body_facts_ignored(client, db_session):
    """Founder route works end-to-end under RLS; unconfigured settings -> NO-GO; caller cannot
    inject facts/attestations/founder_authorized through the JSON body."""
    g = _graph(db_session, FOUNDER_USER_ID, receipt=False)
    csrf = _founder_login(client)
    body = {
        "execution_envelope_id": str(g["envelope"].id), "provider_spend_authorization_id": str(g["spend"].id),
        "repository_identity": REPO, "integration_branch": INTEGRATION, "activation_sha": ACTIVATION,
        "base_branch": "main", "base_sha": BASE, "authority_version": 7,
        "expires_at": (datetime.utcnow() + timedelta(minutes=10)).isoformat(),
        # attacker-supplied extras: must be ignored
        "facts": {"observed_base_sha": BASE}, "founder_attestation_digest": "3" * 64,
        "ruleset_attestation_digest": "2" * 64, "observed_base_sha": BASE, "founder_authorized": True,
    }
    res = client.post(f"/api/mainai/execution/goals/{g['goal'].id}/preflight", json=body, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 201, res.text
    out = res.json()
    assert out["verdict"] == "no_go"
    for fact in ("activation_sha", "base_sha", "founder_attestation", "ruleset_attestation", "runtime_token_attestation", "integration_branch"):
        assert fact in out["unknown_required_facts"], out["unknown_required_facts"]


def test_F2_founder_role_with_wrong_fixed_identity_is_denied(client, make_verified_user):
    from tests.backend.test_activation_authority_substrate import _login

    u, pw = make_verified_user(role="founder")
    csrf = _login(client, u.email, pw)
    h = {"X-CSRF-Token": csrf}
    assert client.post(f"/api/mainai/execution/goals/{uuid.uuid4()}/preflight", json={}, headers=h).status_code == 403
    assert client.post(f"/api/mainai/execution/tasks/{uuid.uuid4()}/push-grants", json={}, headers=h).status_code == 403
    assert client.post(f"/api/mainai/execution/push-grants/{uuid.uuid4()}/cancel", headers=h).status_code == 403


def test_F3_unauthenticated_is_denied(client):
    assert client.post(f"/api/mainai/execution/goals/{uuid.uuid4()}/preflight", json={}).status_code in (401, 403)
    assert client.post(f"/api/mainai/execution/tasks/{uuid.uuid4()}/push-grants", json={}).status_code in (401, 403)


def test_F4_runtime_role_cannot_mint_an_active_push_grant_by_direct_insert(db_session, user):
    """MainAI self-authorization at the DB layer: the restricted runtime role (mainai_app) used by
    the worker can INSERT an 'active' grant directly, bypassing Founder/preflight/verification."""
    g = _graph(db_session, user.id)
    row = MainAIPushGrant(**{k: v for k, v in _grant_kwargs(g).items() if k not in ("founder_authorized",)}, status="active")
    row.authorized_at = datetime.utcnow()
    row.local_commit_sha = "f" * 40  # never verified, never on the worktree
    db_session.add(row)
    try:
        db_session.flush()
        inserted = True
    except DBAPIError:
        inserted = False
    db_session.rollback()
    assert not inserted, "GAP: mainai_app can INSERT an active push grant directly (no DB-level Founder authority)"


def test_F5_runtime_role_cannot_insert_a_go_receipt_directly(db_session, user):
    g = _graph(db_session, user.id, receipt=False)
    row = MainAIActivationPreflightReceipt(
        owner_id=user.id, goal_id=g["goal"].id, execution_envelope_id=g["envelope"].id,
        provider_spend_authorization_id=g["spend"].id, repository_identity=REPO, integration_branch=INTEGRATION,
        activation_sha=ACTIVATION, base_branch="main", base_sha=BASE, authority_version=7,
        required_check_contexts=CHECKS, runtime_config_digest="0" * 64, runtime_token_identity="x",
        runtime_token_attestation_digest="0" * 64, ruleset_attestation_digest="0" * 64,
        founder_attestation_digest="0" * 64, facts={}, unknown_required_facts=[], verdict="go",
        observed_at=datetime.utcnow(), expires_at=datetime.utcnow() + timedelta(minutes=10),
        receipt_digest=uuid.uuid4().hex * 2, provenance={},
    )
    db_session.add(row)
    try:
        db_session.flush()
        inserted = True
    except DBAPIError:
        inserted = False
    db_session.rollback()
    assert not inserted, "GAP: mainai_app can INSERT a GO preflight receipt with arbitrary facts"


# ------------------------------------------------------------------ preflight correctness


def test_P1_go_requires_verified_attestations_not_any_64_hex(db_session, user):
    """Spec: attestations verified against a pinned trust key/identity. Any 64-hex string -> GO?"""
    g = _graph(db_session, user.id, receipt=False)
    row = create_preflight_receipt(
        db_session, owner_id=user.id, goal_id=g["goal"].id, execution_envelope_id=g["envelope"].id,
        provider_spend_authorization_id=g["spend"].id, expected_repository_identity=REPO,
        expected_integration_branch=INTEGRATION, expected_base_branch="main", expected_base_sha=BASE,
        expected_activation_sha=ACTIVATION, expected_authority_version=7,
        facts=_facts(runtime_token_attestation_digest="e" * 64, ruleset_attestation_digest="e" * 64, founder_attestation_digest="e" * 64),
        expires_at=datetime.utcnow() + timedelta(minutes=10), founder_authorized=True,
    )
    assert row.verdict == "no_go", "GAP: arbitrary unverified 64-hex attestation digests yield GO"


def test_P2_receipt_cannot_outlive_spend_authorization(db_session, user):
    """Spec: receipt lifetime never beyond envelope/spend/attestation expiry."""
    g = _graph(db_session, user.id, spend_expires=timedelta(minutes=2))
    assert g["receipt"].verdict == "go"
    assert g["receipt"].expires_at <= g["spend"].expires_at, "GAP: GO receipt outlives its spend authorization"


def test_P3_go_requires_running_code_sha_not_a_config_string():
    """Code-read probe: facts.activation_sha = settings.mainai_activation_sha (a config string)."""
    import inspect

    from app.routers import activation_security as r

    src = inspect.getsource(r.activation_preflight)
    assert "settings.mainai_activation_sha" not in src, "GAP: activation_sha is an operator-set config value, not the running code SHA"


# ------------------------------------------------------------------ grant creation


def test_G1_grant_for_real_path_a_task_branch_is_possible(db_session, user):
    """Path A worktrees run on claude/mainai-task-*; the receipt binds the integration branch.
    create_push_grant requires worktree.branch == branch == receipt.integration_branch."""
    g = _graph(db_session, user.id, worktree_branch=f"claude/mainai-task-{uuid.uuid4().hex[:8]}")
    try:
        create_push_grant(db_session, **_grant_kwargs(g))
        ok = True
    except ActivationAuthorityError as exc:
        ok = False
        reason = str(exc)
    db_session.rollback()
    assert ok, f"BLOCKER: a task-branch worktree can never receive a grant ({reason}); grants only exist for a worktree checked out ON the integration branch"


def test_G2_verification_pass_must_bind_the_granted_commit(db_session, user):
    """Spec: verification PASS durably checkpointed for that job AND commit."""
    g = _graph(db_session, user.id, verified_sha="d" * 40)  # PASS was for a different commit
    try:
        create_push_grant(db_session, **_grant_kwargs(g))
        minted = True
    except ActivationAuthorityError:
        minted = False
    db_session.rollback()
    assert not minted, "GAP: a verification_passed event for another commit authorizes a grant for LOCAL"


def test_G3_grant_rereads_local_head_not_db_column(db_session, user):
    """The route passes no local_head_resolver: HEAD is the DB column. Worktree path does not exist."""
    g = _graph(db_session, user.id)
    try:
        create_push_grant(db_session, **_grant_kwargs(g))
        minted = True
    except ActivationAuthorityError:
        minted = False
    db_session.rollback()
    assert not minted, "GAP: grant minted for a worktree whose path does not exist (HEAD never re-read)"


def test_G4_revoked_spend_invalidates_grant_creation(db_session, user):
    g = _graph(db_session, user.id)
    revoke_provider_spend(db_session, owner_id=user.id, authorization_id=g["spend"].id, reason="examiner")
    db_session.commit()
    _rls(db_session, user.id)
    try:
        create_push_grant(db_session, **_grant_kwargs(g))
        minted = True
    except ActivationAuthorityError:
        minted = False
    db_session.rollback()
    assert not minted, "GAP: spend revoked after GO receipt, grant still minted"


def test_G5_body_cannot_set_founder_authorized_or_resolver(client, db_session):
    g = _graph(db_session, FOUNDER_USER_ID)
    csrf = _founder_login(client)
    body = {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in _grant_kwargs(g).items() if k not in ("owner_id", "task_id", "authorized_by", "founder_authorized")}
    body["expires_at"] = body["expires_at"].isoformat()
    body.update({"founder_authorized": False, "local_head_resolver": "x", "authorized_by": "mainai"})
    res = client.post(f"/api/mainai/execution/tasks/{g['task'].id}/push-grants", json=body, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 201, res.text
    _rls(db_session, FOUNDER_USER_ID)
    row = db_session.get(MainAIPushGrant, uuid.UUID(res.json()["id"]))
    assert row.authorized_by != "mainai"


def test_G6_concurrent_second_grant_for_same_task_is_a_typed_denial(client, db_session):
    """Two different idempotency keys for one task: unique partial index rejects the second.
    Through the API that must be a typed 409, not a 500."""
    g = _graph(db_session, FOUNDER_USER_ID)
    csrf = _founder_login(client)

    def body():
        b = {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in _grant_kwargs(g).items() if k not in ("owner_id", "task_id", "authorized_by", "founder_authorized")}
        b["expires_at"] = b["expires_at"].isoformat()
        return b

    first = client.post(f"/api/mainai/execution/tasks/{g['task'].id}/push-grants", json=body(), headers={"X-CSRF-Token": csrf})
    assert first.status_code == 201, first.text
    try:
        code = client.post(f"/api/mainai/execution/tasks/{g['task'].id}/push-grants", json=body(), headers={"X-CSRF-Token": csrf}).status_code
    except IntegrityError:
        code = "unhandled IntegrityError (HTTP 500)"
    _rls(db_session, FOUNDER_USER_ID)
    active = db_session.execute(text("SELECT count(*) FROM mainai_push_grants WHERE task_id=:t AND status='active'"), {"t": g["task"].id}).scalar_one()
    assert active == 1
    assert code == 409, f"second active grant for task -> {code} (expected typed 409)"


def test_G7_idempotent_replay_of_cancelled_grant_does_not_reactivate(db_session, user):
    g = _graph(db_session, user.id)
    kw = _grant_kwargs(g)
    grant = create_push_grant(db_session, **kw)
    db_session.commit()
    _rls(db_session, user.id)
    cancel_push_grant(db_session, owner_id=user.id, grant_id=grant.id, founder_authorized=True)
    db_session.commit()
    _rls(db_session, user.id)
    replay = create_push_grant(db_session, **kw)
    assert replay.id == grant.id and replay.status == "cancelled"
    with pytest.raises(ActivationAuthorityError):
        consume_push_grant(db_session, owner_id=user.id, grant_id=grant.id, job_id=g["job"].id, local_commit_sha=LOCAL, observed_base_sha=BASE, authority_version=7)


# ------------------------------------------------------------------ consumption / races


def test_C1_concurrent_consumption_exactly_one_wins(db_session, user):
    g = _graph(db_session, user.id)
    grant = create_push_grant(db_session, **_grant_kwargs(g))
    db_session.commit()
    gid, jid = grant.id, g["job"].id
    results: dict[str, str] = {}
    a, b = SessionLocal(), SessionLocal()
    try:
        _rls(a, user.id)
        consume_push_grant(a, owner_id=user.id, grant_id=gid, job_id=jid, local_commit_sha=LOCAL, observed_base_sha=BASE, authority_version=7)
        results["a"] = "consumed (uncommitted, row locked)"

        def other():
            try:
                _rls(b, user.id)
                consume_push_grant(b, owner_id=user.id, grant_id=gid, job_id=jid, local_commit_sha=LOCAL, observed_base_sha=BASE, authority_version=7)
                b.commit()
                results["b"] = "consumed"
            except PushGrantConsumed:
                results["b"] = "denied:consumed"
                b.rollback()
            except Exception as exc:  # pragma: no cover - reported
                results["b"] = f"error:{type(exc).__name__}:{exc}"
                b.rollback()

        t = threading.Thread(target=other)
        t.start()
        time.sleep(1.0)
        assert "b" not in results, "B did not block on A's row lock"
        a.commit()
        t.join(10)
    finally:
        a.close()
        b.close()
    assert results["b"] == "denied:consumed", results


def test_C2_expired_consumption_leaves_task_grantable(db_session, user):
    """consume() marks 'expired' then raises; callers roll back, so the row stays 'active' and
    the one-active-per-task index blocks every future grant until a manual cancel."""
    g = _graph(db_session, user.id)
    grant = create_push_grant(db_session, **_grant_kwargs(g, expires_at=datetime.utcnow() + timedelta(seconds=2)))
    db_session.commit()
    _rls(db_session, user.id)
    with pytest.raises(PushGrantExpired):
        consume_push_grant(db_session, owner_id=user.id, grant_id=grant.id, job_id=g["job"].id, local_commit_sha=LOCAL, observed_base_sha=BASE, authority_version=7, now=datetime.utcnow() + timedelta(minutes=1))
    db_session.rollback()
    _rls(db_session, user.id)
    status = db_session.execute(text("SELECT status FROM mainai_push_grants WHERE id=:i"), {"i": grant.id}).scalar_one()
    assert status != "active", f"GAP(P2): expired grant persists as status={status}"


def test_C3_consume_is_not_wired_to_any_transport():
    """Deferred transport: nothing in app/ calls consume_push_grant; Path A push is ungated by grants."""
    import pathlib

    import app

    root = pathlib.Path(app.__file__).parent
    callers = [p for p in root.rglob("*.py") if "consume_push_grant(" in p.read_text() and p.name != "activation_security.py"]
    assert callers == [], callers


# ------------------------------------------------------------------ DB state machine


def test_D1_db_transitions(db_session, user):
    g = _graph(db_session, user.id)
    grant = create_push_grant(db_session, **_grant_kwargs(g))
    db_session.commit()
    gid = grant.id
    cases = {
        "active->consumed without consumed_at": "UPDATE mainai_push_grants SET status='consumed' WHERE id=:i",
        "rebind local_commit_sha": "UPDATE mainai_push_grants SET local_commit_sha=repeat('f',40) WHERE id=:i",
        "extend expires_at": "UPDATE mainai_push_grants SET expires_at=expires_at + interval '1 day' WHERE id=:i",
        "delete grant": "DELETE FROM mainai_push_grants WHERE id=:i",
        "active->bogus": "UPDATE mainai_push_grants SET status='active2' WHERE id=:i",
    }
    leaked = []
    for name, sql in cases.items():
        _rls(db_session, user.id)
        try:
            n = db_session.execute(text(sql), {"i": gid}).rowcount
            db_session.flush()
            if n:
                leaked.append(name)
        except DBAPIError:
            pass
        db_session.rollback()
    rid = g["receipt"].id
    rcases = {
        "receipt un-invalidate": ("UPDATE mainai_activation_preflight_receipts SET invalidated_at=now() WHERE id=:i", "UPDATE mainai_activation_preflight_receipts SET invalidated_at=NULL WHERE id=:i"),
        "receipt extend": ("SELECT 1", "UPDATE mainai_activation_preflight_receipts SET expires_at=expires_at + interval '1 hour' WHERE id=:i"),
        "receipt flip verdict": ("SELECT 1", "UPDATE mainai_activation_preflight_receipts SET verdict='no_go' WHERE id=:i"),
    }
    for name, (pre, sql) in rcases.items():
        _rls(db_session, user.id)
        try:
            db_session.execute(text(pre), {"i": rid})
            n = db_session.execute(text(sql), {"i": rid}).rowcount
            db_session.flush()
            if n:
                leaked.append(name)
        except DBAPIError:
            pass
        db_session.rollback()
    assert leaked == [], leaked


def test_D2_envelope_bindings_immutable_but_status_mutable(db_session, user):
    g = _graph(db_session, user.id)
    for col, val in (("approved_base_sha", "'" + "f" * 40 + "'"), ("authority_version", "8"), ("base_branch", "'evil'"), ("repository_identity", "'x/y'")):
        _rls(db_session, user.id)
        with pytest.raises(DBAPIError):
            db_session.execute(text(f"UPDATE execution_authorization_envelopes SET {col}={val} WHERE id=:i"), {"i": g["envelope"].id})
        db_session.rollback()


def test_D3_cross_owner_rls(db_session, user, make_verified_user):
    g = _graph(db_session, user.id)
    grant = create_push_grant(db_session, **_grant_kwargs(g))
    db_session.commit()
    other, _ = make_verified_user()
    _rls(db_session, other.id)
    assert db_session.execute(text("SELECT count(*) FROM mainai_push_grants WHERE id=:i"), {"i": grant.id}).scalar_one() == 0
    with pytest.raises(ActivationAuthorityError):
        consume_push_grant(db_session, owner_id=other.id, grant_id=grant.id, job_id=g["job"].id, local_commit_sha=LOCAL, observed_base_sha=BASE, authority_version=7)


# ------------------------------------------------------------------ existing contracts


def test_E1_account_erasure_succeeds_after_activation_rows_exist(db_session, user):
    """Existing contract: DELETE /api/account must succeed for an owner who ran MainAI. The
    new receipt/grant DELETE triggers raise on the users/goal/envelope ON DELETE CASCADE."""
    from app.account.erasure import erase_account_data
    from app.account.reauth import create_account_erasure_reauth_receipt

    g = _graph(db_session, user.id)
    create_push_grant(db_session, **_grant_kwargs(g))
    db_session.commit()
    _rls(db_session, user.id)
    jti = _issue_erasure_access_session(db_session, user.id)
    receipt = create_account_erasure_reauth_receipt(db_session, user=user, password=PASSWORD, access_jti=jti)
    _rls(db_session, user.id)
    db_session.execute(text("SET LOCAL app.current_access_jti = :j"), {"j": receipt.access_jti})
    try:
        erase_account_data(db_session, user, reauth_receipt_id=receipt.receipt_id, reauth_access_jti=receipt.access_jti)
        ok, err = True, ""
    except Exception as exc:
        ok, err = False, f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}"
        db_session.rollback()
    assert ok, f"REGRESSION: account erasure fails once activation rows exist -> {err}"


def test_E1_control_account_erasure_without_activation_rows(db_session, user):
    from app.account.erasure import erase_account_data
    from app.account.reauth import create_account_erasure_reauth_receipt

    _graph(db_session, user.id, receipt=False)
    _rls(db_session, user.id)
    jti = _issue_erasure_access_session(db_session, user.id)
    receipt = create_account_erasure_reauth_receipt(db_session, user=user, password=PASSWORD, access_jti=jti)
    _rls(db_session, user.id)
    db_session.execute(text("SET LOCAL app.current_access_jti = :j"), {"j": receipt.access_jti})
    erase_account_data(db_session, user, reauth_receipt_id=receipt.receipt_id, reauth_access_jti=receipt.access_jti)


def test_E2_281_check_contract_shape_is_accepted_by_envelope(db_session, user):
    """Composition with #281: its contract binds app_id/workflow_path/events per check."""
    _rls(db_session, user.id)
    goal = planner.create_goal(db_session, owner_id=user.id, title="c", original_instruction="c", created_by="founder")
    proposal = propose_execution_scope(db_session, owner_id=user.id, goal_id=goal.id, idempotency_key=f"p:{uuid.uuid4()}", repository_identity=REPO)
    check = {"name": "Backend — unit/integration tests", "app_slug": "github-actions", "app_id": 15368, "workflow_path": ".github/workflows/ci.yml", "events": ["pull_request"]}
    try:
        authorize_execution_scope(
            db_session, owner_id=user.id, proposal_id=proposal.id, authorized_by="founder",
            authorized_paths=["backend/app/"], authorized_capabilities=["path_a", "repo_edit", "push"],
            authorized_risk="low", envelope_idempotency_key=f"e:{uuid.uuid4()}", repository_identity=REPO,
            base_branch="main", approved_base_sha=BASE, required_check_contexts=[check], authority_version=1,
        )
        ok = True
    except (ExecutionEnvelopeError, AttributeError, TypeError) as exc:
        ok = False
        err = f"{type(exc).__name__}: {exc}"
    db_session.rollback()
    assert ok, f"INTEGRATION BLOCKER vs #281: {err}"


def test_E3_legacy_envelope_without_activation_fields_still_authorizes(db_session, user):
    _rls(db_session, user.id)
    goal = planner.create_goal(db_session, owner_id=user.id, title="c", original_instruction="c", created_by="founder")
    proposal = propose_execution_scope(db_session, owner_id=user.id, goal_id=goal.id, idempotency_key=f"p:{uuid.uuid4()}", repository_identity=REPO)
    _, env = authorize_execution_scope(
        db_session, owner_id=user.id, proposal_id=proposal.id, authorized_by="founder",
        authorized_paths=["backend/app/"], authorized_capabilities=["path_a", "repo_edit"],
        authorized_risk="low", envelope_idempotency_key=f"e:{uuid.uuid4()}",
    )
    db_session.flush()
    assert env.base_branch is None and env.authority_version == 1 and env.required_check_contexts == []


def test_E1b_account_erasure_succeeds_after_only_a_no_go_preflight(db_session, user):
    """A single NO-GO preflight (what the Founder route records with unconfigured settings)."""
    from app.account.erasure import erase_account_data
    from app.account.reauth import create_account_erasure_reauth_receipt

    g = _graph(db_session, user.id, receipt=False)
    create_preflight_receipt(
        db_session, owner_id=user.id, goal_id=g["goal"].id, execution_envelope_id=g["envelope"].id,
        provider_spend_authorization_id=g["spend"].id, expected_repository_identity=REPO,
        expected_integration_branch=INTEGRATION, expected_base_branch="main", expected_base_sha=BASE,
        expected_activation_sha=ACTIVATION, expected_authority_version=7,
        facts=_facts(activation_sha=None, observed_base_sha=None, founder_attestation_digest=None),
        expires_at=datetime.utcnow() + timedelta(minutes=5), founder_authorized=True,
    )
    db_session.commit()
    _rls(db_session, user.id)
    jti = _issue_erasure_access_session(db_session, user.id)
    receipt = create_account_erasure_reauth_receipt(db_session, user=user, password=PASSWORD, access_jti=jti)
    _rls(db_session, user.id)
    db_session.execute(text("SET LOCAL app.current_access_jti = :j"), {"j": receipt.access_jti})
    try:
        erase_account_data(db_session, user, reauth_receipt_id=receipt.receipt_id, reauth_access_jti=receipt.access_jti)
        ok, err = True, ""
    except Exception as exc:
        ok, err = False, f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}"
        db_session.rollback()
    assert ok, f"REGRESSION: account erasure fails after one NO-GO preflight -> {err}"
