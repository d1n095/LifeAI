"""CO-4b / CO-5 durable coordination: reconciliation, idempotency, handoff, adversarial cases.

These tests exercise the reusable ledger without inventing a second privilege system.
"""

from __future__ import annotations

from pathlib import Path

from app.agent_coordination.durable import (
    BLOCKS_FIRST_CYCLE,
    FIRST_CYCLE_GO,
    DurableCoordinationService,
    HandoffStatus,
    ObligationStatus,
    TransferTransport,
    artifact_sha256,
)


OWNER = "11111111-1111-1111-1111-111111111111"


def _assigned(service: DurableCoordinationService, **kwargs):
    result = service.assign(
        agent_identity=kwargs.get("agent_identity", "cursor"),
        workstream=kwargs.get("workstream", "co4b-co5"),
        owner_id=OWNER,
        instruction_id=kwargs.get("instruction_id", "instr-1"),
        event_id=kwargs.get("event_id"),
        actor="founder",
    )
    assert result.accepted, result
    return result


def test_instruction_is_not_execution():
    service = DurableCoordinationService()
    result = _assigned(service)
    row = service.get(result.assignment_id)
    assert row["status"] == ObligationStatus.assigned.value
    assert row["genuinely_active"] is True
    view = service.founder_view()
    assert view["first_cycle_go"] is False
    assert view["blocks_first_cycle"] is False
    assert FIRST_CYCLE_GO is False
    assert BLOCKS_FIRST_CYCLE is False


def test_duplicate_instruction_is_idempotent_and_does_not_reassign_after_completion():
    service = DurableCoordinationService()
    first = _assigned(service, instruction_id="same")
    service.acknowledge(assignment_id=first.assignment_id, agent_identity="cursor")
    service.record_execution_progress(
        assignment_id=first.assignment_id,
        agent_identity="cursor",
        evidence_kind="test_run",
        evidence={"passed": 1},
    )
    service.complete(assignment_id=first.assignment_id, agent_identity="cursor")
    replay = service.assign(
        agent_identity="cursor",
        workstream="co4b-co5",
        owner_id=OWNER,
        instruction_id="same",
        actor="founder",
    )
    assert replay.outcome == "idempotent"
    assert replay.assignment_id == first.assignment_id
    assert replay.status == ObligationStatus.completed.value
    assert len(service.founder_view()["obligations"]) == 1


def test_out_of_order_ack_is_buffered_then_applied():
    service = DurableCoordinationService()
    early = service.acknowledge(assignment_id="future", agent_identity="cursor", event_id="ack-1")
    assert early.outcome == "buffered"
    assigned = service.assign(
        agent_identity="cursor",
        workstream="co4b-co5",
        owner_id=OWNER,
        instruction_id="later",
        assignment_id="future",
        actor="founder",
    )
    assert assigned.assignment_id == "future"
    assert service.get("future")["status"] == ObligationStatus.acknowledged.value


def test_stale_queue_empty_does_not_clear_active_work():
    service = DurableCoordinationService()
    first = _assigned(service)
    claim = service.claim_queue_empty(reporter="cursor", event_id="q-1")
    assert claim.accepted is False
    assert claim.outcome == "stale_rejected"
    assert service.get(first.assignment_id)["status"] == ObligationStatus.assigned.value
    service.claim_queue_empty(reporter="cursor", event_id="q-1")
    assert service.founder_view()["queue_empty"] is False


def test_lost_ack_retry_is_idempotent():
    service = DurableCoordinationService()
    first = _assigned(service)
    a = service.acknowledge(assignment_id=first.assignment_id, agent_identity="cursor", event_id="ack-dup")
    b = service.acknowledge(assignment_id=first.assignment_id, agent_identity="cursor", event_id="ack-dup")
    assert a.outcome == "applied"
    assert b.outcome == "replay_ignored"
    assert service.get(first.assignment_id)["status"] == ObligationStatus.acknowledged.value


def test_stale_completion_after_supersede_cannot_create_second_assignment():
    service = DurableCoordinationService()
    first = _assigned(service, instruction_id="orig")
    successor = service.supersede(
        assignment_id=first.assignment_id,
        actor="founder",
        successor_agent="claude",
        instruction_id="orig-v2",
    )
    assert successor.status == ObligationStatus.assigned.value
    stale = service.complete(
        assignment_id=first.assignment_id,
        agent_identity="cursor",
        event_id="old-done",
        evidence={"generation": 1},
    )
    assert stale.outcome == "stale_rejected"
    retry = service.assign(
        agent_identity="cursor",
        workstream="co4b-co5",
        owner_id=OWNER,
        instruction_id="orig",
        actor="founder",
    )
    assert retry.assignment_id == successor.assignment_id
    assert service.get(first.assignment_id)["status"] == ObligationStatus.superseded.value
    assert len(service.founder_view()["obligations"]) == 2


