"""Durable agent obligation ledger and authorized artifact handoff (CO-4b / CO-5).

Coordination-maturity layer ABOVE existing `agent_work_assignments`, assignment events,
AuditLog, and Founder-ops snapshot. It does not mint a second task/job/approval/capability
system, does not implement Founder Authority or GitHub publication (Codex-owned), and is
never a first-cycle GO blocker.

Persistence reuses `AgentWorkAssignment.provenance["durable_coordination"]`,
`AgentWorkAssignment.output_refs`, `AgentWorkAssignmentEvent.evidence_recorded`, and
`AuditLog`. No Alembic migration and no new privilege tables.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.audit import record_audit
from app.models.agent_coordination import (
    AgentWorkAssignment,
    AgentWorkAssignmentEvent,
    AgentWorkAssignmentEventType,
)

BLOCKS_FIRST_CYCLE = False
FIRST_CYCLE_GO = False

KNOWN_AGENT_IDENTITIES = frozenset({"cursor", "claude", "codex", "founder", "mainai", "examiner"})
PRIVILEGED_SUPERSEDE = frozenset({"founder", "mainai"})
INDEPENDENT_EXAMINERS = frozenset({"examiner", "claude", "codex", "cursor"})
PLANNING_STALL_THRESHOLD = 3
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ISO_CLOCK = "%Y-%m-%dT%H:%M:%S.%fZ"

EXECUTION_EVIDENCE_KINDS = frozenset(
    {"code_change", "test_run", "artifact_hash", "adapter_heartbeat", "worktree_head"}
)
PLANNING_EVIDENCE_KINDS = frozenset({"plan", "replanning", "queue_empty_claim", "instruction"})


class ObligationStatus(str, Enum):
    assigned = "assigned"
    acknowledged = "acknowledged"
    executing = "executing"
    blocked = "blocked"
    completed = "completed"
    failed = "failed"
    superseded = "superseded"
    independently_verified = "independently_verified"


ACTIVE_OBLIGATION_STATUSES = frozenset(
    {
        ObligationStatus.assigned,
        ObligationStatus.acknowledged,
        ObligationStatus.executing,
        ObligationStatus.blocked,
    }
)
TERMINAL_OBLIGATION_STATUSES = frozenset(
    {
        ObligationStatus.completed,
        ObligationStatus.failed,
        ObligationStatus.superseded,
        ObligationStatus.independently_verified,
    }
)
FINISHED_OBLIGATION_STATUSES = frozenset(
    {
        ObligationStatus.completed,
        ObligationStatus.failed,
        ObligationStatus.superseded,
        ObligationStatus.independently_verified,
    }
)

# Instruction / existence never implies execution.
_STATUS_RANK = {
    ObligationStatus.assigned: 1,
    ObligationStatus.acknowledged: 2,
    ObligationStatus.executing: 3,
    ObligationStatus.blocked: 3,
    ObligationStatus.completed: 4,
    ObligationStatus.failed: 4,
    ObligationStatus.superseded: 5,
    ObligationStatus.independently_verified: 6,
}


class HandoffStatus(str, Enum):
    registered = "registered"
    transferring = "transferring"
    delivered = "delivered"
    received = "received"
    blocked = "blocked"
    expired = "expired"
    rejected = "rejected"


class TransferTransport(str, Enum):
    in_process_registry = "in_process_registry"
    local_authorized_workspace = "local_authorized_workspace"
    vendor_private_workspace = "vendor_private_workspace"
    github_publication = "github_publication"
    personal_credentials = "personal_credentials"


AVAILABLE_TRANSPORTS = frozenset(
    {
        TransferTransport.in_process_registry,
        TransferTransport.local_authorized_workspace,
    }
)

TRANSPORT_MISSING_CAPABILITY = {
    TransferTransport.vendor_private_workspace: "vendor_private_workspace_access",
    TransferTransport.github_publication: "codex_github_publication_authority",
    TransferTransport.personal_credentials: "personal_credential_use_forbidden",
}


@dataclass(frozen=True)
class OperationResult:
    accepted: bool
    outcome: str
    assignment_id: str | None
    status: str | None
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "outcome": self.outcome,
            "assignment_id": self.assignment_id,
            "status": self.status,
            "reason": self.reason,
            "detail": dict(self.detail),
            "first_cycle_go": FIRST_CYCLE_GO,
            "blocks_first_cycle": BLOCKS_FIRST_CYCLE,
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    stamp = value or _now()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).strftime(_ISO_CLOCK)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, _ISO_CLOCK).replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None


def canonical_work_key(*, owner_id: str, workstream: str, instruction_id: str) -> str:
    raw = f"{owner_id}\0{workstream}\0{instruction_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def artifact_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _reject(
    outcome: str,
    reason: str,
    *,
    assignment_id: str | None = None,
    status: str | None = None,
    **detail: Any,
) -> OperationResult:
    return OperationResult(
        accepted=False,
        outcome=outcome,
        assignment_id=assignment_id,
        status=status,
        reason=reason,
        detail=detail,
    )


class DurableCoordinationService:
    """In-process durable ledger. Persist via `persist_to_assignment` / `load_owner_ledger`."""

    def __init__(self) -> None:
        self._obligations: dict[str, dict[str, Any]] = {}
        self._by_canonical: dict[str, str] = {}
        self._handoffs: dict[str, dict[str, Any]] = {}
        self._seen_event_ids: set[str] = set()
        self._buffered: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ assign

    def assign(
        self,
        *,
        agent_identity: str,
        workstream: str,
        owner_id: str,
        instruction_id: str,
        dependencies: Iterable[str] = (),
        assignment_id: str | None = None,
        backing_assignment_id: str | None = None,
        event_id: str | None = None,
        occurred_at: str | None = None,
        actor: str | None = None,
    ) -> OperationResult:
        actor = actor or "founder"
        if actor not in PRIVILEGED_SUPERSEDE | {"founder"}:
            return _reject(
                "privilege_rejected",
                "only founder/mainai may create obligations",
                actor=actor,
            )
        if agent_identity not in KNOWN_AGENT_IDENTITIES:
            return _reject("invalid_agent", "unknown agent identity", agent_identity=agent_identity)
        key = canonical_work_key(owner_id=owner_id, workstream=workstream, instruction_id=instruction_id)
        existing_id = self._by_canonical.get(key)
        if existing_id:
            existing = self._obligations[existing_id]
            return OperationResult(
                accepted=True,
                outcome="idempotent",
                assignment_id=existing_id,
                status=existing["status"],
                reason="canonical work already recorded; old completion cannot mint a second assignment",
                detail={"canonical_work_key": key, "generation": existing["generation"]},
            )
        event_id = event_id or str(uuid.uuid4())
        if event_id in self._seen_event_ids:
            return _reject("replay_ignored", "duplicate assign event", event_id=event_id)
        new_id = assignment_id or str(uuid.uuid4())
        now = occurred_at or _iso()
        record = {
            "assignment_id": new_id,
            "agent_identity": agent_identity,
            "workstream": workstream,
            "owner_id": owner_id,
            "dependencies": [str(item) for item in dependencies],
            "status": ObligationStatus.assigned.value,
            "evidence": [],
            "superseded_by": None,
            "events": [],
            "canonical_work_key": key,
            "generation": 1,
            "stalled": False,
            "stall_reason": None,
            "blocked_reason": None,
            "missing_capability": None,
            "backing_assignment_id": backing_assignment_id,
            "created_at": now,
            "updated_at": now,
            "planning_streak": 0,
            "instruction_id": instruction_id,
            "instruction_ids": [instruction_id],
        }
        self._obligations[new_id] = record
        self._by_canonical[key] = new_id
        self._append_event(
            record,
            event_id=event_id,
            kind="assigned",
            actor=actor,
            occurred_at=now,
            detail={"instruction_id": instruction_id, "agent_identity": agent_identity},
        )
        self._drain_buffer()
        return OperationResult(
            accepted=True,
            outcome="applied",
            assignment_id=new_id,
            status=ObligationStatus.assigned.value,
            reason="obligation recorded; instruction is not execution",
            detail={"canonical_work_key": key, "generation": 1},
        )

    def supersede(
        self,
        *,
        assignment_id: str,
        actor: str,
        successor_agent: str,
        instruction_id: str,
        event_id: str | None = None,
        occurred_at: str | None = None,
    ) -> OperationResult:
        if actor not in PRIVILEGED_SUPERSEDE:
            return _reject(
                "privilege_rejected",
                "supersession is founder/mainai only",
                assignment_id=assignment_id,
                actor=actor,
            )
        current = self._obligations.get(assignment_id)
        if current is None:
            return _reject("unknown_assignment", "assignment does not exist", assignment_id=assignment_id)
        event_id = event_id or str(uuid.uuid4())
        replay = self._replay_guard(event_id, assignment_id, current["status"])
        if replay is not None:
            return replay
        successor = self.assign(
            agent_identity=successor_agent,
            workstream=current["workstream"],
            owner_id=current["owner_id"],
            instruction_id=instruction_id,
            dependencies=current["dependencies"],
            backing_assignment_id=current.get("backing_assignment_id"),
            event_id=str(uuid.uuid4()),
            occurred_at=occurred_at,
            actor=actor,
        )
        if not successor.accepted or successor.assignment_id is None:
            return successor
        successor_record = self._obligations[successor.assignment_id]
        successor_record["generation"] = int(current["generation"]) + 1
        successor_record["canonical_work_key"] = current["canonical_work_key"]
        self._by_canonical[current["canonical_work_key"]] = successor.assignment_id
        current["status"] = ObligationStatus.superseded.value
        current["superseded_by"] = successor.assignment_id
        current["updated_at"] = occurred_at or _iso()
        self._append_event(
            current,
            event_id=event_id,
            kind="superseded",
            actor=actor,
            occurred_at=current["updated_at"],
            detail={"successor": successor.assignment_id},
        )
        return OperationResult(
            accepted=True,
            outcome="applied",
            assignment_id=successor.assignment_id,
            status=ObligationStatus.assigned.value,
            reason="prior obligation superseded; chronology preserved",
            detail={"superseded": assignment_id, "generation": successor_record["generation"]},
        )

    # -------------------------------------------------------------- reports

    def acknowledge(
        self,
        *,
        assignment_id: str,
        agent_identity: str,
        event_id: str | None = None,
        occurred_at: str | None = None,
    ) -> OperationResult:
        return self.ingest_report(
            assignment_id=assignment_id,
            agent_identity=agent_identity,
            reported_status=ObligationStatus.acknowledged.value,
            event_id=event_id,
            occurred_at=occurred_at,
            evidence_kind="ack",
        )

    def record_execution_progress(
        self,
        *,
        assignment_id: str,
        agent_identity: str,
        evidence_kind: str,
        evidence: dict[str, Any] | None = None,
        event_id: str | None = None,
        occurred_at: str | None = None,
    ) -> OperationResult:
        if evidence_kind not in EXECUTION_EVIDENCE_KINDS:
            return _reject(
                "invalid_evidence",
                "execution is never inferred from an instruction or plan",
                assignment_id=assignment_id,
                evidence_kind=evidence_kind,
            )
        return self.ingest_report(
            assignment_id=assignment_id,
            agent_identity=agent_identity,
            reported_status=ObligationStatus.executing.value,
            event_id=event_id,
            occurred_at=occurred_at,
            evidence_kind=evidence_kind,
            evidence=evidence,
        )

    def record_planning(
        self,
        *,
        assignment_id: str,
        agent_identity: str,
        event_id: str | None = None,
        occurred_at: str | None = None,
        plan_ref: str | None = None,
    ) -> OperationResult:
        return self.ingest_report(
            assignment_id=assignment_id,
            agent_identity=agent_identity,
            reported_status=ObligationStatus.assigned.value,
            event_id=event_id,
            occurred_at=occurred_at,
            evidence_kind="plan",
            evidence={"plan_ref": plan_ref, "plan_generated": False},
        )

    def complete(
        self,
        *,
        assignment_id: str,
        agent_identity: str,
        event_id: str | None = None,
        occurred_at: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> OperationResult:
        return self.ingest_report(
            assignment_id=assignment_id,
            agent_identity=agent_identity,
            reported_status=ObligationStatus.completed.value,
            event_id=event_id,
            occurred_at=occurred_at,
            evidence_kind="completion",
            evidence=evidence,
        )

    def fail(
        self,
        *,
        assignment_id: str,
        agent_identity: str,
        event_id: str | None = None,
        occurred_at: str | None = None,
        reason: str = "failed",
    ) -> OperationResult:
        return self.ingest_report(
            assignment_id=assignment_id,
            agent_identity=agent_identity,
            reported_status=ObligationStatus.failed.value,
            event_id=event_id,
            occurred_at=occurred_at,
            evidence_kind="failure",
            evidence={"reason": reason},
        )

    def block(
        self,
        *,
        assignment_id: str,
        agent_identity: str,
        reason: str,
        missing_capability: str | None = None,
        event_id: str | None = None,
        occurred_at: str | None = None,
    ) -> OperationResult:
        return self.ingest_report(
            assignment_id=assignment_id,
            agent_identity=agent_identity,
            reported_status=ObligationStatus.blocked.value,
            event_id=event_id,
            occurred_at=occurred_at,
            evidence_kind="block",
            evidence={"reason": reason, "missing_capability": missing_capability},
        )

    def independently_verify(
        self,
        *,
        assignment_id: str,
        examiner_identity: str,
        event_id: str | None = None,
        occurred_at: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> OperationResult:
        return self.ingest_report(
            assignment_id=assignment_id,
            agent_identity=examiner_identity,
            reported_status=ObligationStatus.independently_verified.value,
            event_id=event_id,
            occurred_at=occurred_at,
            evidence_kind="independent_review",
            evidence=evidence,
        )

    def claim_queue_empty(
        self,
        *,
        reporter: str,
        event_id: str | None = None,
        occurred_at: str | None = None,
    ) -> OperationResult:
        event_id = event_id or str(uuid.uuid4())
        if event_id in self._seen_event_ids:
            return _reject("replay_ignored", "duplicate queue-empty claim", event_id=event_id)
        self._seen_event_ids.add(event_id)
        active = [row for row in self._obligations.values() if row["status"] in {s.value for s in ACTIVE_OBLIGATION_STATUSES}]
        if active:
            for row in active:
                self._append_event(
                    row,
                    event_id=str(uuid.uuid4()),
                    kind="stale_queue_empty",
                    actor=reporter,
                    occurred_at=occurred_at or _iso(),
                    detail={"ignored": True, "source_event_id": event_id},
                )
            return _reject(
                "stale_rejected",
                "queue-empty claim contradicts active obligations",
                active_assignment_ids=[row["assignment_id"] for row in active],
                reporter=reporter,
            )
        return OperationResult(
            accepted=True,
            outcome="applied",
            assignment_id=None,
            status=None,
            reason="queue is genuinely empty",
            detail={"reporter": reporter, "event_id": event_id},
        )

    def ingest_report(
        self,
        *,
        assignment_id: str,
        agent_identity: str,
        reported_status: str,
        event_id: str | None = None,
        occurred_at: str | None = None,
        evidence_kind: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> OperationResult:
        event_id = event_id or str(uuid.uuid4())
        if event_id in self._seen_event_ids:
            current = self._obligations.get(assignment_id)
            return OperationResult(
                accepted=True,
                outcome="replay_ignored",
                assignment_id=assignment_id,
                status=None if current is None else current["status"],
                reason="duplicate event_id; lost acknowledgment retry is idempotent",
                detail={"event_id": event_id},
            )
        record = self._obligations.get(assignment_id)
        if record is None:
            self._buffered.append(
                {
                    "assignment_id": assignment_id,
                    "agent_identity": agent_identity,
                    "reported_status": reported_status,
                    "event_id": event_id,
                    "occurred_at": occurred_at or _iso(),
                    "evidence_kind": evidence_kind,
                    "evidence": evidence or {},
                }
            )
            return OperationResult(
                accepted=False,
                outcome="buffered",
                assignment_id=assignment_id,
                status=None,
                reason="out-of-order report buffered until assignment exists; execution is not inferred",
                detail={"event_id": event_id},
            )

        replay = self._replay_guard(event_id, assignment_id, record["status"])
        if replay is not None:
            return replay

        generation = int((evidence or {}).get("generation") or record["generation"])
        if generation < int(record["generation"]) or record["status"] == ObligationStatus.superseded.value:
            self._seen_event_ids.add(event_id)
            self._append_event(
                record,
                event_id=event_id,
                kind="stale_rejected",
                actor=agent_identity,
                occurred_at=occurred_at or _iso(),
                detail={"reported_status": reported_status, "generation": generation},
            )
            return _reject(
                "stale_rejected",
                "stale generation or superseded obligation cannot change state or mint a second assignment",
                assignment_id=assignment_id,
                status=record["status"],
                generation=generation,
            )

        if reported_status == ObligationStatus.independently_verified.value:
            if agent_identity == record["agent_identity"]:
                return self._forged(
                    record,
                    event_id,
                    agent_identity,
                    "builder cannot independently verify its own work",
                    occurred_at,
                )
            if agent_identity not in INDEPENDENT_EXAMINERS:
                return self._forged(
                    record,
                    event_id,
                    agent_identity,
                    "examiner identity is not authorized for independent certification",
                    occurred_at,
                )
            if record["status"] not in {
                ObligationStatus.completed.value,
                ObligationStatus.independently_verified.value,
            }:
                return _reject(
                    "invalid_transition",
                    "independent verification requires a completed obligation",
                    assignment_id=assignment_id,
                    status=record["status"],
                )
        elif agent_identity != record["agent_identity"] and agent_identity not in PRIVILEGED_SUPERSEDE:
            return self._forged(
                record,
                event_id,
                agent_identity,
                "cross-agent impersonation rejected",
                occurred_at,
            )

        if evidence_kind in PLANNING_EVIDENCE_KINDS and reported_status == ObligationStatus.executing.value:
            return _reject(
                "invalid_evidence",
                "planning or instruction is not execution progress",
                assignment_id=assignment_id,
                status=record["status"],
            )

        target = ObligationStatus(reported_status)
        applied_at = occurred_at or _iso()
        last = _parse_iso(record.get("last_event_at"))
        incoming = _parse_iso(applied_at)
        if (
            last is not None
            and incoming is not None
            and incoming < last
            and record["status"] == ObligationStatus.independently_verified.value
        ):
            self._seen_event_ids.add(event_id)
            self._append_event(
                record,
                event_id=event_id,
                kind="stale_rejected",
                actor=agent_identity,
                occurred_at=applied_at,
                detail={"reported_status": reported_status, "reason": "late conflicting evidence"},
            )
            return _reject(
                "stale_rejected",
                "late conflicting evidence cannot undo independent certification",
                assignment_id=assignment_id,
                status=record["status"],
            )

        if record["status"] == ObligationStatus.independently_verified.value:
            self._seen_event_ids.add(event_id)
            self._append_event(
                record,
                event_id=event_id,
                kind="stale_rejected",
                actor=agent_identity,
                occurred_at=applied_at,
                detail={"reported_status": reported_status},
            )
            return OperationResult(
                accepted=True,
                outcome="idempotent",
                assignment_id=assignment_id,
                status=record["status"],
                reason="independently verified is sticky for this generation",
                detail={},
            )

        if evidence_kind == "plan":
            record["planning_streak"] = int(record.get("planning_streak") or 0) + 1
            plan_generated = False
            if record["planning_streak"] >= PLANNING_STALL_THRESHOLD:
                record["stalled"] = True
                record["stall_reason"] = "planning_without_execution"
                record["blocked_reason"] = "repeated planning without execution progress"
                target = ObligationStatus.blocked
                self._apply_status(record, target, applied_at)
            else:
                record["updated_at"] = applied_at
            self._seen_event_ids.add(event_id)
            self._append_event(
                record,
                event_id=event_id,
                kind="planning_only",
                actor=agent_identity,
                occurred_at=applied_at,
                detail={
                    "planning_streak": record["planning_streak"],
                    "stalled": record["stalled"],
                    "plan_generated": plan_generated,
                    **(evidence or {}),
                },
            )
            return OperationResult(
                accepted=True,
                outcome="stalled" if record["stalled"] else "applied",
                assignment_id=assignment_id,
                status=record["status"],
                reason=(
                    "repeated planning without execution; no new plan generated"
                    if record["stalled"]
                    else "planning recorded; execution is not inferred"
                ),
                detail={"plan_generated": False, "planning_streak": record["planning_streak"]},
            )

        if evidence_kind in EXECUTION_EVIDENCE_KINDS:
            record["planning_streak"] = 0
            record["stalled"] = False
            record["stall_reason"] = None
            if record["status"] in {
                ObligationStatus.assigned.value,
                ObligationStatus.acknowledged.value,
                ObligationStatus.blocked.value,
                ObligationStatus.executing.value,
            }:
                target = ObligationStatus.executing

        if target == ObligationStatus.acknowledged and record["status"] == ObligationStatus.assigned.value:
            pass
        elif target == ObligationStatus.executing and record["status"] in {
            ObligationStatus.assigned.value,
            ObligationStatus.acknowledged.value,
            ObligationStatus.blocked.value,
            ObligationStatus.executing.value,
        }:
            pass
        elif target in {ObligationStatus.completed, ObligationStatus.failed, ObligationStatus.blocked}:
            if record["status"] == ObligationStatus.assigned.value and target == ObligationStatus.completed:
                # Completion requires that the work was at least acknowledged or executing,
                # unless explicit completion evidence is supplied after execution progress.
                if evidence_kind not in {"completion"} or not (evidence or {}).get("execution_evidence"):
                    return _reject(
                        "invalid_transition",
                        "completion is not inferred from assignment existence",
                        assignment_id=assignment_id,
                        status=record["status"],
                    )
        elif target == ObligationStatus.independently_verified:
            pass
        elif target.value == record["status"]:
            self._seen_event_ids.add(event_id)
            return OperationResult(
                accepted=True,
                outcome="idempotent",
                assignment_id=assignment_id,
                status=record["status"],
                reason="status already recorded",
                detail={},
            )
        else:
            current = ObligationStatus(record["status"])
            if _STATUS_RANK[target] < _STATUS_RANK[current] and current in TERMINAL_OBLIGATION_STATUSES:
                return _reject(
                    "stale_rejected",
                    "terminal status cannot regress",
                    assignment_id=assignment_id,
                    status=record["status"],
                )

        if target == ObligationStatus.blocked:
            record["blocked_reason"] = (evidence or {}).get("reason")
            record["missing_capability"] = (evidence or {}).get("missing_capability")

        self._apply_status(record, target, applied_at)
        if evidence:
            record["evidence"].append({"kind": evidence_kind, "at": applied_at, **evidence})
        self._seen_event_ids.add(event_id)
        self._append_event(
            record,
            event_id=event_id,
            kind=target.value,
            actor=agent_identity,
            occurred_at=applied_at,
            detail={"evidence_kind": evidence_kind, **(evidence or {})},
        )
        return OperationResult(
            accepted=True,
            outcome="applied",
            assignment_id=assignment_id,
            status=record["status"],
            reason="report reconciled",
            detail={"evidence_kind": evidence_kind},
        )

    # -------------------------------------------------------------- artifacts

    def register_artifact(
        self,
        *,
        producer_identity: str,
        consumer_identity: str,
        sha256: str,
        byte_length: int,
        media_type: str = "application/octet-stream",
        provenance: dict[str, Any] | None = None,
        expires_at: str | None = None,
        assignment_id: str | None = None,
        payload: bytes | None = None,
        event_id: str | None = None,
    ) -> OperationResult:
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            return _reject("malicious_artifact", "artifact hash must be 64 lowercase hex", sha256=sha256)
        if byte_length < 0:
            return _reject("malicious_artifact", "negative byte length is invalid")
        if payload is not None:
            digest = artifact_sha256(payload)
            if digest != sha256 or len(payload) != byte_length:
                return _reject(
                    "malicious_artifact",
                    "payload hash or length does not match the registered digest",
                    expected=sha256,
                    actual=digest,
                )
        if producer_identity not in KNOWN_AGENT_IDENTITIES or consumer_identity not in KNOWN_AGENT_IDENTITIES:
            return _reject("invalid_agent", "producer and consumer must be known identities")
        handoff_id = str(uuid.uuid4())
        now = _iso()
        record = {
            "handoff_id": handoff_id,
            "sha256": sha256,
            "byte_length": byte_length,
            "media_type": media_type,
            "provenance": dict(provenance or {}),
            "producer_identity": producer_identity,
            "consumer_identity": consumer_identity,
            "expires_at": expires_at,
            "status": HandoffStatus.registered.value,
            "integrity_ok": payload is None or artifact_sha256(payload) == sha256,
            "delivery_evidence": [],
            "receipt_evidence": [],
            "missing_capability": None,
            "assignment_id": assignment_id,
            "created_at": now,
            "updated_at": now,
        }
        if expires_at and (_parse_iso(expires_at) or _now()) <= _now():
            record["status"] = HandoffStatus.expired.value
        self._handoffs[handoff_id] = record
        event_id = event_id or str(uuid.uuid4())
        self._seen_event_ids.add(event_id)
        if assignment_id and assignment_id in self._obligations:
            self._append_event(
                self._obligations[assignment_id],
                event_id=event_id,
                kind="artifact_registered",
                actor=producer_identity,
                occurred_at=now,
                detail={"handoff_id": handoff_id, "sha256": sha256},
            )
        return OperationResult(
            accepted=True,
            outcome="applied",
            assignment_id=assignment_id,
            status=record["status"],
            reason="artifact registered with exact hash and provenance",
            detail={"handoff_id": handoff_id, "sha256": sha256},
        )

    def transfer_artifact(
        self,
        *,
        handoff_id: str,
        actor: str,
        transport: str,
        source_path: str | None = None,
        destination_path: str | None = None,
        allowed_paths: Iterable[str] = (),
        workspace_root: str | None = None,
        payload: bytes | None = None,
        founder_completed: bool = False,
        event_id: str | None = None,
    ) -> OperationResult:
        handoff = self._handoffs.get(handoff_id)
        if handoff is None:
            return _reject("unknown_handoff", "handoff does not exist", handoff_id=handoff_id)
        if actor != handoff["producer_identity"] and actor not in PRIVILEGED_SUPERSEDE:
            return _reject(
                "privilege_rejected",
                "only the producer or founder/mainai may initiate transfer",
                handoff_id=handoff_id,
                actor=actor,
            )
        if founder_completed:
            return _reject(
                "privilege_rejected",
                "Founder completion of a transfer cannot be asserted by an agent report",
                handoff_id=handoff_id,
            )
        try:
            kind = TransferTransport(transport)
        except ValueError:
            return self._block_handoff(
                handoff,
                missing_capability="unknown_transport",
                reason=f"transport '{transport}' is not a registered adapter",
            )
        if kind not in AVAILABLE_TRANSPORTS:
            missing = TRANSPORT_MISSING_CAPABILITY.get(kind, f"transport_{kind.value}_unavailable")
            return self._block_handoff(
                handoff,
                missing_capability=missing,
                reason=f"automatic transport '{kind.value}' is not available",
            )
        if handoff["status"] == HandoffStatus.expired.value:
            return _reject("expired", "handoff expired before transfer", handoff_id=handoff_id, status=handoff["status"])

        if kind == TransferTransport.in_process_registry:
            if payload is not None and artifact_sha256(payload) != handoff["sha256"]:
                return self._reject_handoff(handoff, "payload digest does not match registered sha256")
            handoff["status"] = HandoffStatus.delivered.value
            handoff["delivery_evidence"].append(
                {"transport": kind.value, "at": _iso(), "actor": actor, "integrity_ok": True}
            )
            handoff["updated_at"] = _iso()
            return OperationResult(
                accepted=True,
                outcome="applied",
                assignment_id=handoff.get("assignment_id"),
                status=handoff["status"],
                reason="delivered via in-process registry (hash + provenance only; no vendor workspace)",
                detail={"handoff_id": handoff_id, "transport": kind.value},
            )

        root = Path(workspace_root or os.getcwd()).resolve()
        if not source_path or not destination_path:
            return self._block_handoff(
                handoff,
                missing_capability="local_authorized_workspace_paths",
                reason="local transfer requires explicit source and destination paths",
            )
        try:
            source = _safe_workspace_path(root, source_path)
            dest = _safe_workspace_path(root, destination_path)
        except ValueError as exc:
            return self._reject_handoff(handoff, str(exc))
        if not _path_allowed(destination_path, allowed_paths):
            return self._block_handoff(
                handoff,
                missing_capability="destination_outside_allowed_paths",
                reason="destination is outside the consumer assignment allowed_paths",
            )
        if not source.is_file():
            return self._block_handoff(
                handoff,
                missing_capability="source_artifact_missing",
                reason="source file is not present in this workspace",
            )
        data = source.read_bytes()
        if artifact_sha256(data) != handoff["sha256"] or len(data) != handoff["byte_length"]:
            return self._reject_handoff(handoff, "source file failed integrity check")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        copied = dest.read_bytes()
        if artifact_sha256(copied) != handoff["sha256"]:
            dest.unlink(missing_ok=True)
            return self._reject_handoff(handoff, "copied destination failed integrity check")
        handoff["status"] = HandoffStatus.delivered.value
        handoff["delivery_evidence"].append(
            {
                "transport": kind.value,
                "at": _iso(),
                "actor": actor,
                "source": str(source),
                "destination": str(dest),
                "integrity_ok": True,
            }
        )
        handoff["updated_at"] = _iso()
        event_id = event_id or str(uuid.uuid4())
        self._seen_event_ids.add(event_id)
        return OperationResult(
            accepted=True,
            outcome="applied",
            assignment_id=handoff.get("assignment_id"),
            status=handoff["status"],
            reason="delivered via local authorized workspace copy",
            detail={"handoff_id": handoff_id, "destination": str(dest)},
        )

    def receive_artifact(
        self,
        *,
        handoff_id: str,
        consumer_identity: str,
        payload: bytes | None = None,
        event_id: str | None = None,
    ) -> OperationResult:
        handoff = self._handoffs.get(handoff_id)
        if handoff is None:
            return _reject("unknown_handoff", "handoff does not exist", handoff_id=handoff_id)
        if consumer_identity != handoff["consumer_identity"]:
            return _reject(
                "forged_rejected",
                "receipt must come from the registered consumer",
                handoff_id=handoff_id,
                actor=consumer_identity,
            )
        if handoff["status"] != HandoffStatus.delivered.value:
            return _reject(
                "invalid_transition",
                "receipt requires a delivered handoff; Founder transfer is not inferred",
                handoff_id=handoff_id,
                status=handoff["status"],
            )
        if payload is not None and artifact_sha256(payload) != handoff["sha256"]:
            return self._reject_handoff(handoff, "receipt payload failed integrity check")
        handoff["status"] = HandoffStatus.received.value
        handoff["receipt_evidence"].append({"at": _iso(), "consumer": consumer_identity, "integrity_ok": True})
        handoff["updated_at"] = _iso()
        event_id = event_id or str(uuid.uuid4())
        self._seen_event_ids.add(event_id)
        return OperationResult(
            accepted=True,
            outcome="applied",
            assignment_id=handoff.get("assignment_id"),
            status=handoff["status"],
            reason="consumer receipt recorded",
            detail={"handoff_id": handoff_id},
        )

    def escalate_privilege(self, *, actor: str, grant: str, assignment_id: str | None = None) -> OperationResult:
        return _reject(
            "privilege_rejected",
            "this coordination layer cannot grant Founder Authority, approvals, or GitHub publication",
            actor=actor,
            grant=grant,
            assignment_id=assignment_id,
            owner="codex_founder_authority",
        )

    # -------------------------------------------------------------- views

    def get(self, assignment_id: str) -> dict[str, Any] | None:
        record = self._obligations.get(assignment_id)
        return None if record is None else _public_obligation(record)

    def get_handoff(self, handoff_id: str) -> dict[str, Any] | None:
        record = self._handoffs.get(handoff_id)
        return None if record is None else dict(record)

    def founder_view(self) -> dict[str, Any]:
        obligations = [_public_obligation(row) for row in self._obligations.values()]
        handoffs = [dict(row) for row in self._handoffs.values()]
        active = [row for row in obligations if row["status"] in {s.value for s in ACTIVE_OBLIGATION_STATUSES}]
        finished = [row for row in obligations if row["status"] in {s.value for s in FINISHED_OBLIGATION_STATUSES}]
        certified = [row for row in obligations if row["status"] == ObligationStatus.independently_verified.value]
        blocked = [row for row in obligations if row["status"] == ObligationStatus.blocked.value]
        stalled = [row for row in obligations if row.get("stalled")]
        return {
            "capability": "CO-4b/CO-5",
            "first_cycle_go": FIRST_CYCLE_GO,
            "blocks_first_cycle": BLOCKS_FIRST_CYCLE,
            "fabricated_progress": False,
            "invented_vendor_access": False,
            "obligations": sorted(obligations, key=lambda row: row["created_at"]),
            "active": active,
            "finished": finished,
            "independently_certified": certified,
            "blocked": blocked,
            "stalled": stalled,
            "handoffs": handoffs,
            "owners": {row["assignment_id"]: row["agent_identity"] for row in obligations},
            "queue_empty": len(active) == 0,
            "needed_work": self.needed_work(),
        }

    def needed_work(self) -> dict[str, Any]:
        """Which agent needs what. Founder is not required to forward routine reports."""
        items: list[dict[str, Any]] = []
        busy: set[str] = set()
        for record in self._obligations.values():
            agent = record["agent_identity"]
            if record["status"] in {s.value for s in ACTIVE_OBLIGATION_STATUSES}:
                busy.add(agent)
            if record.get("stalled"):
                items.append(
                    {
                        "agent_identity": agent,
                        "assignment_id": record["assignment_id"],
                        "need": "execution_progress_not_another_plan",
                        "status": record["status"],
                    }
                )
            elif record["status"] == ObligationStatus.assigned.value:
                items.append(
                    {
                        "agent_identity": agent,
                        "assignment_id": record["assignment_id"],
                        "need": "acknowledge",
                        "status": record["status"],
                    }
                )
            elif record["status"] == ObligationStatus.acknowledged.value:
                items.append(
                    {
                        "agent_identity": agent,
                        "assignment_id": record["assignment_id"],
                        "need": "execution_progress",
                        "status": record["status"],
                    }
                )
            elif record["status"] == ObligationStatus.executing.value:
                items.append(
                    {
                        "agent_identity": agent,
                        "assignment_id": record["assignment_id"],
                        "need": "continue_or_complete",
                        "status": record["status"],
                    }
                )
            elif record["status"] == ObligationStatus.blocked.value:
                items.append(
                    {
                        "agent_identity": agent,
                        "assignment_id": record["assignment_id"],
                        "need": "unblock",
                        "status": record["status"],
                        "missing_capability": record.get("missing_capability"),
                    }
                )
        idle = sorted(identity for identity in ("cursor", "claude", "codex") if identity not in busy)
        return {
            "items": items,
            "idle_agents": idle,
            "founder_must_forward_reports": False,
        }

    def restore(self, payload: dict[str, Any]) -> None:
        obligation = payload.get("obligation")
        if isinstance(obligation, dict) and obligation.get("assignment_id"):
            self._obligations[obligation["assignment_id"]] = obligation
            self._by_canonical[obligation["canonical_work_key"]] = obligation["assignment_id"]
            for event in obligation.get("events") or []:
                if event.get("event_id"):
                    self._seen_event_ids.add(event["event_id"])
        for handoff in payload.get("handoffs") or []:
            if isinstance(handoff, dict) and handoff.get("handoff_id"):
                self._handoffs[handoff["handoff_id"]] = handoff
        for event_id in payload.get("seen_event_ids") or []:
            self._seen_event_ids.add(str(event_id))

    def dump_for(self, assignment_id: str) -> dict[str, Any]:
        record = self._obligations.get(assignment_id)
        related = [
            dict(row)
            for row in self._handoffs.values()
            if row.get("assignment_id") == assignment_id
        ]
        return {
            "obligation": None if record is None else json.loads(json.dumps(record)),
            "handoffs": related,
            "seen_event_ids": sorted(self._seen_event_ids),
        }

    # -------------------------------------------------------------- internals

    def _replay_guard(self, event_id: str, assignment_id: str, status: str) -> OperationResult | None:
        if event_id in self._seen_event_ids:
            return OperationResult(
                accepted=True,
                outcome="replay_ignored",
                assignment_id=assignment_id,
                status=status,
                reason="duplicate event_id ignored",
                detail={"event_id": event_id},
            )
        return None

    def _forged(
        self,
        record: dict[str, Any],
        event_id: str,
        actor: str,
        reason: str,
        occurred_at: str | None,
    ) -> OperationResult:
        self._seen_event_ids.add(event_id)
        self._append_event(
            record,
            event_id=event_id,
            kind="forged_rejected",
            actor=actor,
            occurred_at=occurred_at or _iso(),
            detail={"reason": reason},
        )
        return _reject(
            "forged_rejected",
            reason,
            assignment_id=record["assignment_id"],
            status=record["status"],
            actor=actor,
        )

    def _apply_status(self, record: dict[str, Any], target: ObligationStatus, applied_at: str) -> None:
        record["status"] = target.value
        record["updated_at"] = applied_at
        record["last_event_at"] = applied_at

    def _append_event(
        self,
        record: dict[str, Any],
        *,
        event_id: str,
        kind: str,
        actor: str,
        occurred_at: str,
        detail: dict[str, Any],
    ) -> None:
        record["events"].append(
            {
                "event_id": event_id,
                "kind": kind,
                "actor": actor,
                "occurred_at": occurred_at,
                "detail": detail,
            }
        )
        record["updated_at"] = occurred_at
        record["last_event_at"] = occurred_at
        self._seen_event_ids.add(event_id)

    def _drain_buffer(self) -> None:
        pending = list(self._buffered)
        self._buffered = []
        for item in pending:
            if item["assignment_id"] in self._obligations:
                self.ingest_report(**item)
            else:
                self._buffered.append(item)

    def _block_handoff(self, handoff: dict[str, Any], *, missing_capability: str, reason: str) -> OperationResult:
        handoff["status"] = HandoffStatus.blocked.value
        handoff["missing_capability"] = missing_capability
        handoff["updated_at"] = _iso()
        assignment_id = handoff.get("assignment_id")
        if assignment_id and assignment_id in self._obligations:
            self.block(
                assignment_id=assignment_id,
                agent_identity=handoff["producer_identity"],
                reason=reason,
                missing_capability=missing_capability,
            )
        return OperationResult(
            accepted=False,
            outcome="blocked",
            assignment_id=assignment_id,
            status=HandoffStatus.blocked.value,
            reason=reason,
            detail={"handoff_id": handoff["handoff_id"], "missing_capability": missing_capability},
        )

    def _reject_handoff(self, handoff: dict[str, Any], reason: str) -> OperationResult:
        handoff["status"] = HandoffStatus.rejected.value
        handoff["integrity_ok"] = False
        handoff["updated_at"] = _iso()
        return _reject(
            "malicious_artifact",
            reason,
            assignment_id=handoff.get("assignment_id"),
            status=handoff["status"],
            handoff_id=handoff["handoff_id"],
        )


def _public_obligation(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "assignment_id": record["assignment_id"],
        "agent_identity": record["agent_identity"],
        "workstream": record["workstream"],
        "owner_id": record["owner_id"],
        "dependencies": list(record.get("dependencies") or []),
        "status": record["status"],
        "evidence": list(record.get("evidence") or []),
        "superseded_by": record.get("superseded_by"),
        "events": list(record.get("events") or []),
        "canonical_work_key": record["canonical_work_key"],
        "generation": record.get("generation", 1),
        "stalled": bool(record.get("stalled")),
        "stall_reason": record.get("stall_reason"),
        "blocked_reason": record.get("blocked_reason"),
        "missing_capability": record.get("missing_capability"),
        "backing_assignment_id": record.get("backing_assignment_id"),
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "owner_label": record["agent_identity"],
        "genuinely_active": record["status"] in {s.value for s in ACTIVE_OBLIGATION_STATUSES} and not record.get("stalled"),
        "finished": record["status"] in {s.value for s in FINISHED_OBLIGATION_STATUSES},
        "independently_certified": record["status"] == ObligationStatus.independently_verified.value,
        "blocked": record["status"] == ObligationStatus.blocked.value,
    }


def _safe_workspace_path(root: Path, raw: str) -> Path:
    if not raw or raw.startswith("~") or Path(raw).is_absolute():
        raise ValueError("path must be a relative workspace path")
    if ".." in Path(raw).parts:
        raise ValueError("path traversal is not authorized")
    resolved = (root / raw).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes the authorized workspace") from exc
    return resolved


def _path_allowed(destination: str, allowed_paths: Iterable[str]) -> bool:
    allowed = [item.rstrip("*") for item in allowed_paths]
    if not allowed:
        return False
    normalized = destination.lstrip("./")
    return any(normalized == prefix.rstrip("/") or normalized.startswith(prefix.rstrip("/") + "/") or prefix in ("**", "*") for prefix in allowed)


def persist_to_assignment(
    db: Session,
    *,
    assignment: AgentWorkAssignment,
    service: DurableCoordinationService,
    obligation_id: str,
    actor: str = "mainai",
) -> None:
    """Write the ledger slice onto existing assignment JSON + evidence event + AuditLog."""
    payload = service.dump_for(obligation_id)
    provenance = dict(assignment.provenance or {})
    provenance["durable_coordination"] = payload
    assignment.provenance = provenance
    flag_modified(assignment, "provenance")
    refs = list(assignment.output_refs or [])
    for handoff in payload.get("handoffs") or []:
        entry = {
            "kind": "durable_handoff",
            "handoff_id": handoff.get("handoff_id"),
            "sha256": handoff.get("sha256"),
            "status": handoff.get("status"),
        }
        if entry not in refs:
            refs.append(entry)
    assignment.output_refs = refs
    flag_modified(assignment, "output_refs")
    db.add(
        AgentWorkAssignmentEvent(
            assignment_id=assignment.id,
            owner_id=assignment.owner_id,
            event_type=AgentWorkAssignmentEventType.evidence_recorded,
            detail={"durable_coordination": True, "obligation_id": obligation_id, "actor": actor},
        )
    )
    record_audit(
        db,
        user_id=assignment.owner_id,
        action="durable_coordination.persist",
        entity_type="agent_work_assignment",
        entity_id=str(assignment.id),
        detail=f"obligation={obligation_id}",
        commit=False,
    )
    db.flush()


def load_owner_ledger(db: Session, *, owner_id: uuid.UUID) -> DurableCoordinationService:
    service = DurableCoordinationService()
    rows = list(
        db.execute(select(AgentWorkAssignment).where(AgentWorkAssignment.owner_id == owner_id)).scalars()
    )
    for row in rows:
        payload = (row.provenance or {}).get("durable_coordination")
        if isinstance(payload, dict):
            service.restore(payload)
    return service


def founder_coordination_snapshot(db: Session, *, owner_id: uuid.UUID) -> dict[str, Any]:
    try:
        return load_owner_ledger(db, owner_id=owner_id).founder_view()
    except Exception as exc:  # noqa: BLE001 — Founder UI must not hide missing coordination
        return {
            "capability": "CO-4b/CO-5",
            "available": False,
            "first_cycle_go": FIRST_CYCLE_GO,
            "blocks_first_cycle": BLOCKS_FIRST_CYCLE,
            "error": type(exc).__name__,
            "obligations": [],
            "active": [],
            "finished": [],
            "independently_certified": [],
            "blocked": [],
            "stalled": [],
            "handoffs": [],
            "owners": {},
            "queue_empty": True,
            "needed_work": {"items": [], "idle_agents": ["cursor", "claude", "codex"], "founder_must_forward_reports": False},
        }
