"""Dynamic Workforce Orchestration -- composition layer. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md.

SCHEDULER OUTPUT != EXECUTION AUTHORITY, structurally enforced: `WorkforceRecommendation.
authorized` always defaults `False` and nothing in this module ever imports a mutating
function from `app.agent_coordination`/`app.resource_intelligence`/`app.provider_spend`
(verified by this package's own AST-based structural-purity test).

Composes, never duplicates: real busy/idle/branch/SHA truth from `situational_snapshot.py`
(itself composing the real `app.agent_coordination.runtime_view`), duplication checking from
`app.mainai_cognitive_ops.duplication_control`, and the existing in-flight-attention ranking
`app.resource_intelligence.scheduler.next_best_resource_allocation()` (never reimplemented --
this module answers a DIFFERENT question: "should we wait for the best agent or assign a safe
candidate to a NOT-yet-assigned task", which that existing function does not attempt)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.mainai_cognitive_ops.duplication_control import DuplicationAssessment, assess_duplication
from app.mainai_cognitive_ops.situational_awareness import select_assignable_agents
from app.mainai_cognitive_ops.types import WorkItem
from app.mainai_workforce.situational_snapshot import real_agent_states_snapshot
from app.mainai_workforce.types import WaitOrAssignDecision
from app.mainai_workforce.wait_or_assign import WaitOrAssignResult, decide_wait_or_assign


@dataclass(frozen=True)
class WorkforceRecommendation:
    decision: WaitOrAssignDecision
    reason: str
    duplication: DuplicationAssessment | None
    assignable_agent_ids: tuple[str, ...]
    authorized: bool = False


def recommend_for_task(
    db: Session,
    *,
    owner_id: uuid.UUID,
    candidate_work_item: WorkItem,
    active_work: tuple[WorkItem, ...],
    best_agent_available: bool,
    best_agent_eta_seconds: float | None,
    best_agent_competency: float,
    candidate_agent_available: bool,
    candidate_agent_competency: float,
    critical_path: bool = False,
    is_independent_examiner_context: bool = False,
) -> WorkforceRecommendation:
    """Real assignable-agent list comes from `situational_snapshot.real_agent_states_snapshot()`
    (composing the real agent-coordination registry). Duplication check runs BEFORE the wait/
    assign decision -- an accidental duplicate blocks assignment regardless of who is free."""

    real_states = real_agent_states_snapshot(db, owner_id=owner_id)
    assignable = select_assignable_agents(real_states)

    duplication = assess_duplication(candidate=candidate_work_item, existing_work=active_work, is_independent_examiner_context=is_independent_examiner_context)
    if duplication.verdict.value == "accidental_duplicate":
        return WorkforceRecommendation(
            decision=WaitOrAssignDecision.DEFER, reason=f"duplication check blocks assignment: {duplication.reason}",
            duplication=duplication, assignable_agent_ids=tuple(a.agent_id for a in assignable),
        )

    result: WaitOrAssignResult = decide_wait_or_assign(
        best_agent_available=best_agent_available, best_agent_eta_seconds=best_agent_eta_seconds,
        best_agent_competency=best_agent_competency, candidate_agent_available=candidate_agent_available,
        candidate_agent_competency=candidate_agent_competency, critical_path=critical_path,
        duplication_risk=False, branch_conflict_risk=False,
    )
    return WorkforceRecommendation(
        decision=result.decision, reason=result.reason, duplication=duplication,
        assignable_agent_ids=tuple(a.agent_id for a in assignable),
    )
