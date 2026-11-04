"""Claude pistop_v1 / indepcomp_v1 plus PI-DEDUP and RST-4 regressions."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text as sa_text

from app.agent_coordination.durable import DurableCoordinationService, load_owner_ledger
from app.agent_coordination.external_connectivity.cursor_cloud import WEBHOOK_USER_AGENT
from app.agent_coordination.persist import persist_owner_ledger
from app.agent_coordination.provider_ingestion import (
    bind_provider_assignment,
    continue_after_independent_verification,
    ingest_authenticated_provider_event,
    persist_ingestion,
)
from app.mainai_restrictions import redact_secrets
from app.mainai_verification_registry import VerificationRegistryError, record_verification_attestation
from app.request_context import current_user_id as current_user_id_var
from app.workforce.kill_switch import KillSwitchError, activate_kill_switch, prove_no_reusable_live_authority

SECRET = "fixture-webhook-secret"
AGENT = "bc_probe_agent"
SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture(autouse=True, scope="module")
def _privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _set_rls(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _sign(payload: dict) -> tuple[bytes, str]:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return raw, "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


def _bound(owner_id: str) -> DurableCoordinationService:
    service = DurableCoordinationService()
    assignment_id = str(uuid.uuid4())
    service.assign(
        agent_identity="cursor",
        workstream="provider_integration",
        owner_id=owner_id,
        instruction_id="probe",
        assignment_id=assignment_id,
        actor="founder",
    )
    bind_provider_assignment(
        service,
        owner_id=owner_id,
        assignment_id=assignment_id,
        provider="cursor",
        external_agent_id=AGENT,
    )
    return service


def test_authority_unestablished_without_durable_session():
    owner = str(uuid.uuid4())
    service = _bound(owner)
    raw, signature = _sign({"event": "statusChange", "id": AGENT, "status": "FINISHED", "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")})
    result = ingest_authenticated_provider_event(
        service,
        owner_id=owner,
        secret=SECRET,
        raw_body=raw,
        signature=signature,
        user_agent=WEBHOOK_USER_AGENT,
        fixture=True,
    )
    assert result["outcome"] == "authority_unestablished"
    assert result["independently_verified"] is False


def test_pistop_v1_stop_refuses_transition_across_restart(db_session, make_verified_user):
    user, _password = make_verified_user()
    owner_id = user.id
    _set_rls(db_session, owner_id)
    service = _bound(str(owner_id))
    assignment_id = next(iter(service._provider_ingestion["bindings"]))
    persist_owner_ledger(db_session, owner_id=owner_id, service=service, actor="probe")
    db_session.commit()
    state = activate_kill_switch(db_session, owner_id=owner_id, reason="pistop_v1")
    db_session.commit()
    assert state.active is True
    raw, signature = _sign({"event": "statusChange", "id": AGENT, "status": "FINISHED", "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")})
    refused = ingest_authenticated_provider_event(
        service,
        owner_id=str(owner_id),
        secret=SECRET,
        raw_body=raw,
        signature=signature,
        user_agent=WEBHOOK_USER_AGENT,
        fixture=True,
        db=db_session,
    )
    assert refused["outcome"] == "stop_rejected"
    assert service.get(assignment_id)["status"] == "assigned"
    db_session.expire_all()
    _set_rls(db_session, owner_id)
    restored = load_owner_ledger(db_session, owner_id=owner_id)
    again = ingest_authenticated_provider_event(
        restored,
        owner_id=str(owner_id),
        secret=SECRET,
        raw_body=raw,
        signature=signature,
        user_agent=WEBHOOK_USER_AGENT,
        fixture=True,
        db=db_session,
    )
    assert again["outcome"] == "stop_rejected"
    assert restored.get(assignment_id)["status"] == "assigned"
    assert prove_no_reusable_live_authority(db_session, owner_id=owner_id) is True
    with pytest.raises(KillSwitchError):
        persist_ingestion(db_session, owner_id=owner_id, service=restored)
    with pytest.raises(KillSwitchError):
        persist_ingestion(db_session, owner_id=owner_id, service=restored)


def test_indepcomp_v1_provider_completion_is_not_independent_pass(db_session, superuser_db, make_verified_user):
    user, _password = make_verified_user()
    owner_id = user.id
    _set_rls(db_session, owner_id)
    service = _bound(str(owner_id))
    assignment_id = next(iter(service._provider_ingestion["bindings"]))
    raw, signature = _sign({"event": "statusChange", "id": AGENT, "status": "FINISHED", "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")})
    done = ingest_authenticated_provider_event(
        service,
        owner_id=str(owner_id),
        secret=SECRET,
        raw_body=raw,
        signature=signature,
        user_agent=WEBHOOK_USER_AGENT,
        fixture=True,
        db=db_session,
    )
    assert done["transition"] == "completed"
    assert done["independently_verified"] is False
    assert done["verification_required"] is True
    missing = continue_after_independent_verification(
        db_session,
        service,
        owner_id=str(owner_id),
        assignment_id=assignment_id,
        builder_identity="cursor",
        candidate_sha=SHA,
    )
    assert missing["outcome"] == "verification_required"
    assert missing["independently_verified"] is False
    with pytest.raises(VerificationRegistryError):
        record_verification_attestation(
            superuser_db,
            component_id=f"provider_assignment:{assignment_id}",
            candidate_sha=SHA,
            builder_identity="cursor",
            examiner_identity="cursor",
            review_result="PASS",
            owner_id=owner_id,
            evidence_summary="builder cannot self-certify",
        )
    record_verification_attestation(
        superuser_db,
        component_id=f"provider_assignment:{assignment_id}",
        candidate_sha=SHA,
        builder_identity="cursor",
        examiner_identity="examiner",
        review_result="PASS",
        owner_id=owner_id,
        evidence_summary="independent review of provider assignment",
    )
    superuser_db.commit()
    attested = continue_after_independent_verification(
        db_session,
        service,
        owner_id=str(owner_id),
        assignment_id=assignment_id,
        builder_identity="cursor",
        candidate_sha=SHA,
    )
    assert attested["outcome"] == "attested"
    assert attested["independently_verified"] is True
    assert service.get(assignment_id)["status"] == "completed"


def test_pi_dedup_without_provider_timestamp(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls(db_session, user.id)
    service = _bound(str(user.id))
    payload = {"event": "statusChange", "id": AGENT, "status": "FINISHED", "summary": "no-ts"}
    raw, signature = _sign(payload)
    first = ingest_authenticated_provider_event(
        service, owner_id=str(user.id), secret=SECRET, raw_body=raw, signature=signature, user_agent=WEBHOOK_USER_AGENT, fixture=True, db=db_session
    )
    assert first["outcome"] == "applied"
    replay = ingest_authenticated_provider_event(
        service, owner_id=str(user.id), secret=SECRET, raw_body=raw, signature=signature, user_agent=WEBHOOK_USER_AGENT, fixture=True, db=db_session
    )
    assert replay["outcome"] == "replay_ignored"


def _assembled(*parts: str) -> str:
    """Build fixture tokens at runtime so the source never holds a scanner-shaped secret."""
    return "".join(parts)


def test_rst4_bare_credential_values_are_redacted():
    github = _assembled("gh", "p_", "abcdefghijklmnopqrstuvwxyz123456")
    openai = _assembled("sk", "-proj-", "abcdefghijklmnopqrstuvwxyz012345")
    cursor = _assembled("key", "_live_", "abc123secretvalue")
    slack = _assembled("xox", "b-", "exampletokenvalue")
    redacted = redact_secrets(
        {
            "comment": f"rotate {github} now",
            "openai": openai,
            "cursor": cursor,
            "slack": slack,
            "embedded": f"see {slack} and {cursor}",
        }
    )
    blob = json.dumps(redacted)
    assert redacted["comment"] == "rotate [redacted] now"
    assert redacted["openai"] == "[redacted]"
    assert redacted["cursor"] == "[redacted]"
    assert redacted["slack"] == "[redacted]"
    assert redacted["embedded"] == "see [redacted] and [redacted]"
    assert _assembled("gh", "p_") not in blob
    assert _assembled("sk", "-proj") not in blob
    assert _assembled("key", "_live_") not in blob
    assert _assembled("xox", "b-") not in blob
