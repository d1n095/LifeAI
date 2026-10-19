from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.execution_envelopes.service import authorize_execution_scope, propose_execution_scope
from app.mainai_execution import planner
from app.mainai_execution.activation_security import (
    ActivationAuthorityError,
    ActivationBaseSHAMoved,
    ActivationEnvelopeSuperseded,
    ActivationReceiptStale,
    AuthoritativePreflightFacts,
    AuthorityVersionMismatch,
    FounderAuthorityRequired,
    PushGrantConsumed,
    PushGrantExpired,
    canonical_required_checks,
    consume_push_grant,
    create_preflight_receipt,
    create_push_grant,
)
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import MainAITask, MainAITaskEvent, MainAITaskEventType
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.mainai_recovery import MainAITaskWorktree, MainAITaskWorktreeStatus
from app.provider_spend.service import authorize_provider_spend
from app.request_context import current_user_id as current_user_id_var

REPO = "d1n095/LifeAI"
BASE = "a" * 40
LOCAL = "b" * 40
ACTIVATION = "c" * 40
CHECKS = [{"name": "backend", "app_slug": "github-actions"}, {"name": "npm-audit", "app_slug": "github-actions"}]


def _login(client, email: str, password: str) -> str:
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


@pytest.fixture
def authority_graph(db_session, make_verified_user):
    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    goal = planner.create_goal(db_session, owner_id=user.id, title="activation", original_instruction="safe edit", created_by="founder")
    proposal = propose_execution_scope(db_session, owner_id=user.id, goal_id=goal.id, idempotency_key=f"proposal:{uuid.uuid4()}", repository_identity=REPO)
    _, envelope = authorize_execution_scope(
        db_session, owner_id=user.id, proposal_id=proposal.id, authorized_by="founder",
        authorized_paths=["backend/app/"], authorized_capabilities=["path_a", "repo_edit", "push"],
        authorized_risk="low", envelope_idempotency_key=f"envelope:{uuid.uuid4()}",
        repository_identity=REPO, base_branch="main", approved_base_sha=BASE,
        required_check_contexts=CHECKS, authority_version=7,
    )
    spend = authorize_provider_spend(
        db_session, owner_id=user.id, goal_id=goal.id, execution_envelope_id=envelope.id,
        authorized_by="founder", max_cost_usd=Decimal("1"), max_requests=1,
        max_cost_per_request_usd=Decimal("1"), allowed_providers=["openai"],
        allowed_models=["gpt-4o-mini"], expires_at=datetime.utcnow() + timedelta(hours=1),
        idempotency_key=f"spend:{uuid.uuid4()}",
    )
    planner.create_plan(db_session, goal=goal, rationale="one task", created_by="founder", tasks=[PlannedTaskSpec(description="edit", task_type="repo_edit", verification_plan=[{"kind": "targeted_tests", "target": "tests/backend/test_activation_authority_substrate.py"}])])
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    now = datetime.utcnow()
    job = MainAIJob(owner_id=user.id, job_type="task_execution", status=MainAIJobStatus.completed, started_at=now, completed_at=now, idempotency_key=f"job:{uuid.uuid4()}", created_by="founder")
    db_session.add(job)
    db_session.flush()
    task.mainai_job_id = job.id
    worktree = MainAITaskWorktree(
        task_id=task.id, owner_id=user.id, job_id=job.id, lease_generation=1,
        executor_id="test", repo=REPO, base_sha=BASE, branch="mainai/integration-founder-supervised-v1",
        path="/tmp/not-used", marker_token="marker", current_commit=LOCAL,
        status=MainAITaskWorktreeStatus.active,
    )
    db_session.add_all([worktree, MainAITaskEvent(task_id=task.id, owner_id=user.id, event_type=MainAITaskEventType.verification_passed, detail={"sha": LOCAL})])
    db_session.flush()
    facts = AuthoritativePreflightFacts(
        activation_sha=ACTIVATION, repository_identity=REPO,
        integration_branch="mainai/integration-founder-supervised-v1", observed_base_sha=BASE,
        runtime_config={"write": True, "auto_merge": False}, runtime_token_identity="github-app:1",
        runtime_token_attestation_digest="1" * 64, ruleset_attestation_digest="2" * 64,
        founder_attestation_digest="3" * 64, negative_authority_confirmed=True,
    )
    receipt = create_preflight_receipt(
        db_session, owner_id=user.id, goal_id=goal.id, execution_envelope_id=envelope.id,
        provider_spend_authorization_id=spend.id, expected_repository_identity=REPO,
        expected_integration_branch=worktree.branch, expected_base_branch="main", expected_base_sha=BASE,
        expected_activation_sha=ACTIVATION, expected_authority_version=7, facts=facts,
        expires_at=datetime.utcnow() + timedelta(minutes=10), founder_authorized=True,
    )
    db_session.commit()
    return user, goal, envelope, task, job, worktree, receipt, spend, facts


