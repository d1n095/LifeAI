"""Systemic Debugging Engine. See docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md.

LOCAL CORRECTNESS != SYSTEM CORRECTNESS. FUNCTION PASSES != INTEGRATION PASSES. UNIT TEST PASS
!= SYSTEM PASS. VALID SCHEMA != COMPATIBLE SCHEMA. COMPONENT WORKS != COMPONENTS WORK TOGETHER.
ADAPTER COMPILES != ADAPTER MATCHES REAL IMPLEMENTATION. FIXED BUG != NO REGRESSION. CORRECT
RETURN VALUE != CORRECT SYSTEM EFFECT.

Pure: a `DebugTrace` records which of the SYMPTOM -> ... -> LEARN stages were actually
performed for one bug/fix; `assess_fix_readiness()` gates whether "fixed" may be claimed."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.mainai_cognitive_ops.types import DebugStage

# Every stage is required before a fix may be claimed "fixed, no regression" -- a caller may
# still ship a smaller change (e.g. a documented hotfix) but may not call it fully verified
# without every stage below having actually run.
REQUIRED_STAGES_FOR_FIXED_CLAIM: tuple[DebugStage, ...] = tuple(DebugStage)


@dataclass(frozen=True)
class DebugTrace:
    symptom: str
    completed_stages: frozenset[DebugStage] = field(default_factory=frozenset)
    notes: dict[str, str] = field(default_factory=dict)


def record_stage(trace: DebugTrace, stage: DebugStage, *, note: str | None = None) -> DebugTrace:
    new_notes = dict(trace.notes)
    if note is not None:
        new_notes[stage.value] = note
    return DebugTrace(symptom=trace.symptom, completed_stages=trace.completed_stages | {stage}, notes=new_notes)


@dataclass(frozen=True)
class FixReadiness:
    ready_to_claim_fixed: bool
    missing_stages: tuple[DebugStage, ...]
    reason: str


def assess_fix_readiness(trace: DebugTrace) -> FixReadiness:
    """FIXED BUG != NO REGRESSION and every sibling invariant above are enforced structurally
    here: "fixed" may only be claimed once every stage in `REQUIRED_STAGES_FOR_FIXED_CLAIM` has
    actually been recorded as completed -- a trace that only completed LOCAL_TEST (a passing
    unit test) is explicitly NOT sufficient, matching UNIT TEST PASS != SYSTEM PASS."""

    missing = tuple(s for s in REQUIRED_STAGES_FOR_FIXED_CLAIM if s not in trace.completed_stages)
    if missing:
        return FixReadiness(
            ready_to_claim_fixed=False, missing_stages=missing,
            reason=f"{len(missing)} of {len(REQUIRED_STAGES_FOR_FIXED_CLAIM)} required stages not yet completed: {', '.join(s.value for s in missing)}",
        )
    return FixReadiness(ready_to_claim_fixed=True, missing_stages=(), reason="every required systemic-debugging stage was completed")


def claims_local_correctness_only(trace: DebugTrace) -> bool:
    """LOCAL CORRECTNESS != SYSTEM CORRECTNESS: true when only local-scope stages (up to and
    including LOCAL_TEST) were completed, with none of the system-scope stages (adjacent
    integration test onward) yet run -- the exact shape of a claim that stops too early."""

    local_scope = {
        DebugStage.SYMPTOM, DebugStage.REPRODUCE, DebugStage.ROOT_CAUSE, DebugStage.SMALLEST_SAFE_FIX,
        DebugStage.LOCAL_TEST,
    }
    system_scope = {
        DebugStage.ADJACENT_INTEGRATION_TEST, DebugStage.REAL_RUNTIME_PATH, DebugStage.REGRESSION,
        DebugStage.CRASH_RESTART, DebugStage.NEW_CONFLICT_CHECK,
    }
    return bool(trace.completed_stages & local_scope) and not (trace.completed_stages & system_scope)
