"""Real-state composition for wait/assign and continue/handoff decisions. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md §C/§D.

This is the NORMAL path a caller should use -- it derives competency/rework/context/quota
signals from real durable state (`signal_derivation.py`) and only THEN calls the existing,
already-tested pure `wait_or_assign.decide_wait_or_assign()`/
`continuation_policy.decide_continue_or_handoff()` unchanged. The pure functions remain
directly callable for advanced/override-only use; this module is real COMPOSITION, not a
parallel reimplementation -- every decision it returns carries the exact `SignalEnvelope`
provenance that produced each number, so a caller (or a test) can always see whether a value
was OBSERVED, DERIVED, ESTIMATED, or CALLER_SUPPLIED.

ONE SUCCESS != MASTERY. ONE FAILURE != INCOMPETENCE. PROFILE != AUTHORITY. RESOURCE
INTELLIGENCE != AUTHORITY. RECOMMENDATION != AUTHORIZATION -- this module's own outputs are
exactly as advisory as the pure functions they wrap."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.mainai_cognitive_ops.types import AgentState
from app.mainai_workforce.continuation_policy import ContinuationResult, decide_continue_or_handoff
from app.mainai_workforce.signal_derivation import (
    derive_competency_signal,
    derive_context_loaded_relevance_signal,
    derive_rework_rate_signal,
    resolve_signal,
)
from app.mainai_workforce.types import SignalEnvelope, SignalOrigin
from app.mainai_workforce.wait_or_assign import WaitOrAssignResult, decide_wait_or_assign

# When neither a real signal nor a caller override exists for competency, this is the ONE
# documented, hand-picked neutral fallback -- always tagged ESTIMATED, never silently treated
# as a real measurement. Chosen as the midpoint of the 0..1 range: neither optimistic nor
# pessimistic in the absence of any real evidence.
NEUTRAL_COMPETENCY_ESTIMATE = 0.5
NEUTRAL_REWORK_ESTIMATE = 0.0  # absent any rework evidence, assume none observed yet -- not "known to be reliable"


@dataclass(frozen=True)
class ResolvedSignal:
    value: float
    origin: SignalOrigin
    explanation: str


def _resolve_with_neutral_fallback(envelope: SignalEnvelope, *, override: float | None, neutral: float) -> ResolvedSignal:
    value, origin, explanation = resolve_signal(envelope, override=override)
    if value is None:
        return ResolvedSignal(neutral, SignalOrigin.ESTIMATED, f"{explanation}; falling back to documented neutral estimate {neutral}")
    return ResolvedSignal(value, origin, explanation)


@dataclass(frozen=True)
class WaitOrAssignSignalReport:
    best_agent_competency: ResolvedSignal
    candidate_agent_competency: ResolvedSignal


@dataclass(frozen=True)
class WaitOrAssignWithProvenance:
    result: WaitOrAssignResult
    signals: WaitOrAssignSignalReport


def decide_wait_or_assign_from_real_state(
    db: Session,
    *,
    owner_id: uuid.UUID,
    capability_key: str,
    task_class: str,
    best_agent_id: uuid.UUID,
    candidate_agent_id: uuid.UUID,
    best_agent_available: bool,
    best_agent_eta_seconds: float | None = None,
    best_agent_teacher: str | None = None,
    candidate_agent_teacher: str | None = None,
    best_agent_competency_override: float | None = None,
    candidate_agent_competency_override: float | None = None,
    critical_path: bool = False,
    duplication_risk: bool = False,
    branch_conflict_risk: bool = False,
) -> WaitOrAssignWithProvenance:
    """Derives both agents' competency for THIS `capability_key`/`task_class` from real state
    (Capability Mastery Ledger -> capability_reality -> resource_intelligence efficiency
    profile, in that order -- see `derive_competency_signal()`), resolves overrides only where
    no stronger real evidence exists, then calls the unchanged, already-tested
    `decide_wait_or_assign()`."""

    best_env = derive_competency_signal(db, owner_id=owner_id, capability_key=capability_key, task_class=task_class, external_teacher=best_agent_teacher, agent_id=best_agent_id)
    candidate_env = derive_competency_signal(db, owner_id=owner_id, capability_key=capability_key, task_class=task_class, external_teacher=candidate_agent_teacher, agent_id=candidate_agent_id)

    best_resolved = _resolve_with_neutral_fallback(best_env, override=best_agent_competency_override, neutral=NEUTRAL_COMPETENCY_ESTIMATE)
    candidate_resolved = _resolve_with_neutral_fallback(candidate_env, override=candidate_agent_competency_override, neutral=NEUTRAL_COMPETENCY_ESTIMATE)

    result = decide_wait_or_assign(
        best_agent_available=best_agent_available, best_agent_eta_seconds=best_agent_eta_seconds,
        best_agent_competency=best_resolved.value, candidate_agent_available=True,
        candidate_agent_competency=candidate_resolved.value, critical_path=critical_path,
        duplication_risk=duplication_risk, branch_conflict_risk=branch_conflict_risk,
    )
    return WaitOrAssignWithProvenance(result=result, signals=WaitOrAssignSignalReport(best_resolved, candidate_resolved))


@dataclass(frozen=True)
class ContinuationSignalReport:
    current_agent_competency: ResolvedSignal
    current_agent_rework_rate: ResolvedSignal
    candidate_agent_competency: ResolvedSignal
    candidate_agent_rework_rate: ResolvedSignal
    context_loaded_relevance: ResolvedSignal


@dataclass(frozen=True)
class ContinuationWithProvenance:
    result: ContinuationResult
    signals: ContinuationSignalReport


def decide_continue_or_handoff_from_real_state(
    db: Session,
    *,
    owner_id: uuid.UUID,
    capability_key: str,
    task_class: str,
    current_agent_id: uuid.UUID,
    candidate_agent_id: uuid.UUID,
    current_agent_state: AgentState | None,
    candidate_program_id: str | None,
    handoff_cost_seconds: float,
    interruption_cost_seconds: float = 0.0,
    current_agent_competency_override: float | None = None,
    candidate_agent_competency_override: float | None = None,
) -> ContinuationWithProvenance:
    """Derives BOTH agents' competency and rework rate from real state, and derives
    `context_loaded_relevance` from the current agent's REAL `AgentState.current_program`
    (see `signal_derivation.derive_context_loaded_relevance_signal()`) rather than requiring
    the caller to hand-estimate it, then calls the unchanged, already-tested
    `decide_continue_or_handoff()`."""

    current_competency_env = derive_competency_signal(db, owner_id=owner_id, capability_key=capability_key, task_class=task_class, agent_id=current_agent_id)
    candidate_competency_env = derive_competency_signal(db, owner_id=owner_id, capability_key=capability_key, task_class=task_class, agent_id=candidate_agent_id)
    current_rework_env = derive_rework_rate_signal(db, owner_id=owner_id, agent_id=current_agent_id)
    candidate_rework_env = derive_rework_rate_signal(db, owner_id=owner_id, agent_id=candidate_agent_id)
    context_env = derive_context_loaded_relevance_signal(agent_state=current_agent_state, candidate_program_id=candidate_program_id)

    current_competency = _resolve_with_neutral_fallback(current_competency_env, override=current_agent_competency_override, neutral=NEUTRAL_COMPETENCY_ESTIMATE)
    candidate_competency = _resolve_with_neutral_fallback(candidate_competency_env, override=candidate_agent_competency_override, neutral=NEUTRAL_COMPETENCY_ESTIMATE)
    current_rework = _resolve_with_neutral_fallback(current_rework_env, override=None, neutral=NEUTRAL_REWORK_ESTIMATE)
    candidate_rework = _resolve_with_neutral_fallback(candidate_rework_env, override=None, neutral=NEUTRAL_REWORK_ESTIMATE)
    context_relevance = _resolve_with_neutral_fallback(context_env, override=None, neutral=0.1)

    result = decide_continue_or_handoff(
        current_agent_context_loaded_relevance=context_relevance.value, current_agent_competency=current_competency.value,
        current_agent_rework_rate=current_rework.value, candidate_agent_competency=candidate_competency.value,
        candidate_agent_rework_rate=candidate_rework.value, handoff_cost_seconds=handoff_cost_seconds,
        interruption_cost_seconds=interruption_cost_seconds,
    )
    return ContinuationWithProvenance(
        result=result,
        signals=ContinuationSignalReport(current_competency, current_rework, candidate_competency, candidate_rework, context_relevance),
    )
