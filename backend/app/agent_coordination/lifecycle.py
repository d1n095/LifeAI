"""Production agent-lifecycle ingestion into the existing durable coordination ledger.

Connects assignment/status/execution events, GitHub read-only observations and review
receipts to `DurableCoordinationService`. This is not a second coordinator, not Founder
Authority, and not GitHub publication.

The authenticated boundary is `ingest_lifecycle_event`. The worker tick
`reconcile_pending_owners` scans persisted application events so the Founder does not
manually refresh every change. GitHub-green CI is never independent certification.
A prepared instruction is never a delivered Cursor/Claude/Codex message.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_coordination.adapter_config import SUPPORTED_LOCAL_CLI_PROVIDERS, adapter_availability
from app.agent_coordination.durable import (
    FIRST_CYCLE_GO,
    DurableCoordinationService,
    ObligationStatus,
    load_owner_ledger,
    persist_to_assignment,
)
from app.agent_coordination.github_intake import GitHubDevelopmentIntake
from app.models.agent_coordination import (
    AgentWorkAssignment,
    AgentWorkAssignmentEvent,
    AgentWorkAssignmentEventType,
    CoordinationAgent,
    WorkAssignmentRole,
    WorkAssignmentStatus,
)

PRIVILEGED_ACTORS = frozenset({"founder", "mainai", "worker"})
KNOWN_AGENTS = frozenset({"cursor", "claude", "codex", "examiner", "founder", "mainai"})
MISSING_MESSAGING = "agent_messaging_adapter"
MISSING_PUBLICATION = "codex_github_publication_authority"
LIFECYCLE_KINDS = frozenset(
    {
        "assignment_observed",
        "status_changed",
        "execution_progress",
        "completion_claim",
        "independent_review_receipt",
        "github_observation",
        "delivery_claim",
        "queue_empty_claim",
    }
)


def empty_lifecycle_state() -> dict[str, Any]:
    return {
        "watermark": None,
        "processed_event_ids": [],
        "decisions": [],
        "builder_eligible": [],
        "delivery_claimed": False,
        "first_cycle_go": FIRST_CYCLE_GO,
        "adapters": delivery_adapter_inventory(),
    }


def delivery_adapter_inventory() -> list[dict[str, Any]]:
    """Honest inventory. Local CLI can execute only when Founder-enabled; messaging is BLOCKED."""
    rows: list[dict[str, Any]] = [
        {
            "channel": "in_process_registry",
            "kind": "ledger",
            "usable": True,
            "delivery_claimed": False,
            "reason": "stores prepared instructions; does not message an external agent",
        },
        {
            "channel": "local_authorized_workspace",
            "kind": "transfer",
            "usable": True,
            "delivery_claimed": False,
            "reason": "local copy inside allowed_paths only",
        },
    ]
    for key in SUPPORTED_LOCAL_CLI_PROVIDERS:
        avail = adapter_availability(key)
        usable = bool(avail.enabled and avail.executable_found and avail.credentials_state == "configured")
        rows.append(
            {
                "channel": key,
                "kind": "local_cli_execution",
                "usable": usable,
                "supported": avail.supported,
                "enabled": avail.enabled,
                "executable_found": avail.executable_found,
                "credentials_state": avail.credentials_state,
                "delivery_claimed": False,
                "missing_capability": None if usable else "founder_adapter_enablement",
                "reason": avail.reason + "; local CLI is not vendor messaging",
            }
        )
    rows.extend(
        [
            {
                "channel": "cursor_vendor_messaging",
                "kind": "messaging",
                "usable": False,
                "delivery_claimed": False,
                "missing_capability": MISSING_MESSAGING,
                "reason": "no authorized Cursor messaging adapter",
            },
            {
                "channel": "claude_vendor_messaging",
                "kind": "messaging",
                "usable": False,
                "delivery_claimed": False,
                "missing_capability": MISSING_MESSAGING,
                "reason": "no authorized Claude messaging adapter",
            },
            {
                "channel": "codex_vendor_messaging",
                "kind": "messaging",
                "usable": False,
                "delivery_claimed": False,
                "missing_capability": MISSING_MESSAGING,
                "reason": "no authorized Codex messaging adapter",
            },
            {
                "channel": "github_publication",
                "kind": "write",
                "usable": False,
                "delivery_claimed": False,
                "missing_capability": MISSING_PUBLICATION,
                "reason": "GitHub publication remains Codex-owned",
            },
        ]
    )
    return rows


def identity_from_agent_key(agent_key: str | None, *, role: str | None = None) -> str:
    key = (agent_key or "").lower()
    if key.startswith("claude"):
        return "claude"
    if key.startswith("codex"):
        return "codex"
    if "examiner" in key:
        return "examiner"
    if key.startswith("cursor") or not key:
        return "cursor"
    return "cursor"


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ensure_lifecycle(service: DurableCoordinationService) -> dict[str, Any]:
    state = getattr(service, "_lifecycle", None)
    if not isinstance(state, dict) or not state:
        service._lifecycle = empty_lifecycle_state()
    else:
        service._lifecycle.setdefault("processed_event_ids", [])
        service._lifecycle.setdefault("decisions", [])
        service._lifecycle.setdefault("builder_eligible", [])
        service._lifecycle["adapters"] = delivery_adapter_inventory()
        service._lifecycle["first_cycle_go"] = FIRST_CYCLE_GO
        service._lifecycle["delivery_claimed"] = False
    return service._lifecycle


def _actor_allowed(*, actor: str, claimed_identity: str | None) -> bool:
    if actor in PRIVILEGED_ACTORS:
        return True
    return claimed_identity is not None and actor == claimed_identity and actor in KNOWN_AGENTS


def ingest_lifecycle_event(
    db: Session,
    *,
    owner_id: uuid.UUID,
    payload: dict[str, Any],
    actor: str,
    persist: bool = True,
    service: DurableCoordinationService | None = None,
) -> dict[str, Any]:
    """Authenticated production entry. Rejects spoofed identities, forged completions, stale claims."""
    kind = str(payload.get("kind") or "")
    event_id = str(payload.get("event_id") or "")
    if kind not in LIFECYCLE_KINDS:
        return {"outcome": "forged_rejected", "reason": "unknown lifecycle kind", "delivery_claimed": False}
    if not event_id:
        return {"outcome": "forged_rejected", "reason": "missing event_id", "delivery_claimed": False}
    claimed = payload.get("agent_identity")
    if claimed is not None and claimed not in KNOWN_AGENTS:
        return {"outcome": "forged_rejected", "reason": "unknown agent identity", "agent_identity": claimed}
    if not _actor_allowed(actor=actor, claimed_identity=claimed if actor not in PRIVILEGED_ACTORS else claimed):
        return {
            "outcome": "forged_rejected",
            "reason": "spoofed agent identity or unauthorized actor",
            "actor": actor,
            "agent_identity": claimed,
        }

    service = service or load_owner_ledger(db, owner_id=owner_id)
    _ensure_lifecycle(service)
    if event_id in service._lifecycle["processed_event_ids"] or event_id in service._seen_event_ids:
        return {"outcome": "replay_ignored", "event_id": event_id, "delivery_claimed": False}

    if kind == "delivery_claim":
        service._lifecycle["delivery_claimed"] = False
        service._lifecycle["processed_event_ids"].append(event_id)
        if persist:
            _persist_ledger(db, owner_id=owner_id, service=service)
        return {
            "outcome": "blocked",
            "missing_capability": MISSING_MESSAGING,
            "reason": "prepared instruction is not a delivered message",
            "delivery_claimed": False,
            "agent_identity": claimed,
        }
    if kind == "queue_empty_claim":
        result = service.claim_queue_empty(reporter=actor, event_id=event_id)
        service._lifecycle["processed_event_ids"].append(event_id)
        if persist:
            _persist_ledger(db, owner_id=owner_id, service=service)
        return {"outcome": result.outcome, "reason": result.reason, "delivery_claimed": False}
    if kind == "independent_review_receipt" and payload.get("from_github_ci"):
        service._lifecycle["processed_event_ids"].append(event_id)
        _record_decision(
            service,
            kind="github_ci_rejected_as_certification",
            detail={"sha": (payload.get("evidence") or {}).get("sha"), "independently_verified": False},
        )
        if persist:
            _persist_ledger(db, owner_id=owner_id, service=service)
        return {
            "outcome": "stale_rejected",
            "reason": "GitHub green CI is not independent certification",
            "independently_verified": False,
            "delivery_claimed": False,
            "event_id": event_id,
        }

    if kind == "github_observation":
        intake = GitHubDevelopmentIntake(service, owner_id=str(owner_id))
        applied = intake.ingest_pull_request(dict(payload.get("observation") or {}), actor="mainai")
        service._lifecycle["processed_event_ids"].append(event_id)
        _record_decision(
            service,
            kind="github_observation",
            detail={
                "pr": applied.get("pr"),
                "head_sha": applied.get("head_sha"),
                "freeze_allowed": applied.get("freeze_allowed"),
                "independently_verified": False,
                "delivery_claimed": False,
            },
        )
        _follow_up_github(service, owner_id=str(owner_id))
        if persist:
            _persist_ledger(db, owner_id=owner_id, service=service)
        applied["independently_verified"] = False
        applied["delivery_claimed"] = False
        applied["event_id"] = event_id
        return applied

    assignment = _load_assignment(db, owner_id=owner_id, assignment_id=payload.get("assignment_id"))
    if assignment is None and kind != "independent_review_receipt":
        return {"outcome": "stale_rejected", "reason": "unknown or unauthorized assignment", "event_id": event_id}

    if assignment is not None:
        mapped = identity_from_agent_key(_agent_key(db, assignment), role=assignment.role.value)
        if actor not in PRIVILEGED_ACTORS and claimed not in {None, mapped}:
            return {"outcome": "forged_rejected", "reason": "agent identity does not match assignment", "event_id": event_id}
        agent_identity = mapped if actor in PRIVILEGED_ACTORS else str(claimed or mapped)
        applied = _apply_assignment_kind(
            service,
            assignment=assignment,
            kind=kind,
            agent_identity=agent_identity,
            payload=payload,
            event_id=event_id,
            actor=actor,
        )
        _follow_up_assignment(service, assignment=assignment, agent_identity=agent_identity)
        if persist:
            _persist_ledger(db, owner_id=owner_id, service=service)
        return applied

    return {"outcome": "stale_rejected", "reason": "independent review requires a known assignment", "event_id": event_id}


def reconcile_owner(db: Session, *, owner_id: uuid.UUID) -> dict[str, Any]:
    """Worker/application reconciliation: persisted events in, follow-ups out. Restart-safe."""
    service = load_owner_ledger(db, owner_id=owner_id)
    state = _ensure_lifecycle(service)
    processed = set(state["processed_event_ids"])
    rows = list(
        db.execute(
            select(AgentWorkAssignmentEvent)
            .where(AgentWorkAssignmentEvent.owner_id == owner_id)
            .order_by(AgentWorkAssignmentEvent.created_at.asc(), AgentWorkAssignmentEvent.id.asc())
        ).scalars()
    )
    applied = 0
    for event in rows:
        token = f"awa-{event.id}"
        if token in processed or token in service._seen_event_ids:
            continue
        assignment = db.get(AgentWorkAssignment, event.assignment_id)
        if assignment is None or assignment.owner_id != owner_id:
            processed.add(token)
            continue
        if (assignment.provenance or {}).get("created_without_manual_assignment"):
            processed.add(token)
            continue
        kind = _kind_from_assignment_event(event)
        if kind is None:
            service._lifecycle["processed_event_ids"].append(token)
            processed.add(token)
            continue
        ingest_lifecycle_event(
            db,
            owner_id=owner_id,
            actor="worker",
            persist=False,
            service=service,
            payload={
                "kind": kind,
                "event_id": token,
                "assignment_id": str(assignment.id),
                "agent_identity": identity_from_agent_key(_agent_key(db, assignment), role=assignment.role.value),
                "to": (event.detail or {}).get("to"),
                "from": (event.detail or {}).get("from"),
                "evidence": event.detail or {},
            },
        )
        processed = set(service._lifecycle["processed_event_ids"])
        applied += 1
    state["watermark"] = str(rows[-1].id) if rows else state.get("watermark")
    _follow_up_github(service, owner_id=str(owner_id))
    _persist_ledger(db, owner_id=owner_id, service=service)
    view = service.founder_view()
    view["lifecycle"] = lifecycle_view(service)
    view["applied"] = applied
    view["delivery_claimed"] = False
    view["first_cycle_go"] = FIRST_CYCLE_GO
    return view


def reconcile_pending_owners(db: Session, *, limit: int = 20) -> int:
    owner_ids = list(
        db.execute(select(AgentWorkAssignment.owner_id).distinct().limit(limit)).scalars()
    )
    for owner_id in owner_ids:
        reconcile_owner(db, owner_id=owner_id)
    return len(owner_ids)


def lifecycle_view(service: DurableCoordinationService) -> dict[str, Any]:
    state = _ensure_lifecycle(service)
    needed = service.needed_work()
    for item in state.get("builder_eligible") or []:
        needed.setdefault("items", []).append(
            {
                "agent_identity": item.get("agent_identity"),
                "assignment_id": item.get("assignment_id"),
                "need": "eligible_for_further_work",
                "status": "completed",
            }
        )
    github = service.github_intake_view()
    for row in github.get("needed_review") or []:
        if row.get("need") == "independent_review":
            needed.setdefault("items", []).append(
                {
                    "agent_identity": "examiner",
                    "assignment_id": row.get("examiner_assignment_id"),
                    "need": "independent_review",
                    "status": "assigned",
                    "sha": row.get("sha"),
                    "note": "github_green_is_not_independent_certification",
                }
            )
    founder_required = [
        item
        for item in needed.get("items") or []
        if item.get("need") in {"unblock"} and item.get("missing_capability")
    ]
    return {
        "capability": "lifecycle_ingestion",
        "first_cycle_go": FIRST_CYCLE_GO,
        "delivery_claimed": False,
        "adapters": state.get("adapters") or delivery_adapter_inventory(),
        "decisions": list(state.get("decisions") or [])[-20:],
        "builder_eligible": list(state.get("builder_eligible") or []),
        "needed_work": needed,
        "founder_approval_required": founder_required,
        "github_green_is_independent_certification": False,
    }


def _kind_from_assignment_event(event: AgentWorkAssignmentEvent) -> str | None:
    if event.event_type == AgentWorkAssignmentEventType.created:
        return "assignment_observed"
    if event.event_type == AgentWorkAssignmentEventType.status_changed:
        target = (event.detail or {}).get("to")
        if target == WorkAssignmentStatus.completed.value:
            return "completion_claim"
        return "status_changed"
    if event.event_type == AgentWorkAssignmentEventType.execution_observed:
        return "execution_progress"
    return None


def _apply_assignment_kind(
    service: DurableCoordinationService,
    *,
    assignment: AgentWorkAssignment,
    kind: str,
    agent_identity: str,
    payload: dict[str, Any],
    event_id: str,
    actor: str,
) -> dict[str, Any]:
    _ensure_obligation(service, assignment=assignment, agent_identity=agent_identity)
    obligation_id = str(assignment.id)
    if kind == "assignment_observed":
        service._lifecycle["processed_event_ids"].append(event_id)
        return {"outcome": "applied", "assignment_id": obligation_id, "status": "assigned", "delivery_claimed": False}
    if kind == "status_changed":
        target = str(payload.get("to") or assignment.status.value)
        result = _map_status(service, obligation_id=obligation_id, agent_identity=agent_identity, target=target, event_id=event_id)
        service._lifecycle["processed_event_ids"].append(event_id)
        return {"outcome": result.outcome, "status": result.status, "reason": result.reason, "delivery_claimed": False}
    if kind == "execution_progress":
        result = service.record_execution_progress(
            assignment_id=obligation_id,
            agent_identity=agent_identity,
            evidence_kind="adapter_heartbeat",
            evidence=dict(payload.get("evidence") or {"source": "application"}),
            event_id=event_id,
        )
        service._lifecycle["processed_event_ids"].append(event_id)
        return {"outcome": result.outcome, "status": result.status, "delivery_claimed": False}
    if kind == "completion_claim":
        current = service.get(obligation_id)
        if current and current["status"] in {ObligationStatus.assigned.value, ObligationStatus.acknowledged.value}:
            if actor not in PRIVILEGED_ACTORS and not (payload.get("evidence") or {}).get("execution_evidence"):
                return {
                    "outcome": "forged_rejected",
                    "reason": "completion without execution evidence",
                    "assignment_id": obligation_id,
                    "delivery_claimed": False,
                }
            service.record_execution_progress(
                assignment_id=obligation_id,
                agent_identity=agent_identity,
                evidence_kind="code_change",
                evidence={"source": "application_status", "sha": (payload.get("evidence") or {}).get("sha")},
                event_id=f"{event_id}-progress",
            )
        result = service.complete(
            assignment_id=obligation_id,
            agent_identity=agent_identity,
            event_id=event_id,
            evidence={"execution_evidence": True, **dict(payload.get("evidence") or {})},
        )
        service._lifecycle["processed_event_ids"].append(event_id)
        return {"outcome": result.outcome, "status": result.status, "reason": result.reason, "delivery_claimed": False}
    if kind == "independent_review_receipt":
        if payload.get("from_github_ci"):
            service._lifecycle["processed_event_ids"].append(event_id)
            return {
                "outcome": "stale_rejected",
                "reason": "GitHub green CI is not independent certification",
                "delivery_claimed": False,
            }
        examiner = str(payload.get("agent_identity") or "examiner")
        result = service.independently_verify(
            assignment_id=obligation_id,
            examiner_identity=examiner,
            event_id=event_id,
            evidence=dict(payload.get("evidence") or {}),
        )
        service._lifecycle["processed_event_ids"].append(event_id)
        return {"outcome": result.outcome, "status": result.status, "reason": result.reason, "delivery_claimed": False}
    service._lifecycle["processed_event_ids"].append(event_id)
    return {"outcome": "ignored", "delivery_claimed": False}


def _map_status(service, *, obligation_id: str, agent_identity: str, target: str, event_id: str):
    if target in {WorkAssignmentStatus.running.value, WorkAssignmentStatus.ready.value, "waiting_agent"}:
        current = service.get(obligation_id)
        if current and current["status"] == ObligationStatus.assigned.value:
            service.acknowledge(assignment_id=obligation_id, agent_identity=agent_identity, event_id=f"{event_id}-ack")
        return service.record_execution_progress(
            assignment_id=obligation_id,
            agent_identity=agent_identity,
            evidence_kind="adapter_heartbeat",
            evidence={"application_status": target},
            event_id=event_id,
        )
    if target in {WorkAssignmentStatus.blocked.value, "blocked"}:
        return service.block(
            assignment_id=obligation_id,
            agent_identity=agent_identity,
            reason="application_blocked",
            event_id=event_id,
        )
    if target in {WorkAssignmentStatus.failed.value, "failed", "cancelled"}:
        return service.fail(assignment_id=obligation_id, agent_identity=agent_identity, event_id=event_id, reason=target)
    return service.record_execution_progress(
        assignment_id=obligation_id,
        agent_identity=agent_identity,
        evidence_kind="worktree_head",
        evidence={"application_status": target},
        event_id=event_id,
    )


def _ensure_obligation(service: DurableCoordinationService, *, assignment: AgentWorkAssignment, agent_identity: str) -> None:
    service.assign(
        agent_identity=agent_identity,
        workstream=assignment.role.value,
        owner_id=str(assignment.owner_id),
        instruction_id=str(assignment.id),
        assignment_id=str(assignment.id),
        backing_assignment_id=str(assignment.id),
        actor="mainai",
        event_id=f"assign-{assignment.id}",
        dependencies=[],
    )


def _follow_up_assignment(service: DurableCoordinationService, *, assignment: AgentWorkAssignment, agent_identity: str) -> None:
    record = service.get(str(assignment.id))
    if record is None:
        return
    if record["status"] == ObligationStatus.completed.value and assignment.role == WorkAssignmentRole.builder:
        eligible = {"agent_identity": agent_identity, "assignment_id": str(assignment.id), "at": _iso()}
        existing = service._lifecycle["builder_eligible"]
        if not any(row.get("assignment_id") == eligible["assignment_id"] for row in existing):
            existing.append(eligible)
            _record_decision(service, kind="builder_eligible_for_further_work", detail=eligible)
        _follow_up_github(service, owner_id=str(assignment.owner_id))
    if record["status"] == ObligationStatus.blocked.value:
        _record_decision(
            service,
            kind="blocked_retains_dependency",
            detail={"assignment_id": str(assignment.id), "dependencies_retained": True},
        )


def _follow_up_github(service: DurableCoordinationService, *, owner_id: str) -> None:
    intake = GitHubDevelopmentIntake(service, owner_id=owner_id)
    for candidate in (intake.state.get("candidates") or {}).values():
        if not candidate.get("reviewable"):
            continue
        examiner_id = candidate.get("examiner_assignment_id")
        _record_decision(
            service,
            kind="examiner_obligation",
            detail={
                "pr": candidate.get("pr"),
                "sha": candidate.get("head_sha"),
                "examiner_assignment_id": examiner_id,
                "freeze_allowed": (candidate.get("ci") or {}).get("freeze_allowed"),
                "independently_verified": False,
            },
        )


def _record_decision(service: DurableCoordinationService, *, kind: str, detail: dict[str, Any]) -> None:
    decisions = service._lifecycle.setdefault("decisions", [])
    key = (kind, str(detail.get("assignment_id") or detail.get("pr") or detail.get("sha") or kind))
    if any((row.get("kind"), str(row.get("detail", {}).get("assignment_id") or row.get("detail", {}).get("pr") or row.get("detail", {}).get("sha") or row.get("kind"))) == key for row in decisions):
        return
    decisions.append({"kind": kind, "at": _iso(), "detail": detail, "delivery_claimed": False})


def _load_assignment(db: Session, *, owner_id: uuid.UUID, assignment_id: Any) -> AgentWorkAssignment | None:
    if not assignment_id:
        return None
    try:
        aid = uuid.UUID(str(assignment_id))
    except ValueError:
        return None
    row = db.get(AgentWorkAssignment, aid)
    if row is None or row.owner_id != owner_id:
        return None
    return row


def _agent_key(db: Session, assignment: AgentWorkAssignment) -> str | None:
    agent = db.get(CoordinationAgent, assignment.agent_id)
    return None if agent is None else agent.agent_key


def _persist_ledger(db: Session, *, owner_id: uuid.UUID, service: DurableCoordinationService) -> None:
    from app.agent_coordination.persist import persist_owner_ledger

    persist_owner_ledger(db, owner_id=owner_id, service=service, actor="lifecycle")
