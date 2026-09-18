from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.mainai_founder_boot.authority import DEFAULT_FOUNDER_BOOT_AUTHORITY_PROFILE, reject_self_grant, require_capability
from app.mainai_founder_boot.covenant import ensure_default_covenant, reject_runtime_covenant_mutation
from app.mainai_founder_boot.readiness import assess_personal_recall, build_readiness_matrix, component_manifest, required_boot_blockers
from app.mainai_founder_boot.types import BootStatus, PresenceState, RecallBootStatus
from app.mainai_level2.canonical import CanonicalProgramStore
from app.models.mainai_founder_boot import MainAIFounderBoot, MainAIFounderBootEvent, MainAIFounderBootStatus
from app.models.user import User, UserRole

MAINAI_ID = "mainai-founder-only"


class FounderBootError(ValueError):
    pass


@dataclass(frozen=True)
class FounderBootResult:
    boot: MainAIFounderBoot
    founder_brief: dict[str, Any]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_founder_user(user: User) -> None:
    if not user.is_active or user.role != UserRole.founder:
        raise FounderBootError("founder-only boot requires an active founder user")


def _next_sequence(db: Session, *, boot_id: uuid.UUID) -> int:
    value = db.execute(select(func.max(MainAIFounderBootEvent.sequence)).where(MainAIFounderBootEvent.boot_id == boot_id)).scalar_one()
    return int(value or 0) + 1


def append_boot_event(db: Session, *, boot: MainAIFounderBoot, event_type: str, metadata: dict | None = None) -> MainAIFounderBootEvent:
    event = MainAIFounderBootEvent(
        id=uuid.uuid4(), owner_id=boot.owner_id, boot_id=boot.boot_id,
        sequence=_next_sequence(db, boot_id=boot.boot_id), event_type=event_type,
        metadata_json=metadata or {},
    )
    db.add(event)
    db.flush()
    return event


def _write_status(db: Session, *, boot: MainAIFounderBoot, state: PresenceState, payload: dict) -> None:
    db.execute(update(MainAIFounderBootStatus).where(
        MainAIFounderBootStatus.owner_id == boot.owner_id,
        MainAIFounderBootStatus.boot_id == boot.boot_id,
        MainAIFounderBootStatus.is_current.is_(True),
    ).values(is_current=False))
    row = MainAIFounderBootStatus(
        id=uuid.uuid4(), owner_id=boot.owner_id, boot_id=boot.boot_id,
        presence_state=state.value, status_payload=payload, is_current=True,
    )
    db.add(row)
    boot.presence_state = state.value
    db.flush()


def founder_brief_for_boot(boot: MainAIFounderBoot) -> dict[str, Any]:
    readiness = boot.readiness_matrix or {}
    disabled = sorted(k for k, v in readiness.items() if v.get("STATE") == "DISABLED")
    limited = sorted(k for k, v in readiness.items() if v.get("STATE") == "LIMITED")
    blocked = sorted(k for k, v in readiness.items() if v.get("STATE") == "BLOCKED")
    return {
        "WHAT_WAS_REQUESTED": "Founder-only MainAI boot",
        "STATUS": boot.status,
        "PRESENCE": boot.presence_state,
        "BOOT_ID": str(boot.boot_id),
        "MAINAI_ID": boot.mainai_id,
        "FOUNDER_ID": str(boot.founder_id),
        "COVENANT_VERSION": boot.covenant_version,
        "RECALL": boot.recall_status,
        "COMPONENTS_LOADED": sorted((boot.component_manifest or {}).keys()),
        "DISABLED": disabled,
        "LIMITED": limited,
        "BLOCKED": blocked,
        "AUTHORITY_PROFILE": boot.authority_profile,
        "WHAT_IS_VERIFIED": "Verified Level-2 foundation and integrated components are loadable",
        "WHAT_IS_NOT_VERIFIED": "Unrestricted real providers, merge/deploy, unsafe recall activation, unrestricted computer control",
        "WHAT_NEEDS_FOUNDER": [] if not blocked else blocked,
        "NEXT_SAFE_ACTION": "Use bounded founder-only status/reasoning/program operations" if boot.status in {"READY", "LIMITED"} else "Resolve boot blockers",
    }


