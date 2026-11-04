from __future__ import annotations

import hashlib
import hmac
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import sessionmaker

from app.agent_coordination.external_connectivity.cursor_cloud import WEBHOOK_USER_AGENT
from app.agent_coordination.operating_cycle_runtime import (
    bind_cycle_dispatch,
    claim_next_cycle,
    create_authorized_cycle,
    run_claimed_cycle_step,
)
from app.agent_coordination.provider_webhook_ingress import ingest_cursor_webhook
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.mainai_verification_registry import record_verification_attestation
from app.models.mainai_execution import MainAIGoalStatus, MainAITask, MainAITaskStatus
from app.models.operating_cycle import AuthorizedOperatingCycle, ProviderWebhookEvent
from app.models.user import User
from app.models.workforce import WorkforceAssignment
from app.request_context import current_user_id
from app.workforce.kill_switch import activate_kill_switch
from app.workforce.registry import register_workforce_agent

SECRET = "synthetic-local-webhook-secret"


def _rls(db, owner_id):
    current_user_id.set(str(owner_id))
    db.execute(text("SET LOCAL app.current_user_id=:uid"), {"uid": str(owner_id)})


def _setup(db, *, external_agent_id="cursor-agent-1"):
    owner = User(email=f"rt3-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(owner)
    db.flush()
    _rls(db, owner.id)
    builder = register_workforce_agent(
        db,
        owner_id=owner.id,
        agent_key=f"rt3-builder-{uuid.uuid4().hex[:8]}",
        name="RT3 builder",
        role="builder",
        agent_type="CODING",
        provider_type="cursor",
        trust_zone="CONTROLLED_INTERNAL",
        capability_tags=["read_only_audit"],
        status="active",
    )
    verifier = register_workforce_agent(
        db,
        owner_id=owner.id,
        agent_key=f"rt3-verifier-{uuid.uuid4().hex[:8]}",
        name="RT3 verifier",
        role="verifier",
        agent_type="REVIEW",
        provider_type="none",
        trust_zone="LOCAL_INTERNAL",
        capability_tags=["verification"],
        status="active",
    )
    goal = create_goal(db, owner_id=owner.id, title="RT3", original_instruction="audit", created_by="founder")
    create_plan(
        db,
        goal=goal,
        rationale="bounded",
        tasks=[PlannedTaskSpec(description="audit one thing", task_type="read_only_audit")],
        created_by="founder",
    )
    goal.status = MainAIGoalStatus.running
    proposal = propose_execution_scope(db, owner_id=owner.id, goal_id=goal.id, idempotency_key=f"p-{uuid.uuid4()}")
    _, envelope = authorize_execution_scope(
        db,
        owner_id=owner.id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=["docs/**"],
        authorized_capabilities=["read_file"],
        authorized_risk="low",
        envelope_idempotency_key=f"e-{uuid.uuid4()}",
    )
    cycle = create_authorized_cycle(
        db,
        owner_id=owner.id,
        goal_id=goal.id,
        envelope_id=envelope.id,
        verifier_profile_id=verifier.id,
    )
    db.commit()
    if external_agent_id:
        worker_id = f"setup-{uuid.uuid4()}"
        claim = claim_next_cycle(db, worker_id=worker_id, lease_seconds=60)
        assert claim is not None
        result = run_claimed_cycle_step(
            db, cycle_id=claim[0], owner_id=claim[1], worker_id=worker_id, generation=claim[2]
        )
        assert result["state"] == "awaiting_dispatch"
        bind_cycle_dispatch(
            db,
            owner_id=owner.id,
            cycle_id=cycle.id,
            external_agent_id=external_agent_id,
            dispatch_receipt=f"synthetic-dispatch:{external_agent_id}",
        )
        db.commit()
    return owner, builder, verifier, goal, envelope, cycle


def _sign(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, signature


def test_worker_claim_is_single_fenced_and_restart_reclaimable(superuser_db):
    owner, _builder, _verifier, _goal, _envelope, cycle = _setup(superuser_db, external_agent_id=None)
    first = claim_next_cycle(superuser_db, worker_id="worker-a", lease_seconds=30)
    assert first == (cycle.id, owner.id, 1)
    assert claim_next_cycle(superuser_db, worker_id="worker-b", lease_seconds=30) is None
    superuser_db.execute(
        text("UPDATE authorized_operating_cycles SET lease_expires_at=now()-interval '1 second' WHERE id=:id"),
        {"id": cycle.id},
    )
    superuser_db.commit()
    second = claim_next_cycle(superuser_db, worker_id="worker-b", lease_seconds=30)
    assert second == (cycle.id, owner.id, 2)
    # The stale worker's generation can no longer mutate.
    try:
        run_claimed_cycle_step(
            superuser_db, cycle_id=cycle.id, owner_id=owner.id, worker_id="worker-a", generation=1
        )
    except Exception as exc:
        assert "lease lost" in str(exc)
    else:
        raise AssertionError("stale cycle fence was accepted")


def test_concurrent_workers_cannot_claim_same_cycle(superuser_db):
    owner, _builder, _verifier, _goal, _envelope, cycle = _setup(superuser_db, external_agent_id=None)
    from app.db import migration_engine

    sessions = sessionmaker(bind=migration_engine)
    barrier = threading.Barrier(2)

    def claim(worker_id):
        with sessions() as db:
            barrier.wait(timeout=5)
            return claim_next_cycle(db, worker_id=worker_id, lease_seconds=60)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, ("concurrent-a", "concurrent-b")))
    winners = [result for result in outcomes if result is not None]
    assert len(winners) == 1
    assert winners[0][:2] == (cycle.id, owner.id)