def _grant(db, graph, **changes):
    user, goal, envelope, task, job, worktree, receipt, _spend, _facts = graph
    values = dict(
        owner_id=user.id, goal_id=goal.id, task_id=task.id, job_id=job.id, worktree_id=worktree.id,
        execution_envelope_id=envelope.id, preflight_receipt_id=receipt.id, repository_identity=REPO,
        branch=worktree.branch, base_branch="main", base_sha=BASE, local_commit_sha=LOCAL,
        authority_version=7, expires_at=datetime.utcnow() + timedelta(minutes=5),
        idempotency_key=f"push:{uuid.uuid4()}", authorized_by=user.email, founder_authorized=True,
    )
    values.update(changes)
    return create_push_grant(db, **values)


def test_preflight_unknowns_are_no_go_and_mainai_cannot_self_authorize(db_session, authority_graph):
    user, goal, envelope, _task, _job, _worktree, _receipt, spend, facts = authority_graph
    missing = AuthoritativePreflightFacts(**{**facts.__dict__, "ruleset_attestation_digest": None})
    row = create_preflight_receipt(
        db_session, owner_id=user.id, goal_id=goal.id, execution_envelope_id=envelope.id,
        provider_spend_authorization_id=spend.id, expected_repository_identity=REPO,
        expected_integration_branch="other", expected_base_branch="main", expected_base_sha=BASE,
        expected_activation_sha=ACTIVATION, expected_authority_version=7, facts=missing,
        expires_at=datetime.utcnow() + timedelta(minutes=5), founder_authorized=True,
    )
    assert row.verdict == "no_go"
    assert "ruleset_attestation" in row.unknown_required_facts
    with pytest.raises(FounderAuthorityRequired):
        _grant(db_session, authority_graph, founder_authorized=False)


def test_activation_authority_apis_reject_mainai_or_member_identity(client, make_verified_user):
    user, password = make_verified_user(role="member")
    csrf = _login(client, user.email, password)
    headers = {"X-CSRF-Token": csrf}
    assert client.post(f"/api/mainai/execution/goals/{uuid.uuid4()}/preflight", json={}, headers=headers).status_code == 403
    assert client.post(f"/api/mainai/execution/tasks/{uuid.uuid4()}/push-grants", json={}, headers=headers).status_code == 403
    assert client.post(f"/api/mainai/execution/push-grants/{uuid.uuid4()}/cancel", headers=headers).status_code == 403


@pytest.mark.parametrize("field,value", [("owner_id", uuid.uuid4()), ("job_id", uuid.uuid4()), ("worktree_id", uuid.uuid4()), ("local_commit_sha", "d" * 40)])
def test_push_grant_rejects_wrong_execution_bindings(db_session, authority_graph, field, value):
    with pytest.raises(ActivationAuthorityError):
        _grant(db_session, authority_graph, **{field: value})


def test_push_grant_is_one_shot_and_rejects_moved_base(db_session, authority_graph):
    grant = _grant(db_session, authority_graph)
    db_session.commit()
    with pytest.raises(ActivationBaseSHAMoved):
        consume_push_grant(db_session, owner_id=grant.owner_id, grant_id=grant.id, job_id=grant.job_id, local_commit_sha=LOCAL, observed_base_sha="d" * 40, authority_version=7)
    db_session.rollback()
    consumed = consume_push_grant(db_session, owner_id=grant.owner_id, grant_id=grant.id, job_id=grant.job_id, local_commit_sha=LOCAL, observed_base_sha=BASE, authority_version=7)
    assert consumed.status == "consumed"
    db_session.commit()
    with pytest.raises(PushGrantConsumed):
        consume_push_grant(db_session, owner_id=grant.owner_id, grant_id=grant.id, job_id=grant.job_id, local_commit_sha=LOCAL, observed_base_sha=BASE, authority_version=7)