def test_planning_without_execution_stalls_and_does_not_emit_new_plans():
    service = DurableCoordinationService()
    first = _assigned(service)
    for i in range(3):
        result = service.record_planning(
            assignment_id=first.assignment_id,
            agent_identity="cursor",
            event_id=f"plan-{i}",
            plan_ref=f"plan-{i}",
        )
    assert result.outcome == "stalled"
    assert result.detail["plan_generated"] is False
    row = service.get(first.assignment_id)
    assert row["stalled"] is True
    assert row["status"] == ObligationStatus.blocked.value
    again = service.record_planning(
        assignment_id=first.assignment_id,
        agent_identity="cursor",
        event_id="plan-extra",
    )
    assert again.detail["plan_generated"] is False
    assert len([e for e in row["events"] if e["kind"] == "planning_only"]) >= 3


def test_execution_progress_clears_stall():
    service = DurableCoordinationService()
    first = _assigned(service)
    service.record_planning(assignment_id=first.assignment_id, agent_identity="cursor", event_id="p1")
    service.record_planning(assignment_id=first.assignment_id, agent_identity="cursor", event_id="p2")
    progress = service.record_execution_progress(
        assignment_id=first.assignment_id,
        agent_identity="cursor",
        evidence_kind="code_change",
        evidence={"files": 1},
    )
    assert progress.status == ObligationStatus.executing.value
    assert service.get(first.assignment_id)["stalled"] is False


def test_forged_acknowledgment_and_cross_agent_impersonation():
    service = DurableCoordinationService()
    first = _assigned(service)
    forged = service.acknowledge(assignment_id=first.assignment_id, agent_identity="claude", event_id="fake-ack")
    assert forged.outcome == "forged_rejected"
    assert service.get(first.assignment_id)["status"] == ObligationStatus.assigned.value
    impersonate = service.complete(assignment_id=first.assignment_id, agent_identity="codex")
    assert impersonate.outcome == "forged_rejected"


def test_self_verification_and_conflicting_status_evidence():
    service = DurableCoordinationService()
    first = _assigned(service)
    service.acknowledge(assignment_id=first.assignment_id, agent_identity="cursor")
    service.record_execution_progress(
        assignment_id=first.assignment_id, agent_identity="cursor", evidence_kind="test_run"
    )
    service.complete(assignment_id=first.assignment_id, agent_identity="cursor")
    self_verify = service.independently_verify(
        assignment_id=first.assignment_id, examiner_identity="cursor"
    )
    assert self_verify.outcome == "forged_rejected"
    certified = service.independently_verify(
        assignment_id=first.assignment_id,
        examiner_identity="examiner",
        evidence={"review_result": "PASS"},
    )
    assert certified.status == ObligationStatus.independently_verified.value
    late_fail = service.fail(assignment_id=first.assignment_id, agent_identity="cursor")
    assert late_fail.outcome in {"stale_rejected", "idempotent"}
    assert service.get(first.assignment_id)["status"] == ObligationStatus.independently_verified.value
    assert service.founder_view()["independently_certified"][0]["assignment_id"] == first.assignment_id


def test_artifact_handoff_integrity_and_receipt():
    service = DurableCoordinationService()
    first = _assigned(service)
    payload = b"handoff-bytes"
    digest = artifact_sha256(payload)
    registered = service.register_artifact(
        producer_identity="cursor",
        consumer_identity="claude",
        sha256=digest,
        byte_length=len(payload),
        assignment_id=first.assignment_id,
        payload=payload,
        provenance={"origin": "unit-test"},
    )
    handoff_id = registered.detail["handoff_id"]
    delivered = service.transfer_artifact(
        handoff_id=handoff_id,
        actor="cursor",
        transport=TransferTransport.in_process_registry.value,
        payload=payload,
    )
    assert delivered.status == HandoffStatus.delivered.value
    received = service.receive_artifact(
        handoff_id=handoff_id, consumer_identity="claude", payload=payload
    )
    assert received.status == HandoffStatus.received.value