def test_authenticated_event_never_bypasses_independent_verification(superuser_db):
    owner, builder, verifier, _goal, _envelope, cycle = _setup(superuser_db)
    cycle = superuser_db.get(AuthorizedOperatingCycle, cycle.id)
    assignment = superuser_db.get(WorkforceAssignment, cycle.workforce_assignment_id)
    assert assignment.profile_id == builder.id

    sha = "a" * 40
    now = datetime.now(timezone.utc)
    raw, signature = _sign(
        {"event": "statusChange", "id": cycle.external_agent_id, "status": "FINISHED", "timestamp": now.isoformat(), "candidate_sha": sha}
    )
    received = ingest_cursor_webhook(
        superuser_db,
        owner_id=owner.id,
        secret=SECRET,
        raw_body=raw,
        signature=signature,
        user_agent=WEBHOOK_USER_AGENT,
        delivery_id="delivery-1",
        now=now,
        fixture=True,
    )
    assert received["outcome"] == "verification_required"
    assert received["independently_verified"] is False
    task = superuser_db.get(MainAITask, cycle.current_task_id)
    assert task.status == MainAITaskStatus.ready
    replay = ingest_cursor_webhook(
        superuser_db,
        owner_id=owner.id,
        secret=SECRET,
        raw_body=raw,
        signature=signature,
        user_agent=WEBHOOK_USER_AGENT,
        delivery_id="delivery-1",
        now=now,
        fixture=True,
    )
    assert replay["outcome"] == "replay_ignored"
    assert superuser_db.query(ProviderWebhookEvent).count() == 1

    record_verification_attestation(
        superuser_db,
        owner_id=owner.id,
        component_id=f"workforce_assignment:{assignment.id}",
        candidate_sha=sha,
        builder_identity=builder.agent_key,
        examiner_identity=verifier.agent_key,
        review_result="PASS",
        evidence_summary="synthetic independent fixture",
    )
    superuser_db.commit()
    claim = claim_next_cycle(superuser_db, worker_id="worker", lease_seconds=60)
    assert claim
    verified = run_claimed_cycle_step(
        superuser_db, cycle_id=claim[0], owner_id=claim[1], worker_id="worker", generation=claim[2]
    )
    assert verified["state"] == "pending"
    assert superuser_db.get(MainAITask, task.id).status == MainAITaskStatus.completed
    assert superuser_db.get(WorkforceAssignment, assignment.id).verification_status == "VERIFIED"


