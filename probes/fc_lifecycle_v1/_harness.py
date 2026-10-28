"""Shared harness for the production agent-lifecycle integration gate (DB-backed).

Real production paths: app.agent_coordination.provider_ingestion (authenticated provider
event -> binding -> dedup/expiry/reorder/STOP fence -> durable transition), persist/load owner
ledger, and app.mainai_verification_registry (independent certification). Provider events are
locally generated and CORRECTLY SIGNED with a caller-held HMAC secret: that is
AUTHENTICATED-LOCAL evidence, NOT a genuine external Cursor delivery (no api.cursor.com call).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import pytest
from sqlalchemy import text as sa_text

from app.request_context import current_user_id as current_user_id_var

# A caller-held webhook secret. HMAC over the raw body authenticates the body; holding the
# secret ourselves makes these AUTHENTICATED-LOCAL events, never genuine external delivery.
SECRET = "whsec_examiner_lifecycle"
UA = "Cursor-Agent-Webhook/1.0"


@pytest.fixture(autouse=True, scope="module")
def _privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    return user.id


def sig(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()


def event_body(*, status: str, agent_id: str, timestamp: str | None = None) -> bytes:
    payload = {"event": "statusChange", "status": status, "id": agent_id}
    if timestamp:
        payload["timestamp"] = timestamp
    return json.dumps(payload).encode("utf-8")


def new_service():
    from app.agent_coordination.durable import DurableCoordinationService

    return DurableCoordinationService()


def assign(service, *, owner, agent, workstream, instruction):
    r = service.assign(
        agent_identity=agent,
        workstream=workstream,
        owner_id=str(owner),
        instruction_id=instruction,
        actor="founder",
    )
    assert r.accepted, r
    return r.assignment_id


def bind(service, *, owner, assignment_id, agent_id, recipient):
    from app.agent_coordination.provider_ingestion import bind_provider_assignment

    return bind_provider_assignment(
        service,
        owner_id=str(owner),
        assignment_id=assignment_id,
        provider="cursor",
        external_agent_id=agent_id,
        recipient=recipient,
    )


def ingest(service, *, owner, body, signature=None, user_agent=UA, db=None, fixture=False):
    from app.agent_coordination.provider_ingestion import ingest_authenticated_provider_event

    return ingest_authenticated_provider_event(
        service,
        owner_id=str(owner),
        secret=SECRET,
        raw_body=body,
        signature=signature if signature is not None else sig(body),
        user_agent=user_agent,
        fixture=fixture,
        db=db,
    )


def provider_events(service, assignment_id: str, *, transition: str | None = None) -> list:
    rows = [e for e in (service._provider_ingestion.get("events") or []) if e.get("assignment_id") == assignment_id]
    if transition is not None:
        rows = [e for e in rows if e.get("transition") == transition]
    return rows
