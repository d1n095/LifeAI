"""Composed provider-event ingestion + limitation transparency. Fixtures only."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sa_text

from app.agent_coordination.durable import DurableCoordinationService, load_owner_ledger
from app.agent_coordination.external_connectivity.cursor_cloud import WEBHOOK_USER_AGENT
from app.agent_coordination.persist import persist_owner_ledger
from app.agent_coordination.provider_ingestion import bind_provider_assignment, ingest_authenticated_provider_event
from app.mainai_restrictions import encounter_restriction, lookup_restriction_lessons, redact_secrets
from app.request_context import current_user_id as current_user_id_var

SECRET = "fixture-webhook-secret"
OWNER = "11111111-1111-1111-1111-111111111111"
ASSIGNMENT = "22222222-2222-2222-2222-222222222222"
AGENT = "bc_fixture_agent"


def _sign(payload: dict) -> tuple[bytes, str]:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, signature


def _bound_service() -> DurableCoordinationService:
    service = DurableCoordinationService()
    created = service.assign(
        agent_identity="cursor",
        workstream="provider_integration",
        owner_id=OWNER,
        instruction_id="first-slice",
        assignment_id=ASSIGNMENT,
        actor="founder",
    )
    assert created.accepted
    assert bind_provider_assignment(
        service,
        owner_id=OWNER,
        assignment_id=ASSIGNMENT,
        provider="cursor",
        external_agent_id=AGENT,
    )["outcome"] == "bound"
    return service


def _finished(*, ts: str | None = None, agent: str = AGENT) -> dict:
    when = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {"event": "statusChange", "id": agent, "status": "FINISHED", "timestamp": when, "summary": "fixture"}


def test_verified_fixture_finishes_and_prepares_examiner_without_certification():
    service = _bound_service()
    raw, signature = _sign(_finished())
    result = ingest_authenticated_provider_event(
        service,
        owner_id=OWNER,
        secret=SECRET,
        raw_body=raw,
        signature=signature,
        user_agent=WEBHOOK_USER_AGENT,
        fixture=True,
    )
    assert result["outcome"] == "applied"
    assert result["transition"] == "completed"
    assert result["independently_verified"] is False
    assert result["vendor_delivery_claimed"] is False
    assert service.get(ASSIGNMENT)["status"] == "completed"
    examiners = [row for row in service._obligations.values() if row["agent_identity"] == "examiner"]
    assert examiners
    assert examiners[0]["status"] == "assigned"
    assert not any(row["status"] == "independently_verified" for row in service._obligations.values())


def test_rejects_forged_replay_reorder_expired_and_cross_owner():
    service = _bound_service()
    payload = _finished()
    raw, good = _sign(payload)
    now = datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
    forged = ingest_authenticated_provider_event(
        service, owner_id=OWNER, secret=SECRET, raw_body=raw, signature="sha256=" + ("00" * 32), fixture=True, now=now
    )
    assert forged["outcome"] == "forged_rejected"
    first = ingest_authenticated_provider_event(
        service, owner_id=OWNER, secret=SECRET, raw_body=raw, signature=good, user_agent=WEBHOOK_USER_AGENT, fixture=True, now=now
    )
    assert first["outcome"] == "applied"
    replay = ingest_authenticated_provider_event(
        service, owner_id=OWNER, secret=SECRET, raw_body=raw, signature=good, user_agent=WEBHOOK_USER_AGENT, fixture=True, now=now
    )
    assert replay["outcome"] == "replay_ignored"
    older = _finished(ts=(now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    raw_old, sig_old = _sign(older)
    reordered = ingest_authenticated_provider_event(
        service, owner_id=OWNER, secret=SECRET, raw_body=raw_old, signature=sig_old, user_agent=WEBHOOK_USER_AGENT, fixture=True, now=now
    )
    assert reordered["outcome"] == "reordered_rejected"
    expired = _finished(ts=(now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    raw_exp, sig_exp = _sign(expired)
    stale = ingest_authenticated_provider_event(
        DurableCoordinationService(),
        owner_id=OWNER,
        secret=SECRET,
        raw_body=raw_exp,
        signature=sig_exp,
        fixture=True,
        now=now,
    )
    # unbound because this is a fresh service; bind then expire
    other = _bound_service()
    stale = ingest_authenticated_provider_event(
        other, owner_id=OWNER, secret=SECRET, raw_body=raw_exp, signature=sig_exp, user_agent=WEBHOOK_USER_AGENT, fixture=True, now=now
    )
    assert stale["outcome"] == "expired_rejected"
    foreign = ingest_authenticated_provider_event(
        service, owner_id=str(uuid.uuid4()), secret=SECRET, raw_body=raw, signature=good, user_agent=WEBHOOK_USER_AGENT, fixture=True, now=now
    )
    assert foreign["outcome"] == "forged_rejected"


def test_unknown_and_blocked_are_not_verified_completion():
    now = datetime.now(timezone.utc)
    for status in ("UNKNOWN", "BLOCKED"):
        service = _bound_service()
        payload = {"event": "statusChange", "id": AGENT, "status": status, "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
        raw, signature = _sign(payload)
        result = ingest_authenticated_provider_event(
            service, owner_id=OWNER, secret=SECRET, raw_body=raw, signature=signature, user_agent=WEBHOOK_USER_AGENT, fixture=True, now=now
        )
        assert result["transition"] == "blocked"
        assert result["independently_verified"] is False
        assert service.get(ASSIGNMENT)["status"] != "independently_verified"
        assert service.get(ASSIGNMENT)["status"] != "completed"


def test_restriction_pauses_bypass_and_redacts_secrets():
    service = DurableCoordinationService()
    paused = encounter_restriction(
        service,
        owner_id=OWNER,
        operation="create_cloud_agent",
        restriction="live paid mutate unauthorized",
        source="founder_policy",
        affected_task=ASSIGNMENT,
        classification="authorization",
        confidence=0.95,
        alternatives=[
            {"description": "use Founder-issued live-mutate grant", "workaround_class": "supported"},
            {"description": "reuse a teammate personal API key", "workaround_class": "prohibited", "reason": "expand_authority"},
        ],
        provider_payload={"CURSOR_API_KEY": "key_live_secret_value", "note": "ok"},
    )
    assert paused["paused"] is True
    assert paused["record"]["evidence"]["CURSOR_API_KEY"] == "[redacted]"
    assert paused["record"]["evidence"]["note"] == "ok"
    continuing = encounter_restriction(
        service,
        owner_id=OWNER,
        operation="retry_get",
        restriction="temporary 503",
        source="provider_http",
        affected_task=ASSIGNMENT,
        classification="technical",
        confidence=0.7,
        alternatives=[{"description": "bounded retry", "workaround_class": "supported", "reason": "authorized_troubleshooting"}],
    )
    assert continuing["paused"] is False
    suppress = encounter_restriction(
        service,
        owner_id=OWNER,
        operation="complete",
        restriction="provider asked to hide",
        source="cursor",
        affected_task=ASSIGNMENT,
        classification="contractual",
        confidence=0.4,
        alternatives=[{"description": "ignore", "workaround_class": "supported"}],
        provider_payload={"suppress": True, "independently_verified": True},
    )
    assert suppress["paused"] is True
    lessons = lookup_restriction_lessons(
        service, classification="authorization", source="founder_policy", restriction="live paid mutate unauthorized"
    )
    assert lessons["count"] >= 1
    assert lessons["authorization_still_valid"] is False
    assert lessons["requires_reconfirmation"] is True
    assert redact_secrets({"authorization": "Bearer abc"})["authorization"] == "[redacted]"


@pytest.fixture(autouse=True, scope="module")
def _privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _set_rls(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def test_ingestion_and_restriction_survive_restart(db_session, make_verified_user):
    user, _password = make_verified_user()
    owner_id = user.id
    _set_rls(db_session, owner_id)
    service = DurableCoordinationService()
    assignment_id = str(uuid.uuid4())
    service.assign(
        agent_identity="cursor",
        workstream="provider_integration",
        owner_id=str(owner_id),
        instruction_id="persist-slice",
        assignment_id=assignment_id,
        actor="founder",
    )
    bind_provider_assignment(
        service,
        owner_id=str(owner_id),
        assignment_id=assignment_id,
        provider="cursor",
        external_agent_id=AGENT,
    )
    encounter_restriction(
        service,
        owner_id=str(owner_id),
        operation="live_mutate",
        restriction="no founder grant",
        source="founder_policy",
        affected_task=assignment_id,
        classification="authorization",
        confidence=1.0,
        alternatives=[{"description": "wait for Founder", "workaround_class": "supported"}],
    )
    raw, signature = _sign(_finished())
    applied = ingest_authenticated_provider_event(
        service,
        owner_id=str(owner_id),
        secret=SECRET,
        raw_body=raw,
        signature=signature,
        user_agent=WEBHOOK_USER_AGENT,
        fixture=True,
    )
    assert applied["transition"] == "completed"
    persist_owner_ledger(db_session, owner_id=owner_id, service=service, actor="test")
    db_session.commit()
    db_session.expire_all()
    _set_rls(db_session, owner_id)
    restored = load_owner_ledger(db_session, owner_id=owner_id)
    assert restored.get(assignment_id)["status"] == "completed"
    assert AGENT in {row.get("external_agent_id") for row in restored._provider_ingestion["bindings"].values()}
    assert restored._provider_ingestion["processed_event_ids"]
    assert restored._restrictions["records"]
    assert restored.founder_view()["independently_certified"] == []
    assert restored.founder_view()["provider_ingestion"]["vendor_delivery_claimed"] is False