def test_webhook_rejects_forgery_stale_cross_owner_and_stop(superuser_db):
    owner, _builder, _verifier, _goal, _envelope, cycle = _setup(superuser_db)
    now = datetime.now(timezone.utc)
    payload = {"event": "statusChange", "id": cycle.external_agent_id, "status": "RUNNING", "timestamp": now.isoformat()}
    raw, signature = _sign(payload)
    forged = ingest_cursor_webhook(
        superuser_db, owner_id=owner.id, secret=SECRET, raw_body=raw, signature="sha256=" + "0" * 64,
        user_agent=WEBHOOK_USER_AGENT, delivery_id="f", now=now, fixture=True,
    )
    assert forged["reason"] == "signature_invalid"
    stale_payload = {**payload, "timestamp": (now - timedelta(minutes=10)).isoformat()}
    stale_raw, stale_sig = _sign(stale_payload)
    stale = ingest_cursor_webhook(
        superuser_db, owner_id=owner.id, secret=SECRET, raw_body=stale_raw, signature=stale_sig,
        user_agent=WEBHOOK_USER_AGENT, delivery_id="s", now=now, fixture=True,
    )
    assert stale["reason"] == "timestamp_out_of_window"
    foreign = ingest_cursor_webhook(
        superuser_db, owner_id=uuid.uuid4(), secret=SECRET, raw_body=raw, signature=signature,
        user_agent=WEBHOOK_USER_AGENT, delivery_id="x", now=now, fixture=True,
    )
    assert foreign["reason"] == "unknown_or_inactive_binding"
    activate_kill_switch(superuser_db, owner_id=owner.id, reason="test stop")
    superuser_db.commit()
    stopped = ingest_cursor_webhook(
        superuser_db, owner_id=owner.id, secret=SECRET, raw_body=raw, signature=signature,
        user_agent=WEBHOOK_USER_AGENT, delivery_id="after-stop", now=now, fixture=True,
    )
    assert stopped["reason"] == "authority_not_live"


def test_provider_completion_with_mismatched_sha_cannot_use_other_pass(superuser_db):
    owner, builder, verifier, _goal, _envelope, cycle = _setup(superuser_db)
    assignment = superuser_db.get(WorkforceAssignment, superuser_db.get(AuthorizedOperatingCycle, cycle.id).workforce_assignment_id)
    record_verification_attestation(
        superuser_db, owner_id=owner.id, component_id=f"workforce_assignment:{assignment.id}",
        candidate_sha="b" * 40, builder_identity=builder.agent_key, examiner_identity=verifier.agent_key,
        review_result="PASS", evidence_summary="different sha",
    )
    record_verification_attestation(
        superuser_db, owner_id=owner.id, component_id=f"workforce_assignment:{assignment.id}",
        candidate_sha="c" * 40, builder_identity=builder.agent_key, examiner_identity="unassigned-examiner",
        review_result="PASS", evidence_summary="right sha but wrong examiner",
    )
    superuser_db.commit()
    now = datetime.now(timezone.utc)
    raw, sig = _sign({"event":"statusChange","id":cycle.external_agent_id,"status":"FINISHED","timestamp":now.isoformat(),"candidate_sha":"c"*40})
    ingest_cursor_webhook(superuser_db, owner_id=owner.id, secret=SECRET, raw_body=raw, signature=sig,
                          user_agent=WEBHOOK_USER_AGENT, delivery_id="wrong-sha", now=now, fixture=True)
    claim = claim_next_cycle(superuser_db, worker_id="w", lease_seconds=60)
    result = run_claimed_cycle_step(superuser_db, cycle_id=claim[0], owner_id=claim[1], worker_id="w", generation=claim[2])
    assert result["state"] == "awaiting_verification"
    assert superuser_db.get(WorkforceAssignment, assignment.id).verification_status == "UNVERIFIED"