def test_push_grant_idempotency_cannot_rebind_authority(db_session, authority_graph):
    key = f"push:{uuid.uuid4()}"
    _grant(db_session, authority_graph, idempotency_key=key)
    db_session.flush()
    with pytest.raises(ActivationAuthorityError, match="idempotency key reused"):
        _grant(db_session, authority_graph, idempotency_key=key, branch="attacker/substitute")


def test_expired_grant_and_authority_version_fail_closed(db_session, authority_graph):
    past = datetime.utcnow() - timedelta(minutes=1)
    with pytest.raises(ActivationAuthorityError):
        _grant(db_session, authority_graph, expires_at=past)
    grant = _grant(db_session, authority_graph)
    with pytest.raises(AuthorityVersionMismatch):
        consume_push_grant(db_session, owner_id=grant.owner_id, grant_id=grant.id, job_id=grant.job_id, local_commit_sha=LOCAL, observed_base_sha=BASE, authority_version=8)
    with pytest.raises(PushGrantExpired):
        consume_push_grant(db_session, owner_id=grant.owner_id, grant_id=grant.id, job_id=grant.job_id, local_commit_sha=LOCAL, observed_base_sha=BASE, authority_version=7, now=grant.expires_at + timedelta(seconds=1))


def test_stale_receipt_superseded_envelope_and_altered_checks_are_denied(db_session, authority_graph):
    _user, _goal, envelope, _task, _job, _worktree, receipt, _spend, _facts = authority_graph
    assert canonical_required_checks(list(reversed(CHECKS))) == canonical_required_checks(CHECKS)
    with pytest.raises(ActivationAuthorityError):
        canonical_required_checks(CHECKS + [{"name": "backend", "app_slug": "github-actions"}])
    receipt.invalidated_at = datetime.utcnow()
    db_session.flush()
    with pytest.raises(ActivationReceiptStale):
        _grant(db_session, authority_graph)
    db_session.rollback()
    envelope.status = "superseded"
    db_session.flush()
    with pytest.raises(ActivationEnvelopeSuperseded):
        _grant(db_session, authority_graph)


def test_consumption_rechecks_receipt_staleness(db_session, authority_graph):
    grant = _grant(db_session, authority_graph)
    receipt = authority_graph[6]
    db_session.commit()
    receipt.invalidated_at = datetime.utcnow()
    db_session.commit()
    with pytest.raises(ActivationReceiptStale):
        consume_push_grant(db_session, owner_id=grant.owner_id, grant_id=grant.id, job_id=grant.job_id, local_commit_sha=LOCAL, observed_base_sha=BASE, authority_version=7)


def test_consumption_rechecks_envelope_supersession(db_session, authority_graph):
    grant = _grant(db_session, authority_graph)
    envelope = authority_graph[2]
    db_session.commit()
    envelope.status = "superseded"
    db_session.commit()
    with pytest.raises(ActivationEnvelopeSuperseded):
        consume_push_grant(db_session, owner_id=grant.owner_id, grant_id=grant.id, job_id=grant.job_id, local_commit_sha=LOCAL, observed_base_sha=BASE, authority_version=7)


def test_database_prevents_required_check_set_mutation(db_session, authority_graph):
    envelope = authority_graph[2]
    envelope.required_check_contexts = [{"name": "substitute", "app_slug": "untrusted"}]
    with pytest.raises(DBAPIError, match="authority bindings are immutable"):
        db_session.flush()


def test_database_prevents_consumed_grant_revival(db_session, authority_graph):
    grant = _grant(db_session, authority_graph)
    db_session.commit()
    consume_push_grant(
        db_session,
        owner_id=grant.owner_id,
        grant_id=grant.id,
        job_id=grant.job_id,
        local_commit_sha=LOCAL,
        observed_base_sha=BASE,
        authority_version=7,
    )
    db_session.commit()
    grant.status = "active"
    grant.consumed_at = None
    grant.consumed_by_job_id = None
    with pytest.raises(DBAPIError, match="terminal push grant state is immutable"):
        db_session.flush()
