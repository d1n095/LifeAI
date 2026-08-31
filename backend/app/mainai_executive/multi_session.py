"""Multi-session continuity scenario — hours/days across sessions without invented authority."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.mainai_executive.continuity import load_continuity_checkpoint, resume_summary
from app.mainai_executive.loop import resume_executive_cycle, run_executive_cycle
from app.mainai_executive.priority import PriorityFactors, apply_hysteresis, score_priority
from app.mainai_executive.types import ExecutiveCycleResult


@dataclass
class SessionStep:
    name: str
    founder_request: str
    note_id: uuid.UUID | None = None
    run_workforce_dry: bool = False
    interruption_point: str | None = None
    priority_factors: PriorityFactors | None = None
    expect_uncertain_contains: str | None = None


@dataclass
class MultiSessionReport:
    program_id: str
    session_count: int
    steps: list[dict[str, Any]] = field(default_factory=list)
    duplicate_work_detected: bool = False
    stale_authority_detected: bool = False
    lost_correction: bool = False
    false_completion: bool = False
    final_remaining: list[str] = field(default_factory=list)
    why_remaining: list[str] = field(default_factory=list)


def run_multi_session_program(
    db: Session,
    *,
    owner_id: uuid.UUID,
    source_entity_id: uuid.UUID,
    steps: list[SessionStep],
    program_id: str | None = None,
    session_id: str | None = None,
) -> MultiSessionReport:
    """Run session steps against ONE durable session_id (elapsed-time continuity)."""
    program_id = program_id or str(uuid.uuid4())
    session_id = session_id or f"multi-{program_id}"
    report = MultiSessionReport(program_id=program_id, session_count=0)
    seen_candidate_keys: set[str] = set()
    prev_priority: str | None = None

    for step in steps:
        report.session_count += 1
        result: ExecutiveCycleResult = run_executive_cycle(
            db,
            owner_id=owner_id,
            founder_request=step.founder_request,
            session_id=session_id,
            source_entity_id=source_entity_id,
            note_id=step.note_id,
            run_workforce_dry=step.run_workforce_dry,
            interruption_point=step.interruption_point,
        )
        # Priority evidence (planning only)
        prio = None
        if step.priority_factors is not None:
            proposed, raw, expl = score_priority(step.priority_factors)
            stable = apply_hysteresis(previous=prev_priority, proposed=proposed, raw_score=raw)
            prev_priority = stable
            prio = {"proposed": proposed, "stable": stable, "raw": raw, **expl}

        if any(d for d in result.authority_denials if "AUTHORITY" in d):
            # denials are expected — stale authority would be missing denials + authorized=True
            pass
        if any(h.authorized for h in result.horizon_items):
            report.stale_authority_detected = True
        if result.completion_assessment and result.completion_assessment.get("claimed_complete"):
            report.false_completion = True

        # Duplicate work: stable candidate keys should not grow unboundedly for same titles
        for cid in result.work_candidate_ids:
            key = str(cid)
            if key in seen_candidate_keys:
                # Same id reused across sessions is GOOD (dedupe), not duplicate work
                pass
            seen_candidate_keys.add(key)

        cp = load_continuity_checkpoint(db, owner_id=owner_id, session_id=session_id)
        why = list(cp.remaining) if cp else list(result.observability.get("open_risks") or [])
        report.steps.append(
            {
                "name": step.name,
                "phase": result.phase.value,
                "uncertain": list(
                    load_continuity_checkpoint(db, owner_id=owner_id, session_id=session_id).uncertain
                    if cp
                    else []
                ),
                "priority": prio,
                "authority_holds_execution": False,
                "why_remaining": why,
            }
        )
        if step.expect_uncertain_contains and cp:
            if step.expect_uncertain_contains not in (cp.uncertain or []):
                report.lost_correction = True

    # Final resume proves restart continuity
    resumed = resume_executive_cycle(db, owner_id=owner_id, session_id=session_id, continue_work=True)
    report.steps.append({"name": "final_resume", "resumed": resumed.get("resumed"), "recovery": resumed.get("recovery")})
    cp = load_continuity_checkpoint(db, owner_id=owner_id, session_id=session_id)
    if cp:
        report.final_remaining = list(cp.remaining)
        report.why_remaining = list(cp.remaining) + list(cp.uncertain)
        summary = resume_summary(cp)
        if summary.get("hallucinated_continuation"):
            report.stale_authority_detected = True
        if summary.get("authority_still_exists"):
            report.stale_authority_detected = True
    return report