def test_global_epoch_change_fences_preexisting_cycle(superuser_db):
    owner, _builder, _verifier, _goal, _envelope, cycle = _setup(superuser_db, external_agent_id=None)
    original = superuser_db.execute(
        text("SELECT epoch FROM workforce_authority_epoch WHERE scope_key='GLOBAL'")
    ).scalar_one()
    try:
        superuser_db.execute(text("UPDATE workforce_authority_epoch SET epoch=epoch+1 WHERE scope_key='GLOBAL'"))
        superuser_db.commit()
        claim = claim_next_cycle(superuser_db, worker_id="epoch-worker", lease_seconds=60)
        result = run_claimed_cycle_step(
            superuser_db, cycle_id=claim[0], owner_id=claim[1], worker_id="epoch-worker", generation=claim[2]
        )
        assert result["outcome"] == "blocked"
        assert "authority epoch changed" in result["reason"]
    finally:
        superuser_db.execute(
            text("UPDATE workforce_authority_epoch SET epoch=:epoch WHERE scope_key='GLOBAL'"), {"epoch": original}
        )
        superuser_db.commit()


def test_delivery_id_cannot_be_reused_with_changed_body(superuser_db):
    owner, _builder, _verifier, _goal, _envelope, cycle = _setup(superuser_db)
    now = datetime.now(timezone.utc)
    first_raw, first_sig = _sign({"event":"statusChange","id":cycle.external_agent_id,"status":"RUNNING","timestamp":now.isoformat()})
    first = ingest_cursor_webhook(superuser_db, owner_id=owner.id, secret=SECRET, raw_body=first_raw,
                                  signature=first_sig, user_agent=WEBHOOK_USER_AGENT,
                                  delivery_id="immutable-delivery", now=now, fixture=True)
    assert first["outcome"] == "progress_recorded"
    changed_raw, changed_sig = _sign({"event":"statusChange","id":cycle.external_agent_id,"status":"FINISHED","timestamp":now.isoformat(),"candidate_sha":"d"*40})
    changed = ingest_cursor_webhook(superuser_db, owner_id=owner.id, secret=SECRET, raw_body=changed_raw,
                                    signature=changed_sig, user_agent=WEBHOOK_USER_AGENT,
                                    delivery_id="immutable-delivery", now=now, fixture=True)
    assert changed == {"outcome": "rejected", "reason": "delivery_id_body_mismatch", "first_cycle_go": False}


def test_concurrent_duplicate_webhooks_apply_once(superuser_db):
    owner, _builder, _verifier, _goal, _envelope, cycle = _setup(superuser_db)
    from app.db import migration_engine

    now = datetime.now(timezone.utc)
    raw, signature = _sign(
        {"event": "statusChange", "id": cycle.external_agent_id, "status": "RUNNING", "timestamp": now.isoformat()}
    )
    sessions = sessionmaker(bind=migration_engine)
    barrier = threading.Barrier(2)

    def ingest(_worker_id):
        with sessions() as db:
            barrier.wait(timeout=5)
            return ingest_cursor_webhook(
                db,
                owner_id=owner.id,
                secret=SECRET,
                raw_body=raw,
                signature=signature,
                user_agent=WEBHOOK_USER_AGENT,
                delivery_id="concurrent-delivery",
                now=now,
                fixture=True,
            )["outcome"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(ingest, ("concurrent-a", "concurrent-b")))
    assert sorted(outcomes) == ["progress_recorded", "replay_ignored"]
    superuser_db.expire_all()
    assert (
        superuser_db.query(ProviderWebhookEvent)
        .filter(ProviderWebhookEvent.event_key.isnot(None), ProviderWebhookEvent.cycle_id == cycle.id)
        .count()
        == 1
    )


