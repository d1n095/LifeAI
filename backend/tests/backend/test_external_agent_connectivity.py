"""Real external-agent connectivity: official catalog, identity, fail-closed first path."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

from app.agent_coordination.connect import operational_handover_view
from app.agent_coordination.delivery import mint_bridge_token
from app.agent_coordination.durable import DurableCoordinationService, FIRST_CYCLE_GO
from app.agent_coordination.external_connectivity import (
    CursorCloudAgentClient,
    IdentityClass,
    capability_verdict,
    classify_credential,
    inspect_external_connectivity,
    official_interface_catalog,
    propose_ledger_event,
    selected_first_path,
    unattended_trial_plan,
    verify_v0_webhook,
    vertical_slice,
)
from app.agent_coordination.external_connectivity.cursor_cloud import WEBHOOK_USER_AGENT
from app.agent_coordination.external_connectivity.identity import local_token_is_not_provider_identity


def _signed_webhook(secret: str, payload: dict) -> tuple[bytes, str]:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return raw, signature


def test_catalog_selects_cursor_cloud_and_blocks_unavailable_providers():
    catalog = official_interface_catalog()
    providers = {(row["provider"], row["surface"]) for row in catalog}
    assert ("cursor", "cloud_agents_api_v1") in providers
    assert ("cursor", "cloud_agents_api_v0_webhooks") in providers
    assert ("claude", "headless_cli_and_agent_sdk") in providers
    assert ("codex", "github_coding_agent") in providers
    first = selected_first_path()
    assert first["path_id"] == "cursor_cloud_agents_api_v1"
    assert first["live_round_trip_possible_here"] is False
    assert first["vendor_delivery_claimed"] is False
    assert first["first_cycle_go"] is False
    assert FIRST_CYCLE_GO is False
    for row in catalog:
        assert row["vendor_delivery_claimed"] is False
        if row["provider"] in {"claude", "codex"}:
            assert row["usable_now"] is False
            assert row["status"] == "blocked"
        if row.get("selected_first_path"):
            assert row["usable_now"] is False


def test_bridge_token_is_not_provider_identity():
    token, _record = mint_bridge_token(owner_id=str(uuid.uuid4()), recipient="cursor")
    identity = classify_credential(token, source="cursor_cloud_api_key")
    assert identity is IdentityClass.local_bridge_token
    assert local_token_is_not_provider_identity(identity)
    assert classify_credential(token, source="LIFE_AGENT_BRIDGE_TOKEN") is IdentityClass.local_bridge_token


def test_this_runtime_socket_is_not_a_delivery_api():
    socket = "/tmp/this-cloud-agent.sock"
    identity = classify_credential(socket, source="CURSOR_AGENT_SOCKET", environ={"CURSOR_AGENT_SOCKET": socket})
    assert identity is IdentityClass.this_cloud_agent_runtime
    reused = classify_credential(socket, source="cursor_cloud_api_key", environ={"CURSOR_AGENT_SOCKET": socket})
    assert reused is IdentityClass.this_cloud_agent_runtime


def test_create_and_follow_up_are_fail_closed_without_and_with_candidate_key():
    empty = CursorCloudAgentClient(api_key=None).create_agent("must not run")
    assert empty["outcome"] == "blocked"
    assert empty["live_http_attempted"] is False
    assert empty["vendor_delivery_claimed"] is False
    token, _record = mint_bridge_token(owner_id=str(uuid.uuid4()), recipient="cursor")
    stolen = CursorCloudAgentClient(api_key=token, founder_authorized=True).create_agent("must not run")
    assert stolen["missing_capability"] == "provider_identity_required"
    assert stolen["identity_class"] == IdentityClass.local_bridge_token.value
    gated = CursorCloudAgentClient(
        api_key="key_that_is_not_a_bridge_token",
        founder_authorized=True,
        environ={"LIFE_CURSOR_CLOUD_AGENTS_ENABLED": "true", "LIFE_CURSOR_CLOUD_AGENTS_LIVE_MUTATE": "true"},
    )
    created = gated.create_agent("must not spend")
    followed = gated.follow_up("bc_test", "must not spend")
    assert created["outcome"] == "blocked"
    assert followed["outcome"] == "blocked"
    assert created["live_http_attempted"] is False
    assert created["missing_capability"] == "founder_cursor_cloud_live_mutate_authorization"


def test_webhook_hmac_accepts_official_signature_and_rejects_tamper():
    secret = "webhook-secret-for-tests"
    payload = {
        "event": "statusChange",
        "id": "bc_fixture",
        "status": "FINISHED",
        "summary": "fixture only",
        "target": {"branchName": "cursor/demo", "prUrl": "https://example.invalid/pr/1"},
    }
    raw, signature = _signed_webhook(secret, payload)
    accepted = verify_v0_webhook(
        secret=secret,
        raw_body=raw,
        signature=signature,
        user_agent=WEBHOOK_USER_AGENT,
        fixture=True,
    )
    assert accepted["outcome"] == "accepted"
    assert accepted["provider_authenticated"] is False
    assert accepted["vendor_delivery_claimed"] is False
    assert accepted["independently_verified"] is False
    rejected = verify_v0_webhook(
        secret=secret,
        raw_body=raw,
        signature="sha256=" + ("00" * 32),
        user_agent=WEBHOOK_USER_AGENT,
        fixture=True,
    )
    assert rejected["outcome"] == "rejected"
    assert rejected["provider_authenticated"] is False


def test_verified_fixture_proposes_ledger_event_but_does_not_persist_or_claim_delivery():
    secret = "webhook-secret-for-tests"
    payload = {"event": "statusChange", "id": "bc_fixture", "status": "FINISHED", "summary": "fixture"}
    raw, signature = _signed_webhook(secret, payload)
    verified = verify_v0_webhook(secret=secret, raw_body=raw, signature=signature, fixture=True)
    proposed = propose_ledger_event(verified)
    assert proposed["persisted"] is False
    assert proposed["proposed"] is True
    assert proposed["completion_evidence"]["proposed_delivery_status"] == "completed"
    assert proposed["agent_identity"]["provider_authenticated"] is False
    assert proposed["vendor_delivery_claimed"] is False
    assert proposed["independently_verified"] is False
    assert propose_ledger_event({"outcome": "rejected"})["proposed"] is False


def test_inspect_and_handover_never_claim_vendor_delivery():
    view = inspect_external_connectivity(probe_network=False)
    assert view["vendor_delivery_claimed"] is False
    assert view["independently_verified"] is False
    assert view["first_cycle_go"] is False
    assert view["amends_303_through_306"] is False
    assert view["cursor_cloud_create"]["outcome"] == "blocked"
    assert view["external_vendor_operations"]["usable"] is False
    handover = operational_handover_view(DurableCoordinationService())
    assert handover["vendor_delivery_claimed"] is False
    assert handover["external_vendor_operations"]["usable"] is False
    assert handover["external_agent_connectivity"]["selected_first_path"]["path_id"] == "cursor_cloud_agents_api_v1"
    assert handover["first_cycle_go"] is False
    assert handover["unattended_operating_cycle_ready"] is False
    assert handover["unattended_autonomy_ready"] is False
    assert handover["local_gateway_is_not_unattended_independence"] is True
    assert "not operational independence" in handover["answer"]


def test_capability_verdict_is_not_ready_and_does_not_overclaim():
    verdict = capability_verdict()
    assert verdict["ready"] is False
    assert verdict["unattended_operating_cycle_ready"] is False
    assert verdict["unattended_autonomy_ready"] is False
    assert verdict["local_gateway_sufficient"] is False
    assert verdict["green_ci_sufficient"] is False
    assert verdict["disconnected_prototype_sufficient"] is False
    assert verdict["vendor_delivery_claimed"] is False
    assert verdict["independently_verified"] is False
    assert verdict["first_cycle_go"] is False
    assert verdict["external_agent_connected"] is False
    assert verdict["single_vendor_mandatory"] is False
    assert verdict["amends_303_through_306"] is False
    assert "FA-9" in " ".join(verdict["unattended_trial"]["codex_owned_gates_remain_authoritative"])
    assert "CO-10" in " ".join(verdict["unattended_trial"]["codex_owned_gates_remain_authoritative"])
    assert all(row["satisfied"] is False for row in verdict["requirements"])
    assert len(verdict["requirements"]) == 10
    assert "NOT READY" in verdict["verdict"]


def test_vertical_slice_is_one_provider_and_not_live():
    slice_spec = vertical_slice()
    assert slice_spec["provider"] == "cursor"
    assert slice_spec["additional_providers"] == []
    assert slice_spec["complete"] is False
    assert slice_spec["vendor_delivery_claimed"] is False
    assert all(step["live"] is False for step in slice_spec["steps"])


def test_unattended_trial_cannot_start():
    trial = unattended_trial_plan()
    assert trial["duration_hours"] == 72
    assert trial["start_authorized"] is False
    assert trial["unattended_autonomy_ready"] is False
    assert trial["zero_routine_founder_message_forwarding"] is True
    assert "fa9_co10_privilege_and_publication_gates_unsatisfied" in trial["cannot_start_while"]
    assert "independent_certification_blocked" in trial["cannot_start_while"]
    inspect = inspect_external_connectivity(probe_network=False)
    assert inspect["capability_verdict"]["unattended_trial"]["start_authorized"] is False
    assert inspect["unattended_operating_cycle_ready"] is False
