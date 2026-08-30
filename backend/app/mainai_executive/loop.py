"""Composed MainAI executive cycle — one end-to-end planning/ops loop.

Composes:
  founder request → lookaround (context + lessons + candidates)
  → memory linkage (optional note)
  → missing-piece detection
  → staffing decision
  → workforce dry-run (activate_provider=False only)
  → durable continuity checkpoint
  → observability snapshot

NEVER:
  - authorize_work_candidate
  - activate_provider=True
  - treat memory / plan / staffing as execution authority
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.mainai_executive.assumption_scan import scan_assumptions_and_conflicts
from app.mainai_executive.bounds import ExecutiveScanBounds
from app.mainai_executive.completion import assess_completion
from app.mainai_executive.continuity import (
    build_checkpoint_from_cycle,
    load_continuity_checkpoint,
    resume_summary,
    save_continuity_checkpoint,
)
from app.mainai_executive.lookaround import run_executive_lookaround
from app.mainai_executive.missing_piece import detect_missing_pieces
from app.mainai_executive.observability import executive_status_snapshot
from app.mainai_executive.types import ExecutiveCycleResult, ExecutivePhase
from app.memory_work_linkage import TimingClass, apply_memory_work_linkage
from app.workforce.staff_manager import decide_staffing
from app.workforce.vertical_slice import run_low_risk_classification_slice

logger = logging.getLogger(__name__)

AUTHORITY_DENIALS = [
    "MODEL_OUTPUT_IS_NOT_AUTHORITY",
    "MEMORY_IS_NOT_AUTHORITY",
    "FUTURE_PLAN_IS_NOT_FUTURE_AUTHORITY",
    "STAFFING_DECISION_IS_NOT_AUTHORITY",
    "WORKFORCE_DRY_RUN_IS_NOT_PROVIDER_ACTIVATION",
    "EXECUTIVE_CYCLE_DOES_NOT_AUTHORIZE_WORK",
]


def run_executive_cycle(
    db: Session,
    *,
    owner_id: uuid.UUID,
    founder_request: str,
    session_id: str | None = None,
    source_entity_id: uuid.UUID | None = None,
    note_id: uuid.UUID | None = None,
    need_capability: str = "low_risk_classification",
    bounds: ExecutiveScanBounds | None = None,
    run_workforce_dry: bool = True,
    interruption_point: str | None = None,
) -> ExecutiveCycleResult:
    """Run one composed executive cycle. Planning + dry-run only."""
    bounds = bounds or ExecutiveScanBounds()
    session_id = session_id or str(uuid.uuid4())
    completed: list[str] = []
    uncertain: list[str] = []
    remaining: list[str] = [
        "VERIFY",
        "STORE",
        "LEARN",
        "REPLAN",
        "CONTINUE",
    ]

    # UNDERSTAND + CONNECT + PLAN (lookaround)
    look = run_executive_lookaround(
        db,
        owner_id=owner_id,
        founder_request=founder_request,
        session_id=session_id,
        source_entity_id=source_entity_id,
        bounds=bounds,
    )
    completed.extend(["UNDERSTAND", "CONNECT", "PLAN"])
    if interruption_point == "mid_plan":
        remaining = ["ACT", "VERIFY", "STORE", "LEARN", "REPLAN", "CONTINUE"]
        return _finalize(
            db,
            owner_id=owner_id,
            session_id=session_id,
            founder_request=founder_request,
            phase=ExecutivePhase.PLAN,
            look=look,
            note_id=note_id,
            source_entity_id=source_entity_id,
            staffing_action=None,
            staffing_reason=None,
            workforce_dry_run=None,
            completed=completed,
            uncertain=["plan_incomplete_due_to_interruption"],
            remaining=remaining,
            interruption_point=interruption_point,
            missing_pieces=[],
            assumption_scan={},
        )

    # Memory → work linkage (park only; never insert authorized tasks)
    linkage_actions: list[str] = []
    if note_id is not None:
        try:
            result = apply_memory_work_linkage(
                db,
                owner_id=owner_id,
                note_id=note_id,
                timing=TimingClass.NOW,
                park_candidate=True,
                insert_subordinate=False,
            )
            linkage_actions = [a.value if hasattr(a, "value") else str(a) for a in result.actions]
            look.work_candidate_ids.extend(result.created_candidate_ids)
            completed.append("MEMORY_LINKAGE")
        except Exception:
            logger.exception("memory linkage failed; continuing without authority escalation")
            uncertain.append("memory_linkage_failed")

    missing = detect_missing_pieces(founder_request=founder_request)
    completed.append("MISSING_PIECE_SCAN")

    assumption_scan = scan_assumptions_and_conflicts(
        db,
        owner_id=owner_id,
        lesson_tags=list(look.lesson_tags),
    )
    if assumption_scan.get("assumption_invalidation_requires_replan"):
        uncertain.append("assumptions_or_lesson_conflicts_need_review")
        remaining = ["REPLAN"] + [r for r in remaining if r != "REPLAN"]
    completed.append("ASSUMPTION_SCAN")

    # Workforce decision (intelligence only)
    staffing = decide_staffing(
        db,
        owner_id=owner_id,
        need_capability=need_capability,
        need_summary=founder_request[:200],
        prefer_local_only=True,
    )
    completed.append("STAFFING_DECISION")

    workforce_dry: dict[str, Any] | None = None
    if run_workforce_dry and staffing.action in ("use_existing", "create_candidate", "refuse"):
        try:
            from app.workforce.kill_switch import assert_not_killed

            assert_not_killed()
        except Exception as exc:
            uncertain.append(f"kill_switch_blocked:{type(exc).__name__}")
            run_workforce_dry = False
            workforce_dry = None
        if run_workforce_dry:
            # Always dry-run classification path — never provider.
            slice_result = run_low_risk_classification_slice(
                db,
                owner_id=owner_id,
                note_excerpt=founder_request[:240],
                activate_provider=False,
            )
            workforce_dry = {
                "request_id": str(slice_result.request_id),
                "assignment_id": str(slice_result.assignment_id),
                "verification_status": slice_result.verification_status,
                "provider_invoked": slice_result.provider_invoked,
                "consequential_effects": slice_result.consequential_effects,
                "activate_provider": False,
            }
            completed.append("ACT_DRY_RUN")
            if interruption_point == "after_delegation_before_result":
                uncertain.append("delegation_result_unknown")
                remaining = ["VERIFY", "STORE", "LEARN", "REPLAN", "CONTINUE"]
            elif interruption_point == "after_verify_before_memory":
                completed.append("VERIFY")
                remaining = ["STORE", "LEARN", "REPLAN", "CONTINUE"]
                uncertain.append("memory_update_pending")
            else:
                completed.append("VERIFY")

    phase = ExecutivePhase.CONTINUE if not interruption_point else ExecutivePhase.ACT
    if not interruption_point:
        completed.extend(["STORE", "LEARN"])
        remaining = ["REPLAN", "CONTINUE"]
        # After successful dry cycle, mark continue-ready.
        phase = ExecutivePhase.CONTINUE
        remaining = ["CONTINUE"]

    return _finalize(
        db,
        owner_id=owner_id,
        session_id=session_id,
        founder_request=founder_request,
        phase=phase,
        look=look,
        note_id=note_id,
        source_entity_id=source_entity_id,
        staffing_action=staffing.action,
        staffing_reason=staffing.reason,
        workforce_dry_run=workforce_dry,
        completed=completed,
        uncertain=uncertain,
        remaining=remaining,
        interruption_point=interruption_point,
        missing_pieces=[missing],
        linkage_actions=linkage_actions,
        assumption_scan=assumption_scan,
    )


def resume_executive_cycle(
    db: Session,
    *,
    owner_id: uuid.UUID,
    session_id: str,
    continue_work: bool = True,
) -> dict[str, Any]:
    """Recover from durable checkpoint only. No hallucinated continuation."""
    cp = load_continuity_checkpoint(db, owner_id=owner_id, session_id=session_id)
    if cp is None:
        return {
            "resumed": False,
            "reason": "no_durable_checkpoint",
            "hallucinated_continuation": False,
        }
    summary = resume_summary(cp)
    if not continue_work:
        return {"resumed": True, "continued": False, "recovery": summary}

    # Re-enter from durable state — does not invent missing authority.
    result = run_executive_cycle(
        db,
        owner_id=owner_id,
        founder_request=cp.founder_request,
        session_id=session_id,
        source_entity_id=uuid.UUID(cp.source_entity_id) if cp.source_entity_id else None,
        note_id=uuid.UUID(cp.note_id) if cp.note_id else None,
        run_workforce_dry=True,
    )
    return {
        "resumed": True,
        "continued": True,
        "recovery": summary,
        "new_phase": result.phase.value,
        "authority_still_valid": False,
        "needs_founder_confirmation": True,
        "session_id": result.session_id,
    }


def _finalize(
    db: Session,
    *,
    owner_id: uuid.UUID,
    session_id: str,
    founder_request: str,
    phase: ExecutivePhase,
    look: Any,
    note_id: uuid.UUID | None,
    source_entity_id: uuid.UUID | None,
    staffing_action: str | None,
    staffing_reason: str | None,
    workforce_dry_run: dict[str, Any] | None,
    completed: list[str],
    uncertain: list[str],
    remaining: list[str],
    interruption_point: str | None,
    missing_pieces: list[dict[str, Any]],
    linkage_actions: list[str] | None = None,
    assumption_scan: dict[str, Any] | None = None,
) -> ExecutiveCycleResult:
    wf_req = None
    if workforce_dry_run and workforce_dry_run.get("request_id"):
        wf_req = uuid.UUID(workforce_dry_run["request_id"])

    checkpoint = build_checkpoint_from_cycle(
        session_id=session_id,
        phase=phase,
        founder_request=founder_request,
        context_set_id=look.context_set_id,
        note_id=note_id,
        source_entity_id=source_entity_id,
        work_candidate_ids=list(look.work_candidate_ids),
        lesson_ids=list(look.lesson_ids),
        horizon_items=list(look.horizon_items),
        staffing_action=staffing_action,
        workforce_request_id=wf_req,
        completed=completed,
        uncertain=uncertain,
        remaining=remaining,
        interruption_point=interruption_point,
    )
    cont_note = save_continuity_checkpoint(db, owner_id=owner_id, checkpoint=checkpoint)
    obs = executive_status_snapshot(db, owner_id=owner_id, session_id=session_id)
    obs["assumption_scan"] = assumption_scan or {}
    obs["staffing_reason"] = staffing_reason
    contradiction_refs = list((assumption_scan or {}).get("contradiction_refs") or [])
    obs["contradiction_refs"] = contradiction_refs
    completion = assess_completion(
        feature="executive_cycle",
        evidence={
            "designed": True,
            "implemented": True,
            "tested": False,  # caller/tests flip this
            "integrated": True,
            "runtime_proven": bool(workforce_dry_run),
            "recoverable": True,
            "documented": True,
            "observed": True,
            "security_tested": False,
            "production_ready": False,
        },
    )
    return ExecutiveCycleResult(
        session_id=session_id,
        phase=phase,
        context_set_id=look.context_set_id,
        note_id=note_id,
        lesson_ids=list(look.lesson_ids),
        work_candidate_ids=list(look.work_candidate_ids),
        horizon_items=list(look.horizon_items),
        staffing_action=staffing_action,
        staffing_reason=staffing_reason,
        workforce_dry_run=workforce_dry_run,
        continuity_note_id=cont_note.id,
        scan_bound_reached=bool(look.scan_bound_reached),
        observability=obs,
        authority_denials=list(AUTHORITY_DENIALS),
        missing_pieces=missing_pieces,
        completion_assessment=completion,
        contradiction_refs=contradiction_refs,
    )
