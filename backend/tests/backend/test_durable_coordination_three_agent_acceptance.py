"""Working three-agent coordination demonstration (CO-4b/CO-5 acceptance).

Cursor, Claude and Codex are simulated in-process. MainAI reads needed_work from the
ledger instead of asking the Founder to forward reports. Not a first-cycle GO.
"""

from __future__ import annotations

from pathlib import Path

from app.agent_coordination.durable import (
    DurableCoordinationService,
    HandoffStatus,
    ObligationStatus,
    TransferTransport,
    artifact_sha256,
)

OWNER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _assign(service, *, agent, instruction, workstream="acceptance"):
    result = service.assign(
        agent_identity=agent,
        workstream=workstream,
        owner_id=OWNER,
        instruction_id=instruction,
        actor="founder",
    )
    assert result.accepted, result
    return result.assignment_id


def test_three_agent_coordination_demonstration(tmp_path: Path):
    service = DurableCoordinationService()

    stale_empty = service.claim_queue_empty(reporter="cursor", event_id="hist-empty")
    assert stale_empty.accepted is True

    delayed_ack = service.acknowledge(assignment_id="cursor-live", agent_identity="cursor", event_id="ack-early")
    assert delayed_ack.outcome == "buffered"

    cursor_id = service.assign(
        agent_identity="cursor",
        workstream="proxy-identity",
        owner_id=OWNER,
        instruction_id="cursor-live-work",
        assignment_id="cursor-live",
        actor="founder",
    ).assignment_id
    claude_id = _assign(service, agent="claude", instruction="claude-exam")
    codex_id = _assign(service, agent="codex", instruction="codex-grants")

    assert service.get("cursor-live")["status"] == ObligationStatus.acknowledged.value

    dup = service.assign(
        agent_identity="cursor",
        workstream="proxy-identity",
        owner_id=OWNER,
        instruction_id="cursor-live-work",
        actor="founder",
    )
    assert dup.outcome == "idempotent"
    assert dup.assignment_id == cursor_id

    service.acknowledge(assignment_id=claude_id, agent_identity="claude", event_id="ack-claude")
    service.acknowledge(assignment_id=claude_id, agent_identity="claude", event_id="ack-claude")
    service.acknowledge(assignment_id=codex_id, agent_identity="codex")

    service.record_execution_progress(
        assignment_id=codex_id, agent_identity="codex", evidence_kind="test_run", evidence={"passed": 4}
    )
    service.complete(assignment_id=codex_id, agent_identity="codex")
    verified = service.independently_verify(
        assignment_id=codex_id, examiner_identity="examiner", evidence={"review_result": "PASS"}
    )
    assert verified.status == ObligationStatus.independently_verified.value

    later_empty = service.claim_queue_empty(reporter="cursor", event_id="stale-empty-after")
    assert later_empty.outcome == "stale_rejected"
    assert service.get(cursor_id)["status"] != ObligationStatus.completed.value
    assert service.founder_view()["queue_empty"] is False

    for i in range(3):
        plan = service.record_planning(
            assignment_id=claude_id, agent_identity="claude", event_id=f"plan-{i}", plan_ref=f"plan-{i}"
        )
    assert plan.outcome == "stalled"
    assert plan.detail["plan_generated"] is False
    assert service.get(claude_id)["stalled"] is True

    payload = b"authorized-local-artifact"
    digest = artifact_sha256(payload)
    source = tmp_path / "in" / "handoff.bin"
    source.parent.mkdir()
    source.write_bytes(payload)
    local = service.register_artifact(
        producer_identity="cursor",
        consumer_identity="claude",
        sha256=digest,
        byte_length=len(payload),
        assignment_id=cursor_id,
        payload=payload,
        provenance={"demo": "three-agent"},
    )
    delivered = service.transfer_artifact(
        handoff_id=local.detail["handoff_id"],
        actor="cursor",
        transport=TransferTransport.local_authorized_workspace.value,
        source_path="in/handoff.bin",
        destination_path="out/handoff.bin",
        allowed_paths=["out/"],
        workspace_root=str(tmp_path),
    )
    assert delivered.accepted is True
    assert (tmp_path / "out" / "handoff.bin").read_bytes() == payload
    received = service.receive_artifact(
        handoff_id=local.detail["handoff_id"], consumer_identity="claude", payload=payload
    )
    assert received.status == HandoffStatus.received.value

    blocked = service.register_artifact(
        producer_identity="cursor",
        consumer_identity="codex",
        sha256=digest,
        byte_length=len(payload),
        assignment_id=cursor_id,
        payload=payload,
    )
    vendor = service.transfer_artifact(
        handoff_id=blocked.detail["handoff_id"],
        actor="cursor",
        transport=TransferTransport.vendor_private_workspace.value,
    )
    assert vendor.outcome == "blocked"
    assert vendor.detail["missing_capability"] == "vendor_private_workspace_access"
    github = service.transfer_artifact(
        handoff_id=blocked.detail["handoff_id"],
        actor="cursor",
        transport=TransferTransport.github_publication.value,
    )
    assert github.detail["missing_capability"] == "codex_github_publication_authority"

    view = service.founder_view()
    assert view["first_cycle_go"] is False
    assert view["blocks_first_cycle"] is False
    owners = view["owners"]
    assert owners[cursor_id] == "cursor"
    assert owners[claude_id] == "claude"
    assert owners[codex_id] == "codex"
    assert {row["assignment_id"] for row in view["active"]} == {cursor_id, claude_id}
    assert any(row["assignment_id"] == codex_id for row in view["independently_certified"])
    assert any(row["status"] == HandoffStatus.received.value for row in view["handoffs"])
    assert any(row["status"] == HandoffStatus.blocked.value for row in view["handoffs"])

    needed = view["needed_work"]
    assert needed["founder_must_forward_reports"] is False
    needs = {item["agent_identity"]: item["need"] for item in needed["items"]}
    assert needs["cursor"] in {"acknowledge", "execution_progress", "continue_or_complete", "unblock"}
    assert needs["claude"] == "execution_progress_not_another_plan"
    assert "codex" not in needs
    assert "codex" in needed["idle_agents"]
    assert len(view["obligations"]) == 3