def boot_mainai_founder_only(
    db: Session,
    *,
    founder: User,
    founder_request: str | None = None,
    create_safe_program: bool = False,
    event_hook: Callable[[Session, MainAIFounderBoot, str], None] | None = None,
) -> FounderBootResult:
    _require_founder_user(founder)
    covenant = ensure_default_covenant(db, owner_id=founder.id, created_by="system")
    manifest = component_manifest()
    recall_status, recall_blocker, recall_evidence = assess_personal_recall()
    readiness = build_readiness_matrix(covenant_ready=True, founder_ready=True, db_ready=True)
    blockers = required_boot_blockers(readiness)
    status = BootStatus.BLOCKED if blockers else (BootStatus.LIMITED if recall_status != RecallBootStatus.READY else BootStatus.READY)
    presence = PresenceState.BLOCKED if status == BootStatus.BLOCKED else PresenceState.READY
    boot = MainAIFounderBoot(
        id=uuid.uuid4(), owner_id=founder.id, boot_id=uuid.uuid4(), mainai_id=MAINAI_ID,
        founder_id=founder.id, system_instance_id=f"local-founder-only-{uuid.uuid4()}",
        covenant_id=covenant.id, covenant_version=covenant.version, mode="FOUNDER_ONLY",
        status=status.value, recall_status=recall_status.value,
        status_reason="; ".join(blockers) if blockers else (recall_blocker if recall_status != RecallBootStatus.READY else "all required boot gates passed"),
        component_manifest=manifest, readiness_matrix=readiness,
        authority_profile={**DEFAULT_FOUNDER_BOOT_AUTHORITY_PROFILE, "covenant_hash": covenant.covenant_hash},
        presence_state=presence.value, ready_at=_now() if status in {BootStatus.READY, BootStatus.LIMITED} else None,
        audit_summary={"founder_request": founder_request, "recall_evidence": recall_evidence},
    )
    db.add(boot)
    db.flush()
    def _emit(event_type: str, metadata: dict | None = None) -> None:
        append_boot_event(db, boot=boot, event_type=event_type, metadata=metadata)
        if event_hook is not None:
            event_hook(db, boot, event_type)

    _emit("PROCESS_START", {"sha": manifest["level2_base"]["candidate_sha"]})
    _emit("FOUNDER_BOUND", {"founder_id": str(founder.id), "mode": "FOUNDER_ONLY"})
    _emit("COVENANT_LOADED", {"version": covenant.version, "hash": covenant.covenant_hash})
    _emit("READINESS_DERIVED", {"status": status.value, "recall": recall_status.value})
    if create_safe_program and status in {BootStatus.READY, BootStatus.LIMITED}:
        store = CanonicalProgramStore(db)
        program = store.create_level2_program(
            owner_id=founder.id,
            objective=founder_request or "Founder-only boot status program",
            scope=["local_status", "no_external_effects"],
            acceptance=["founder brief produced"],
            verification=["no self-granted authority"],
            authority_boundary=["no_merge", "no_deploy", "no_unrestricted_provider", "no_computer_control"],
            budget={"usd": 0, "provider": "none"},
        )
        boot.active_program_id = program.id
        _emit("SAFE_PROGRAM_CREATED", {"program_id": str(program.id)})
    brief = founder_brief_for_boot(boot)
    boot.audit_summary = {**(boot.audit_summary or {}), "founder_brief": brief}
    _write_status(db, boot=boot, state=presence, payload={"status": boot.status, "brief": brief})
    db.flush()
    return FounderBootResult(boot=boot, founder_brief=brief)