def test_real_http_ingress_uses_raw_signature_and_runtime_rls(client, db_session, make_verified_user, monkeypatch):
    user, _password = make_verified_user()
    _rls(db_session, user.id)
    # Build the canonical rows in the ordinary runtime session, then let the HTTP request
    # reopen them under the path owner scope. The path value itself is never authority.
    register_workforce_agent(
        db_session, owner_id=user.id, agent_key=f"http-builder-{uuid.uuid4().hex[:8]}", name="HTTP builder",
        role="builder", agent_type="CODING", provider_type="cursor", trust_zone="CONTROLLED_INTERNAL",
        capability_tags=["read_only_audit"], status="active",
    )
    verifier = register_workforce_agent(
        db_session, owner_id=user.id, agent_key=f"http-verifier-{uuid.uuid4().hex[:8]}", name="HTTP verifier",
        role="verifier", agent_type="REVIEW", provider_type="none", trust_zone="LOCAL_INTERNAL",
        capability_tags=["verification"], status="active",
    )
    goal = create_goal(db_session, owner_id=user.id, title="HTTP RT3", original_instruction="audit", created_by="founder")
    create_plan(db_session, goal=goal, rationale="http", tasks=[PlannedTaskSpec(description="audit", task_type="read_only_audit")], created_by="founder")
    goal.status = MainAIGoalStatus.running
    proposal = propose_execution_scope(db_session, owner_id=user.id, goal_id=goal.id, idempotency_key=f"hp-{uuid.uuid4()}")
    _, envelope = authorize_execution_scope(db_session, owner_id=user.id, proposal_id=proposal.id,
        authorized_by="founder", authorized_paths=["docs/**"], authorized_capabilities=["read_file"],
        authorized_risk="low", envelope_idempotency_key=f"he-{uuid.uuid4()}")
    cycle = create_authorized_cycle(db_session, owner_id=user.id, goal_id=goal.id, envelope_id=envelope.id,
                                    verifier_profile_id=verifier.id)
    db_session.commit()
    claim = claim_next_cycle(db_session, worker_id="http-worker", lease_seconds=60)
    run_claimed_cycle_step(db_session, cycle_id=claim[0], owner_id=claim[1], worker_id="http-worker", generation=claim[2])
    bind_cycle_dispatch(db_session, owner_id=user.id, cycle_id=cycle.id, external_agent_id="http-agent",
                        dispatch_receipt="synthetic-http-dispatch")
    db_session.commit()
    monkeypatch.setattr(
        "app.routers.provider_webhooks.get_settings",
        lambda: SimpleNamespace(cursor_cloud_webhook_secret=SECRET, provider_webhook_max_body_bytes=65536, provider_webhook_max_age_seconds=300),
    )
    now = datetime.now(timezone.utc)
    raw, sig = _sign({"event":"statusChange","id":"http-agent","status":"RUNNING","timestamp":now.isoformat()})
    response = client.post(
        f"/api/provider-events/cursor/{user.id}",
        content=raw,
        headers={"X-Webhook-Signature": sig, "X-Webhook-Id": "http-1", "User-Agent": WEBHOOK_USER_AGENT, "Content-Type": "application/json"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "progress_recorded"
    forged = client.post(
        f"/api/provider-events/cursor/{user.id}", content=raw,
        headers={"X-Webhook-Signature": "sha256=" + "0"*64, "X-Webhook-Id": "http-2",
                 "User-Agent": WEBHOOK_USER_AGENT, "Content-Type": "application/json"},
    )
    assert forged.status_code == 401


def test_runtime_role_cannot_delete_cycle_or_mutate_webhook_receipt(db_session, make_verified_user):
    user, _password = make_verified_user()
    _rls(db_session, user.id)
    with pytest.raises(ProgrammingError):
        db_session.execute(text("DELETE FROM authorized_operating_cycles WHERE owner_id=:owner"), {"owner": user.id})
    db_session.rollback()
    _rls(db_session, user.id)
    with pytest.raises(ProgrammingError):
        db_session.execute(text("UPDATE provider_webhook_events SET outcome='forged' WHERE owner_id=:owner"), {"owner": user.id})
    db_session.rollback()
