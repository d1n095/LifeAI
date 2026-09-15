"""Derive Workforce Signals From Real System State. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md §C.

Composes real, already-owned sources -- never a second copy of any of their truth:

- `mastery_ledger.py` (this package's own durable Capability Mastery Ledger) --
  examiner-verified pass rate for a specific (capability_key, task_class, external_teacher).
- `app.capability_reality` -- the deterministic, caller-asserted `confidence`/`status` for a
  capability_key (a DIFFERENT axis, see that package's own docstring; used here only as a
  fallback signal when no mastery-ledger history exists yet).
- `app.resource_intelligence.efficiency_profile.agent_efficiency_profile()` -- real,
  recency-weighted `accepted_commit_rate`/`rework_rate` for a specific agent.
- `app.resource_intelligence.quota.provider_quota_remaining()` -- real quota-remaining
  fractions for a goal.
- `situational_snapshot`'s real `AgentState.current_program` -- whether an agent already owns
  the SAME program/goal as the candidate task (context-loaded relevance).

PROFILE != AUTHORITY. RESOURCE INTELLIGENCE != AUTHORITY. MISSING DATA != ZERO. UNKNOWN COST
!= FREE. Every function here returns a `SignalEnvelope`, never a bare float, so a caller
always knows whether a number is OBSERVED/DERIVED/ESTIMATED/CALLER_SUPPLIED/UNKNOWN."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.mainai_cognitive_ops.types import AgentState
from app.mainai_workforce.mastery_ledger import get_or_create_mastery_record
from app.mainai_workforce.types import SignalEnvelope, SignalOrigin

_SOURCE_MASTERY_LEDGER = "app.mainai_workforce.mastery_ledger"
_SOURCE_CAPABILITY_REALITY = "app.capability_reality"
_SOURCE_EFFICIENCY_PROFILE = "app.resource_intelligence.efficiency_profile"
_SOURCE_QUOTA = "app.resource_intelligence.quota"
_SOURCE_SITUATIONAL = "app.mainai_workforce.situational_snapshot"


def derive_competency_signal(
    db: Session,
    *,
    owner_id: uuid.UUID,
    capability_key: str,
    task_class: str,
    external_teacher: str | None = None,
    agent_id: uuid.UUID | None = None,
) -> SignalEnvelope:
    """Priority order (first real signal wins, never blended):

    1. This task-class's own Capability Mastery Ledger examiner pass rate -- the most directly
       relevant signal (examiner PASS/FAIL history for THIS exact task class), DERIVED.
    2. `app.capability_reality`'s own asserted `confidence` for this `capability_key` --
       DERIVED (a caller's own explicit, durable assertion, not this function's invention).
    3. `app.resource_intelligence.efficiency_profile.agent_efficiency_profile()`'s real
       `accepted_commit_rate` for `agent_id` -- OBSERVED (a direct, recency-weighted
       measurement of actual outcomes).
    4. UNKNOWN -- no real signal exists; this function never fabricates a numeric default."""

    if external_teacher is not None:
        mastery = get_or_create_mastery_record(
            db, owner_id=owner_id, capability_key=capability_key, task_class=task_class,
            external_teacher=external_teacher, idempotency_key=f"signal-lookup:{capability_key}:{task_class}:{external_teacher}",
        )
        total_examined = mastery["examiner_pass_count"] + mastery["examiner_fail_count"]
        if total_examined > 0:
            rate = mastery["examiner_pass_count"] / total_examined
            return SignalEnvelope(
                value=rate, origin=SignalOrigin.DERIVED, source=_SOURCE_MASTERY_LEDGER,
                note=f"{mastery['examiner_pass_count']}/{total_examined} examiner-verified passes for capability_key={capability_key}, task_class={task_class}",
            )

    from app.capability_reality.service import get_capability_reality

    record = get_capability_reality(db, owner_id=owner_id, capability_key=capability_key)
    if record is not None and record.confidence is not None:
        return SignalEnvelope(value=float(record.confidence), origin=SignalOrigin.DERIVED, source=_SOURCE_CAPABILITY_REALITY, note=f"capability_reality status={record.status}")

    if agent_id is not None:
        from app.resource_intelligence.efficiency_profile import agent_efficiency_profile

        profile = agent_efficiency_profile(db, owner_id=owner_id, agent_id=agent_id)
        accepted = profile["accepted_commit_rate"]
        if not accepted.missing_data and accepted.value is not None:
            return SignalEnvelope(value=float(accepted.value), origin=SignalOrigin.OBSERVED, source=_SOURCE_EFFICIENCY_PROFILE, note=accepted.method)

    return SignalEnvelope(value=None, origin=SignalOrigin.UNKNOWN, source="none", note="no mastery-ledger, capability_reality, or efficiency-profile signal available")


def derive_rework_rate_signal(db: Session, *, owner_id: uuid.UUID, agent_id: uuid.UUID | None) -> SignalEnvelope:
    if agent_id is None:
        return SignalEnvelope(value=None, origin=SignalOrigin.UNKNOWN, source="none", note="no agent_id supplied")

    from app.resource_intelligence.efficiency_profile import agent_efficiency_profile

    profile = agent_efficiency_profile(db, owner_id=owner_id, agent_id=agent_id)
    rework = profile["rework_rate"]
    if rework.missing_data or rework.value is None:
        return SignalEnvelope(value=None, origin=SignalOrigin.UNKNOWN, source=_SOURCE_EFFICIENCY_PROFILE, note=rework.method)
    return SignalEnvelope(value=float(rework.value), origin=SignalOrigin.OBSERVED, source=_SOURCE_EFFICIENCY_PROFILE, note=rework.method)


def derive_quota_uncertainty_signal(db: Session, *, owner_id: uuid.UUID, goal_id: uuid.UUID | None) -> SignalEnvelope:
    """UNKNOWN COST != FREE: a missing/absent quota authorization never reports as 0 committed
    (i.e. "unlimited") -- it reports UNKNOWN, exactly like `resource_intelligence.quota`'s own
    `unknown_metric()` doctrine."""

    if goal_id is None:
        return SignalEnvelope(value=None, origin=SignalOrigin.UNKNOWN, source="none", note="no goal_id supplied")

    from app.resource_intelligence.quota import provider_quota_remaining

    quota = provider_quota_remaining(db, owner_id=owner_id, goal_id=goal_id)
    known = [float(e.value) for e in quota.values() if not e.missing_data and e.value is not None]
    if not known:
        return SignalEnvelope(value=None, origin=SignalOrigin.UNKNOWN, source=_SOURCE_QUOTA, note="no active provider_spend_authorization or no configured ceiling")
    return SignalEnvelope(value=min(known), origin=SignalOrigin.OBSERVED, source=_SOURCE_QUOTA, note="minimum known remaining-fraction across quota dimensions")


def derive_context_loaded_relevance_signal(*, agent_state: AgentState | None, candidate_program_id: str | None) -> SignalEnvelope:
    """Pure (no `db`) -- reasons over an already-fetched real `AgentState` (see
    `situational_snapshot.py`). DERIVED, not OBSERVED: "does this agent already own the same
    program" is a real fact about real data, but the 0.9/0.1 mapping itself is a documented,
    hand-picked interpretation of that fact, not a direct measurement."""

    if agent_state is None or candidate_program_id is None:
        return SignalEnvelope(value=None, origin=SignalOrigin.UNKNOWN, source=_SOURCE_SITUATIONAL, note="no real agent state or candidate program id supplied")

    if agent_state.current_program == candidate_program_id:
        return SignalEnvelope(value=0.9, origin=SignalOrigin.DERIVED, source=_SOURCE_SITUATIONAL, note=f"agent already owns program {candidate_program_id}")

    return SignalEnvelope(value=0.1, origin=SignalOrigin.DERIVED, source=_SOURCE_SITUATIONAL, note="agent does not currently own the candidate program")


def resolve_signal(envelope: SignalEnvelope, *, override: float | None = None) -> tuple[float | None, SignalOrigin, str]:
    """Do not let an override silently replace stronger current durable evidence: an
    OBSERVED/DERIVED envelope with a real value always wins over a caller override, regardless
    of whether an override was also supplied. An override is only used when the real signal is
    UNKNOWN/ESTIMATED (i.e. there IS no stronger current durable evidence to protect)."""

    if envelope.origin in (SignalOrigin.OBSERVED, SignalOrigin.DERIVED) and envelope.value is not None:
        return envelope.value, envelope.origin, f"real {envelope.origin.value} signal from {envelope.source} ({envelope.note})"

    if override is not None:
        return override, SignalOrigin.CALLER_SUPPLIED, f"no stronger real signal available ({envelope.source}: {envelope.note}); using caller-supplied override"

    return None, SignalOrigin.UNKNOWN, f"no real signal and no override supplied ({envelope.source}: {envelope.note})"