def stop_mainai(db: Session, *, boot_id: uuid.UUID, owner_id: uuid.UUID, reason: str, stopped_by: str = "founder") -> MainAIFounderBoot:
    boot = db.execute(select(MainAIFounderBoot).where(MainAIFounderBoot.boot_id == boot_id, MainAIFounderBoot.owner_id == owner_id).execution_options(populate_existing=True)).scalar_one_or_none()
    if boot is None:
        raise FounderBootError("boot not found for owner")
    boot.status = BootStatus.STOPPED.value
    boot.presence_state = PresenceState.OFFLINE.value
    boot.stopped_at = _now()
    boot.stop_reason = reason
    append_boot_event(db, boot=boot, event_type="CLEAN_SHUTDOWN", metadata={"reason": reason, "stopped_by": stopped_by, "new_effects": "stopped"})
    _write_status(db, boot=boot, state=PresenceState.OFFLINE, payload={"status": boot.status, "reason": reason})
    db.flush()
    return boot


def recover_boot(db: Session, *, owner_id: uuid.UUID, boot_id: uuid.UUID) -> dict[str, Any]:
    boot = db.execute(select(MainAIFounderBoot).where(MainAIFounderBoot.owner_id == owner_id, MainAIFounderBoot.boot_id == boot_id).execution_options(populate_existing=True)).scalar_one_or_none()
    if boot is None:
        raise FounderBootError("boot not found for owner")
    events = db.execute(select(MainAIFounderBootEvent).where(MainAIFounderBootEvent.owner_id == owner_id, MainAIFounderBootEvent.boot_id == boot_id).order_by(MainAIFounderBootEvent.sequence).execution_options(populate_existing=True)).scalars().all()
    return {"boot_id": str(boot.boot_id), "status": boot.status, "presence": boot.presence_state, "events": len(events), "source": "postgresql", "active_program_id": str(boot.active_program_id) if boot.active_program_id else None}


def answer_status_request(db: Session, *, boot_id: uuid.UUID, owner_id: uuid.UUID) -> dict[str, Any]:
    boot = db.execute(select(MainAIFounderBoot).where(MainAIFounderBoot.boot_id == boot_id, MainAIFounderBoot.owner_id == owner_id).execution_options(populate_existing=True)).scalar_one_or_none()
    if boot is None:
        raise FounderBootError("boot not found for owner")
    append_boot_event(db, boot=boot, event_type="FOUNDER_STATUS_REQUEST", metadata={"authority": "none"})
    brief = founder_brief_for_boot(boot)
    _write_status(db, boot=boot, state=PresenceState.LISTENING, payload={"status": boot.status, "brief": brief})
    return brief


def reason_about_conflict(*, founder_preference: str, evidence: str) -> dict[str, Any]:
    from app.mainai_executive.judgment import decide_judgment
    decision = decide_judgment(confidence=0.85, evidence_strength=0.8, founder_originated=True, stakes=0.6, urgency=0.2, founder_attention_cost=0.2)
    return {
        "founder_preference": founder_preference,
        "evidence": evidence,
        "decision": decision.action.value,
        "authorized": decision.authorized,
        "message": "I may disagree when evidence warrants it, but this grants no execution authority.",
    }


def runtime_attempt_covenant_rewrite() -> None:
    reject_runtime_covenant_mutation(actor="mainai_runtime")


def attempt_self_grant(capability: str) -> None:
    reject_self_grant(capability)


def attempt_computer_control(profile: dict, capability: str) -> dict[str, Any]:
    decision = require_capability(profile, capability)
    if not decision.allowed:
        raise PermissionError(decision.reason)
    return {"allowed": True, "capability": capability}


__all__ = [
    "FounderBootError", "FounderBootResult", "boot_mainai_founder_only", "stop_mainai", "recover_boot",
    "answer_status_request", "founder_brief_for_boot", "reason_about_conflict", "runtime_attempt_covenant_rewrite",
    "attempt_self_grant", "attempt_computer_control",
]
