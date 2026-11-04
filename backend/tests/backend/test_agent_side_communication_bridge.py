"""Agent-side communication bridge over the existing #302/#303 gateway."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text as sa_text

from app.agent_coordination.agent_side import run_agent_side
from app.agent_coordination.delivery import (
    issue_bridge_credential,
    resolve_bridge,
    revoke_bridge_credential,
    submit_receipt,
)
from app.agent_coordination.persist import find_owner_assignments
from app.agent_coordination.durable import load_owner_ledger
from app.agent_coordination.github_intake import passing_required_runs
from app.agent_coordination.lifecycle import ingest_lifecycle_event
from app.agent_coordination.local_bridge import LocalAuthorizedBridge
from app.agent_coordination.transport import capability_matrix
from app.agent_coordination.transports.executor import RestrictedAssignmentExecutor
from app.agent_coordination.transports.vendor import BlockedVendorTransport
from app.request_context import current_user_id as current_user_id_var
from app.worker import Worker

HEAD = "74ebda974ff6952c7c7ca15f837430a73c594b43"
PARENT = "ff28b51429a5bc61f78308b657b8d57b2e5bf152"


def _set_rls(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture(autouse=True, scope="module")
def _privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _owner(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls(db_session, user.id)
    return user.id


def _observation():
    checks, workflows = passing_required_runs(HEAD)
    return {
        "number": 303,
        "title": "coordination handover",
        "draft": False,
        "merged": False,
        "state": "open",
        "html_url": "https://github.com/d1n095/LifeAI/pull/303",
        "head_sha": HEAD,
        "base_sha": PARENT,
        "head_branch": "cursor/coordination-operational-handover-91c1",
        "base_branch": "cursor/agent-messaging-delivery-gateway-91c1",
        "ancestry": [HEAD, PARENT],
        "reviews": [],
        "check_runs": checks,
        "workflow_runs": workflows,
    }


def _prepare(db, owner_id, recipient="cursor"):
    ingest_lifecycle_event(
        db,
        owner_id=owner_id,
        actor="worker",
        payload={"kind": "github_observation", "event_id": f"gh-{recipient}", "observation": _observation()},
    )
    db.commit()
    issued = issue_bridge_credential(db, owner_id=owner_id, recipient=recipient, actor="founder")
    db.commit()
    Worker()._reconcile_durable_coordination(db)
    db.commit()
    return issued


def test_capability_matrix_does_not_equate_surfaces():
    rows = {row["transport_id"]: row for row in capability_matrix()}
    assert rows["authorized_gateway_runner"]["surface"] == "agent_side_runner"
    assert rows["authorized_gateway_runner"]["usable"] is True
    assert rows["local_cli_execution"]["surface"] == "local_cli"
    assert rows["local_cli_execution"]["note"]
    assert rows["provider_model_api"]["usable"] is False
    assert rows["remote_background_agent"]["usable"] is False
    for name in ("cursor", "claude", "codex"):
        session = rows[f"vendor_session_{name}"]
        assert session["usable"] is False
        assert session["surface"] == "private_vendor_session"
        assert session["missing_capability"] == "vendor_private_session"


def test_end_to_end_agent_side_runner(db_session, make_verified_user, client):
    owner_id = _owner(db_session, make_verified_user)
    issued = _prepare(db_session, owner_id)
    result = run_agent_side(token=issued["token"], request=client.request, execute=True)
    assert result["outcome"] == "applied"
    assert result["delivered_count"] == 1
    assert result["vendor_delivery_claimed"] is False
    receipt = result["receipts"][0]
    assert receipt["delivered"]["status"] == "delivered"
    assert receipt["ack"]["status"] == "acknowledged"
    assert receipt["execute"]["ran"] is True
    assert receipt["execute"]["interpolated_instruction"] is False
    assert receipt["complete"]["status"] == "completed"
    assert receipt["complete"]["independently_verified"] is False

    db_session.expire_all()
    _set_rls(db_session, owner_id)
    Worker()._reconcile_durable_coordination(db_session)
    db_session.commit()
    view = load_owner_ledger(db_session, owner_id=owner_id).founder_view()
    flags = view["operational_handover"]["agent_side_bridge"]
    assert flags["adapter_installed"] is True
    assert flags["founder_authorized"] is True
    assert flags["assignment_claimed"] is True
    assert flags["execution_confirmed"] is True
    assert flags["completion_verified"] is True
    examiners = [row for row in view["obligations"] if row["agent_identity"] == "examiner"]
    assert len(examiners) == 1
    assert view["operational_handover"]["external_vendor_operations"]["usable"] is False
    assert view["first_cycle_go"] is False


def test_replay_duplicate_revoked_and_wrong_recipient(db_session, make_verified_user, client):
    owner_id = _owner(db_session, make_verified_user)
    issued = _prepare(db_session, owner_id)
    first = run_agent_side(token=issued["token"], request=client.request)
    second = run_agent_side(token=issued["token"], request=client.request)
    assert first["delivered_count"] == 1
    assert second["delivered_count"] == 0

    claude = issue_bridge_credential(db_session, owner_id=owner_id, recipient="claude", actor="founder")
    db_session.commit()
    foreign = LocalAuthorizedBridge(token=claude["token"], request=client.request)
    inbox = foreign.list_inbox()
    assert inbox == []

    revoke_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder")
    db_session.commit()
    revoked = client.get("/api/mainai/agent-gateway/inbox", headers={"Authorization": f"Bearer {issued['token']}"})
    assert revoked.status_code in {401, 403}


def test_malformed_out_of_order_and_no_interpolation(db_session, make_verified_user, client):
    owner_id = _owner(db_session, make_verified_user)
    issued = _prepare(db_session, owner_id)
    pulled = LocalAuthorizedBridge(token=issued["token"], request=client.request).pull()
    message_id = pulled[0]["message"]["message_id"]
    early = submit_receipt(
        db_session,
        owner_id=owner_id,
        recipient="cursor",
        payload={
            "kind": "execution_progress",
            "event_id": "early-exec",
            "message_id": message_id,
            "evidence": {"kind": "adapter_heartbeat"},
        },
    )
    assert early["outcome"] == "stale_rejected"
    forged = submit_receipt(
        db_session,
        owner_id=owner_id,
        recipient="cursor",
        payload={
            "kind": "acknowledge",
            "event_id": "forged-cert",
            "message_id": message_id,
            "independently_verified": True,
        },
    )
    assert forged["outcome"] == "forged_rejected"
    instruction = {"message": {"message_id": "m1", "instruction": "github-pr-303-build; rm -rf /"}}
    executed = RestrictedAssignmentExecutor().run(instruction)
    assert executed["interpolated_instruction"] is False
    assert executed["ran"] is True
    assert "rm -rf" not in str(executed.get("summary") or "")


def test_vendor_and_disconnected_fail_closed():
    blocked = BlockedVendorTransport(
        transport_id="vendor_session_cursor",
        surface="private_vendor_session",
        missing="vendor_private_session",
        bound_identity="cursor",
    )
    assert blocked.deliver()["outcome"] == "blocked"
    assert blocked.deliver()["vendor_delivery_claimed"] is False
    assert run_agent_side(token="")["missing_capability"] == "founder_agent_side_authorization"


def test_http_transport_inventory_requires_founder(client):
    assert client.get("/api/mainai/founder-ops/agent-gateway/transports").status_code in {401, 403}


def test_cs1_issued_bridge_survives_fresh_session_without_prior_assignment(db_session, make_verified_user, client):
    owner_id = _owner(db_session, make_verified_user)
    assert find_owner_assignments(db_session, owner_id=owner_id) == []
    issued = issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder")
    db_session.commit()
    assert issued["outcome"] == "issued"
    assert find_owner_assignments(db_session, owner_id=owner_id)
    db_session.expire_all()
    _set_rls(db_session, owner_id)
    resolved = resolve_bridge(db_session, token=issued["token"])
    assert resolved is not None
    assert resolved["recipient"] == "cursor"
    inbox = client.get("/api/mainai/agent-gateway/inbox", headers={"Authorization": f"Bearer {issued['token']}"})
    assert inbox.status_code == 200
    assert inbox.json()["vendor_delivery_claimed"] is False


def test_cs2_warm_existing_assignment_still_resolves(db_session, make_verified_user, client):
    owner_id = _owner(db_session, make_verified_user)
    issued = _prepare(db_session, owner_id)
    db_session.expire_all()
    _set_rls(db_session, owner_id)
    assert resolve_bridge(db_session, token=issued["token"]) is not None
    inbox = client.get("/api/mainai/agent-gateway/inbox", headers={"Authorization": f"Bearer {issued['token']}"})
    assert inbox.status_code == 200
