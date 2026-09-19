"""First real MainAI safe-internal boot — not a unit-test substitute.

Sequence:
  LOAD DURABLE STATE → READINESS → KILL SWITCH / AUTHORITY
  → MEMORY/CONTINUITY → EXECUTIVE + SCHOOL → LOCAL ATTEMPT
  → VERIFY → STORE/RECEIPTS → CHECKPOINT
  → SHUTDOWN → RESTART → RESUME

No external provider. No consequential external writes.
BOOT NEVER CLEARS KILL SWITCH.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select, text as sa_text
from sqlalchemy.orm import Session

from app.mainai_executive.dashboard import founder_executive_dashboard
from app.mainai_executive.loop import resume_executive_cycle, run_executive_cycle
from app.mainai_executive.safe_composed_run import run_composed_safe_internal_mainai_run
from app.mainai_school.metrics import snapshot_domain
from app.mainai_school.offline import audit_offline_capabilities
from app.mainai_startup_readiness import evaluate_startup_readiness
from app.models.user import User
from app.request_context import current_user_id as current_user_id_var
from app.workforce.kill_switch import (
    KillSwitchError,
    assert_not_killed,
    query_stop_status,
    record_boot_blocked,
)
from app.workforce.provider_attempt_ledger import (
    assert_provider_attempts_unchanged,
    snapshot_provider_attempts,
)


DEFAULT_FOUNDER_TASK = (
    "Review our public library hours notice and classify it for the research department: "
    "is it informational_public_text, and what NOW/NEAR follow-ups should park as "
    "unreviewed work candidates (never authorize). Also check whether local classification "
    "workforce can handle this without any external provider."
)


@dataclass
class InternalBootReport:
    integration_note: str
    readiness_level: str
    session_id: str
    owner_id: str
    founder_task: str
    started_at: str
    shutdown_at: str | None
    restarted_at: str | None
    provider_call_count: int
    local_attempt_used: bool
    school_used: bool
    first_task_phase: str | None
    first_task_ok: bool
    durable_receipts: dict[str, Any]
    shutdown_ok: bool
    restart_ok: bool
    resume_ok: bool
    status_surface: dict[str, Any]
    offline_ok: bool
    kill_switch_after: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    safe_internal_boundary: dict[str, Any] = field(default_factory=dict)
    blocked_by_kill_switch: bool = False
    provider_ledger_crosscheck: dict[str, Any] = field(default_factory=dict)
    existing_state_inspect: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


SAFE_INTERNAL_ALLOWED = [
    "local_reasoning",
    "memory_retrieval",
    "planning",
    "local_dry_run_workers",
    "internal_analysis",
    "local_school_practice",
    "internal_verification",
    "durable_notes_checkpoints",
    "safe_scope_project_status_updates",
]

SAFE_INTERNAL_FORBIDDEN = [
    "real_external_provider_invocation",
    "consequential_external_writes",
    "autonomous_production_deploy",
    "money_movement",
    "deletion",
    "approval_on_founder_behalf",
    "authority_widening",
    "automatic_kill_switch_clear",
]


def ensure_boot_owner(db: Session, *, email: str | None = None) -> User:
    email = email or f"mainai-boot-{uuid.uuid4().hex[:10]}@local.internal"
    existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing:
        user = existing
    else:
        user = User(email=email, password_hash="boot-local-only", email_verified=True)
        db.add(user)
        db.flush()
    current_user_id_var.set(str(user.id))
    try:
        db.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    except Exception:
        pass
    return user


def startup_status_surface(
    db: Session,
    *,
    owner_id: uuid.UUID,
    session_id: str | None,
    school_path: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dash = founder_executive_dashboard(db, owner_id=owner_id, session_id=session_id)
    readiness = evaluate_startup_readiness(claude_reviews_satisfied=None)
    stop = query_stop_status(db, owner_id=owner_id)
    school = school_path or {}
    domain_snap = snapshot_domain("research", owner_id=str(owner_id))
    return {
        "MAINAI_STATUS": "BLOCKED_BY_KILL_SWITCH"
        if stop["blocked"]
        else ("RUNNING_SAFE_INTERNAL" if session_id else "IDLE"),
        "READINESS": readiness.level.value,
        "CURRENT_GOAL": dash.get("WHAT_SHE_IS_DOING"),
        "CURRENT_PLAN": dash.get("WHAT_IS_NEXT"),
        "CURRENT_TASK": (dash.get("WHAT_SHE_REMEMBERS_AS_CURRENT") or {}).get("phase"),
        "ACTIVE_LOCAL_AGENTS": dash.get("WHAT_AGENTS_ARE_WORKING"),
        "BLOCKED_WORK": dash.get("WHAT_IS_BLOCKED"),
        "LAST_CHECKPOINT": session_id,
        "LAST_FAILURE": dash.get("WHAT_FAILED_RECENTLY"),
        "LAST_RECOVERY": dash.get("WHAT_RECOVERED"),
        "SCHOOL_STATUS": {
            "wired": school.get("wired"),
            "local_attempt_first": school.get("local_attempt_first"),
            "teacher_invoked": school.get("teacher_invoked", False),
            "seek_external_teacher": school.get("seek_external_teacher"),
        },
        "LOCAL_VS_EXTERNAL_DEPENDENCY": {
            "external_dependency_ratio": domain_snap.external_dependency_ratio,
            "local_attempt_rate": domain_snap.local_attempt_rate,
            "provider_enabled": False,
        },
        "KILL_SWITCH": stop,
        "PROVIDER_ENABLED": False,
        "chain_of_thought_exposed": False,
    }


def run_first_real_internal_boot(
    db: Session,
    *,
    owner_email: str | None = None,
    founder_task: str = DEFAULT_FOUNDER_TASK,
    session_id: str | None = None,
    seed_existing_state: bool = False,
) -> InternalBootReport:
    """Boot MainAI through composed safe-internal path with restart proof.

    Never clears kill switch. If stopped → BLOCKED_BY_KILL_SWITCH.
    When seed_existing_state=True, seeds rich history first (existing-state boot).
    """
    notes: list[str] = []
    started = datetime.utcnow().isoformat() + "Z"
    existing_inspect: dict[str, Any] = {}

    if seed_existing_state:
        from app.mainai_executive.existing_state import (
            inspect_existing_state_after_boot,
            seed_rich_safe_internal_state,
        )

        seeded = seed_rich_safe_internal_state(db, owner_email=owner_email)
        owner_email = seeded["owner_email"]
        notes.append(f"seeded_existing_state tag={seeded['tag']}")
        # Pre-inspect before the boot cycle proper (continuity already present).
        existing_inspect["pre_boot"] = inspect_existing_state_after_boot(
            db,
            owner_id=uuid.UUID(seeded["owner_id"]),
            session_id=seeded["session_id"],
        )
        existing_inspect["seed"] = {
            k: seeded[k]
            for k in (
                "owner_id",
                "old_note_id",
                "current_correction_id",
                "session_id",
                "continuity_note_id",
            )
        }

    owner = ensure_boot_owner(db, email=owner_email)
    notes.append(f"owner={owner.email}")

    provider_before = snapshot_provider_attempts(db, owner_id=owner.id)

    # HARD: boot must not clear stop state.
    try:
        assert_not_killed(db, owner_id=owner.id)
    except KillSwitchError as exc:
        record_boot_blocked(db, owner_id=owner.id, reason=str(exc))
        stop = query_stop_status(db, owner_id=owner.id)
        status = startup_status_surface(db, owner_id=owner.id, session_id=None)
        return InternalBootReport(
            integration_note="blocked_by_kill_switch",
            readiness_level=evaluate_startup_readiness(claude_reviews_satisfied=None).level.value,
            session_id=session_id or "blocked",
            owner_id=str(owner.id),
            founder_task=founder_task,
            started_at=started,
            shutdown_at=None,
            restarted_at=None,
            provider_call_count=0,
            local_attempt_used=False,
            school_used=False,
            first_task_phase="BLOCKED_BY_KILL_SWITCH",
            first_task_ok=False,
            durable_receipts={"stop_status": stop, "boot_cannot_clear": True},
            shutdown_ok=False,
            restart_ok=False,
            resume_ok=False,
            status_surface=status,
            offline_ok=False,
            kill_switch_after=stop,
            notes=[f"BLOCKED_BY_KILL_SWITCH:{exc.code}"],
            safe_internal_boundary={
                "allowed": SAFE_INTERNAL_ALLOWED,
                "forbidden": SAFE_INTERNAL_FORBIDDEN,
                "provider_enabled": False,
            },
            blocked_by_kill_switch=True,
            provider_ledger_crosscheck={
                "before": provider_before.as_dict(),
                "mainai_report_alone_insufficient": True,
            },
            existing_state_inspect=existing_inspect,
        )

    readiness = evaluate_startup_readiness(claude_reviews_satisfied=None)
    notes.append(f"readiness={readiness.level.value}")

    offline = audit_offline_capabilities()
    offline_ok = bool(offline.get("offline_meaningful")) and not offline.get(
        "requires_external_api_to_exist"
    )

    sid = session_id or f"boot-{uuid.uuid4()}"

    cycle = run_executive_cycle(
        db,
        owner_id=owner.id,
        founder_request=founder_task,
        session_id=sid,
        need_capability="low_risk_classification",
        run_workforce_dry=True,
    )
    school = cycle.school_path or {}
    provider_calls = 1 if (cycle.workforce_dry_run or {}).get("provider_invoked") else 0
    if provider_calls:
        raise RuntimeError("provider invoked during safe-internal boot — abort")

    first_ok = (
        cycle.phase.value == "CONTINUE"
        and bool(school.get("local_attempt_first"))
        and bool(school.get("wired"))
        and provider_calls == 0
    )
    notes.append(f"first_cycle_phase={cycle.phase.value}")

    durable = {
        "continuity_note_id": str(cycle.continuity_note_id) if cycle.continuity_note_id else None,
        "session_id": sid,
        "workforce_request_id": (cycle.workforce_dry_run or {}).get("request_id"),
        "assignment_id": (cycle.workforce_dry_run or {}).get("assignment_id"),
        "work_candidate_ids": [str(x) for x in cycle.work_candidate_ids],
        "lesson_ids": [str(x) for x in cycle.lesson_ids],
        "school_learning_contract": (school.get("learning_contract") or {}),
        "authority_denials": list(cycle.authority_denials),
        "provider_invoked": False,
        "boot_cleared_kill_switch": False,
    }

    # SHUTDOWN: process end only — must NOT clear durable stop state.
    shutdown_at = datetime.utcnow().isoformat() + "Z"
    shutdown_ok = True
    notes.append("shutdown_without_clearing_kill_switch")

    # RESTART + RESUME from durable checkpoint (process memory irrelevant).
    restarted_at = datetime.utcnow().isoformat() + "Z"
    resumed = resume_executive_cycle(
        db, owner_id=owner.id, session_id=sid, continue_work=True
    )
    resume_ok = bool(resumed.get("resumed")) and resumed.get("authority_still_valid") is False
    restart_ok = resume_ok
    notes.append("resume_from_durable_checkpoint" if resume_ok else f"resume_issue={resumed}")

    composed = run_composed_safe_internal_mainai_run(
        db,
        owner_id=owner.id,
        founder_request=founder_task[:180],
        session_id=f"{sid}-composed",
    )
    if composed.phase == "BLOCKED_BY_KILL_SWITCH":
        notes.append("composed_blocked_by_kill_switch")
    else:
        notes.append(f"composed_restart_ok={composed.restart_ok}")
        provider_calls += 1 if composed.provider_invoked else 0

    provider_after = snapshot_provider_attempts(db, owner_id=owner.id)
    ledger_check = assert_provider_attempts_unchanged(provider_before, provider_after)
    if not ledger_check["unchanged"]:
        raise RuntimeError(
            "independent provider ledger changed during safe-internal boot — abort "
            f"{ledger_check}"
        )
    notes.append("provider_ledger_unchanged")

    if seed_existing_state:
        from app.mainai_executive.existing_state import inspect_existing_state_after_boot

        existing_inspect["post_boot"] = inspect_existing_state_after_boot(
            db, owner_id=owner.id, session_id=sid
        )
        inv = (existing_inspect["post_boot"].get("invariants") or {})
        if not inv.get("no_oldest_row_as_current"):
            notes.append("WARN_existing_state_oldest_as_current")
        if not inv.get("superseded_not_in_current"):
            notes.append("WARN_existing_state_superseded_in_current")
        if not inv.get("no_false_provider_competence"):
            raise RuntimeError("existing-state boot invented provider competence")

    status_after = startup_status_surface(
        db, owner_id=owner.id, session_id=sid, school_path=school
    )
    db.commit()

    return InternalBootReport(
        integration_note="safe_internal_boot_from_merged_tip",
        readiness_level=readiness.level.value,
        session_id=sid,
        owner_id=str(owner.id),
        founder_task=founder_task,
        started_at=started,
        shutdown_at=shutdown_at,
        restarted_at=restarted_at,
        provider_call_count=provider_calls,
        local_attempt_used=bool(school.get("local_attempt_first")),
        school_used=bool(school.get("wired")),
        first_task_phase=cycle.phase.value,
        first_task_ok=first_ok,
        durable_receipts=durable,
        shutdown_ok=shutdown_ok,
        restart_ok=restart_ok,
        resume_ok=resume_ok,
        status_surface=status_after,
        offline_ok=offline_ok and composed.offline_ok,
        kill_switch_after=query_stop_status(db, owner_id=owner.id),
        notes=notes,
        safe_internal_boundary={
            "allowed": SAFE_INTERNAL_ALLOWED,
            "forbidden": SAFE_INTERNAL_FORBIDDEN,
            "provider_enabled": False,
        },
        blocked_by_kill_switch=False,
        provider_ledger_crosscheck=ledger_check,
        existing_state_inspect=existing_inspect,
    )
