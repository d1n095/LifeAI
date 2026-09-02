"""Composed SAFE INTERNAL MainAI run — executive + school, no external provider.

Proves: founder request → interpret → local capability → local attempt →
verify → school learn → checkpoint → resume. API failure != MainAI failure.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.mainai_executive.loop import resume_executive_cycle, run_executive_cycle
from app.mainai_school.offline import audit_offline_capabilities
from app.workforce.kill_switch import assert_not_killed


@dataclass
class ComposedSafeInternalReport:
    session_id: str
    phase: str
    local_attempt_first: bool
    school_wired: bool
    provider_invoked: bool
    restart_ok: bool
    offline_ok: bool
    teacher_invoked: bool
    authority_denials: list[str]
    notes: list[str] = field(default_factory=list)
    observability: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "phase": self.phase,
            "local_attempt_first": self.local_attempt_first,
            "school_wired": self.school_wired,
            "provider_invoked": self.provider_invoked,
            "restart_ok": self.restart_ok,
            "offline_ok": self.offline_ok,
            "teacher_invoked": self.teacher_invoked,
            "authority_denials": self.authority_denials,
            "notes": self.notes,
            "milestone": "COMPOSED_SAFE_INTERNAL_EXECUTIVE_SCHOOL",
            "escalated_to_provider": False,
            "api_dependency_required": False,
        }


def run_composed_safe_internal_mainai_run(
    db: Session,
    *,
    owner_id: uuid.UUID,
    founder_request: str = "Classify this public library notice as research/low-risk text.",
    session_id: str | None = None,
) -> ComposedSafeInternalReport:
    """Full chain without external APIs."""
    notes: list[str] = []
    # NEVER clear kill switch on boot — BOOT != FOUNDER ACK.
    try:
        assert_not_killed(db, owner_id=owner_id)
    except Exception as exc:
        from app.workforce.kill_switch import KillSwitchError, record_boot_blocked, query_stop_status

        if isinstance(exc, KillSwitchError):
            record_boot_blocked(db, owner_id=owner_id, reason=str(exc))
            status = query_stop_status(db, owner_id=owner_id)
            return ComposedSafeInternalReport(
                session_id=session_id or "blocked",
                phase="BLOCKED_BY_KILL_SWITCH",
                local_attempt_first=False,
                school_wired=False,
                provider_invoked=False,
                restart_ok=False,
                offline_ok=False,
                teacher_invoked=False,
                authority_denials=["BOOT_CANNOT_CLEAR_KILL_SWITCH"],
                notes=[f"blocked:{exc.code}", str(status)],
                observability={"stop_status": status, "code": exc.code},
            )
        raise

    offline = audit_offline_capabilities()
    offline_ok = bool(offline.get("offline_meaningful")) and not offline.get(
        "requires_external_api_to_exist"
    )
    notes.append(f"offline_ok={offline_ok}")

    sid = session_id or f"safe-int-{uuid.uuid4()}"
    result = run_executive_cycle(
        db,
        owner_id=owner_id,
        founder_request=founder_request,
        session_id=sid,
        need_capability="low_risk_classification",
        run_workforce_dry=True,
    )
    school = result.school_path or {}
    provider_invoked = bool((result.workforce_dry_run or {}).get("provider_invoked"))
    if provider_invoked:
        raise RuntimeError("composed safe internal must not invoke provider")

    resumed = resume_executive_cycle(db, owner_id=owner_id, session_id=sid, continue_work=True)
    restart_ok = bool(resumed.get("resumed")) and resumed.get("authority_still_valid") is False
    notes.append(f"resume={resumed.get('resumed')} authority_still_valid={resumed.get('authority_still_valid')}")

    return ComposedSafeInternalReport(
        session_id=sid,
        phase=result.phase.value,
        local_attempt_first=bool(school.get("local_attempt_first"))
        or bool((result.workforce_dry_run or {}).get("local_attempt_first")),
        school_wired=bool(school.get("wired")),
        provider_invoked=False,
        restart_ok=restart_ok,
        offline_ok=offline_ok,
        teacher_invoked=bool(school.get("teacher_invoked")),
        authority_denials=list(result.authority_denials),
        notes=notes,
        observability={
            "school": school,
            "workforce_dry_run": result.workforce_dry_run,
            "continuity_note_id": str(result.continuity_note_id) if result.continuity_note_id else None,
        },
    )