def test_malicious_artifact_and_impersonated_receipt(tmp_path: Path):
    service = DurableCoordinationService()
    payload = b"good"
    digest = artifact_sha256(payload)
    registered = service.register_artifact(
        producer_identity="cursor",
        consumer_identity="claude",
        sha256=digest,
        byte_length=len(payload),
        payload=b"evil",
    )
    assert registered.outcome == "malicious_artifact"
    ok = service.register_artifact(
        producer_identity="cursor",
        consumer_identity="claude",
        sha256=digest,
        byte_length=len(payload),
        payload=payload,
    )
    handoff_id = ok.detail["handoff_id"]
    traversal = service.transfer_artifact(
        handoff_id=handoff_id,
        actor="cursor",
        transport=TransferTransport.local_authorized_workspace.value,
        source_path="../etc/passwd",
        destination_path="out/a.bin",
        allowed_paths=["out/"],
        workspace_root=str(tmp_path),
    )
    assert traversal.outcome == "malicious_artifact"
    service.transfer_artifact(
        handoff_id=handoff_id,
        actor="cursor",
        transport=TransferTransport.in_process_registry.value,
        payload=payload,
    )
    stolen = service.receive_artifact(handoff_id=handoff_id, consumer_identity="codex", payload=payload)
    assert stolen.outcome == "forged_rejected"


def test_unavailable_transports_stay_blocked_and_do_not_invent_founder_transfer():
    service = DurableCoordinationService()
    first = _assigned(service)
    payload = b"x"
    digest = artifact_sha256(payload)
    registered = service.register_artifact(
        producer_identity="cursor",
        consumer_identity="claude",
        sha256=digest,
        byte_length=1,
        assignment_id=first.assignment_id,
        payload=payload,
    )
    handoff_id = registered.detail["handoff_id"]
    vendor = service.transfer_artifact(
        handoff_id=handoff_id,
        actor="cursor",
        transport=TransferTransport.vendor_private_workspace.value,
        founder_completed=True,
    )
    assert vendor.outcome == "privilege_rejected"
    github = service.transfer_artifact(
        handoff_id=handoff_id,
        actor="cursor",
        transport=TransferTransport.github_publication.value,
    )
    assert github.outcome == "blocked"
    assert github.detail["missing_capability"] == "codex_github_publication_authority"
    creds = service.transfer_artifact(
        handoff_id=handoff_id,
        actor="cursor",
        transport=TransferTransport.personal_credentials.value,
    )
    assert creds.detail["missing_capability"] == "personal_credential_use_forbidden"
    assert service.get_handoff(handoff_id)["status"] == HandoffStatus.blocked.value
    assert service.get(first.assignment_id)["status"] == ObligationStatus.blocked.value
    assert service.get(first.assignment_id)["missing_capability"] == "personal_credential_use_forbidden"


def test_local_authorized_copy(tmp_path: Path):
    service = DurableCoordinationService()
    payload = b"workspace-artifact"
    digest = artifact_sha256(payload)
    source = tmp_path / "in" / "a.bin"
    source.parent.mkdir()
    source.write_bytes(payload)
    registered = service.register_artifact(
        producer_identity="cursor",
        consumer_identity="claude",
        sha256=digest,
        byte_length=len(payload),
        payload=payload,
    )
    delivered = service.transfer_artifact(
        handoff_id=registered.detail["handoff_id"],
        actor="cursor",
        transport=TransferTransport.local_authorized_workspace.value,
        source_path="in/a.bin",
        destination_path="out/a.bin",
        allowed_paths=["out/"],
        workspace_root=str(tmp_path),
    )
    assert delivered.accepted is True
    assert (tmp_path / "out" / "a.bin").read_bytes() == payload


def test_unauthorized_privilege_escalation_is_rejected():
    service = DurableCoordinationService()
    first = _assigned(service)
    for grant in ("github_write", "founder_authority", "approval_bypass", "recall_activation"):
        result = service.escalate_privilege(actor="cursor", grant=grant, assignment_id=first.assignment_id)
        assert result.outcome == "privilege_rejected"
    agent_assign = service.assign(
        agent_identity="claude",
        workstream="co4b-co5",
        owner_id=OWNER,
        instruction_id="hijack",
        actor="cursor",
    )
    assert agent_assign.outcome == "privilege_rejected"


def test_truthful_empty_queue_and_founder_visibility():
    service = DurableCoordinationService()
    empty = service.claim_queue_empty(reporter="founder")
    assert empty.accepted is True
    first = _assigned(service, workstream="visible")
    view = service.founder_view()
    assert view["owners"][first.assignment_id] == "cursor"
    assert view["active"][0]["assignment_id"] == first.assignment_id
    assert view["queue_empty"] is False
