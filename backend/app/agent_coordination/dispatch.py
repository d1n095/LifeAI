"""Bounded Dispatch Foundation -- turns a routing DECISION (`app.agent_coordination.routing`:
"agent X should do assignment Y next") into a real, auditable dispatch, without granting any
authority the coordination layer did not already, explicitly grant.

Three things live here, all pure REUSE of what migration 0046 / PR #82 / PR #83 already built
-- never a second task/job queue, never a second approval system, never a second agent
registry:

1. DISPATCH LIFECYCLE (`DISPATCH_LIFECYCLE` below) -- a named-alias mapping onto the ALREADY
   EXISTING `WorkAssignmentStatus`, never a second status column or enum. "AUTHORIZED" has no
   status of its own: it is exactly "ready AND passes evaluate_dispatch_readiness()'s approval
   check," computed at read time, never stored -- matching this codebase's "derived, never
   stored" doctrine everywhere else (see e.g. `app.agent_coordination.runtime_view`'s own
   block-reason handling).

2. THE FAIL-CLOSED GATE (`evaluate_dispatch_readiness()`) -- every check the founder's own
   spec requires, layered strictly ON TOP of `evaluate_assignment_readiness()` (agent
   availability, authority/base-sha staleness, dependency satisfaction, duplicate work,
   path/worktree conflict, lease requirement), never duplicating any of it. Unlike
   `next_feasible_assignment_for_agent()`'s own selection-time tolerance,
   `LEASE_REQUIRED` is NOT accepted here: a caller reaching this gate is about to actually
   invoke a real external agent and MUST already hold the write lease. On top of that:
   capability match, an explicit branch+worktree for any `read_write` dispatch, and founder
   approval -- delegated entirely to `app.mainai_execution.approval.require_task_approval()`,
   the REAL gate, never reimplemented here.

3. THE RESULT HANDOFF (`DispatchResult`/`apply_dispatch_result()`) -- a structured envelope a
   completed/failed dispatch hands back, recorded through the EXISTING
   `record_assignment_execution()`/`record_assignment_outcome()`/`build_agent_outcome_payload()`
   primitives PR #83 already built, and transitioned through the EXISTING state machine
   (`transition_status()`) -- never a raw status write, never a second evidence store, never a
   fabricated quality score.

`dispatch_assignment()` is the actual `dispatch(agent_id, assignment_id, authority_envelope)`
control-plane entry point the founder's spec asked for -- see that function's own docstring for
how it enforces the authority boundary against whatever `app.agent_coordination.adapters`
implementation is handed to it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.agent_coordination.service import (
    OUTCOME_ASSIGNABLE,
    build_agent_outcome_payload,
    evaluate_assignment_readiness,
    record_assignment_execution,
    record_assignment_outcome,
    transition_status,
)
from app.mainai_execution.approval import ApprovalRequiredError, require_task_approval
from app.models.agent_coordination import AgentWorkAssignment, CoordinationAgent, WorkAssignmentReadWriteMode, WorkAssignmentStatus
from app.models.mainai_execution import MainAIGoal, MainAITask

if TYPE_CHECKING:
    from app.agent_coordination.adapters import AgentAdapter

# ============================================================================ 1: dispatch
# lifecycle -- named aliases onto the ALREADY EXISTING WorkAssignmentStatus. Never a second
# status column. "AUTHORIZED" is deliberately absent as a distinct VALUE (only as a concept in
# this comment) -- it is a DERIVED fact (evaluate_dispatch_readiness() returning ASSIGNABLE
# with its approval check passed), not a stored one; see module docstring.
DISPATCH_LIFECYCLE: dict[str, WorkAssignmentStatus] = {
    "PROPOSED": WorkAssignmentStatus.planned,
    "READY": WorkAssignmentStatus.ready,
    "DISPATCHING": WorkAssignmentStatus.waiting_agent,
    "RUNNING": WorkAssignmentStatus.running,
    "COMPLETED": WorkAssignmentStatus.completed,
    "FAILED": WorkAssignmentStatus.failed,
    "CANCELLED": WorkAssignmentStatus.cancelled,
    "BLOCKED": WorkAssignmentStatus.blocked,
}


# ============================================================================ 2: the fail-
# closed dispatch gate

OUTCOME_CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
OUTCOME_APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
OUTCOME_SCOPE_NOT_EXPLICIT = "SCOPE_NOT_EXPLICIT"
OUTCOME_REAL_PROVIDER_NOT_CONFIGURED = "REAL_PROVIDER_NOT_CONFIGURED"


@dataclass(frozen=True)
class DispatchDecision:
    outcome: str
    reason: str


def evaluate_dispatch_readiness(
    db: Session,
    *,
    assignment: AgentWorkAssignment,
    agent: CoordinationAgent,
    required_capabilities: tuple[str, ...] = (),
    current_base_sha: str | None = None,
) -> DispatchDecision:
    """The FAIL-CLOSED gate a caller must pass immediately before actually invoking an
    adapter -- `dispatch_assignment()` below always calls this first and never skips it. Every
    check is a hard stop: any failure means "do not dispatch," never a warning to log and
    proceed past -- the same doctrine `app.mainai_execution.approval`'s own module docstring
    establishes for its own gate. Checked in this fixed order:

    1. `evaluate_assignment_readiness()` -- agent availability, authority/base-sha staleness,
       dependency satisfaction, duplicate work, path/worktree conflict, lease requirement.
       Only a plain `ASSIGNABLE` passes; `LEASE_REQUIRED` does NOT (a dispatch is about to
       actually invoke a real agent and must already hold its write lease -- contrast with
       `next_feasible_assignment_for_agent()`'s own selection-time tolerance for that outcome).
    2. Capability match -- `required_capabilities` must be a subset of the agent's own
       declared `capabilities`.
    3. Explicit scope for writes -- a `read_write` dispatch requires BOTH `branch` and
       `worktree_path` already set on the assignment; neither may be inferred or left implicit.
    4. Founder approval -- delegated entirely to
       `app.mainai_execution.approval.require_task_approval()` when the assignment has a
       linked `task_id`. When it does NOT (an assignment ahead of a formal `MainAITask`) but
       `assignment.approval_required` is still set, there is no real gate to check against yet
       -- fails closed rather than silently treating "nothing to check" as "approved.\""""

    base_decision = evaluate_assignment_readiness(db, assignment=assignment, current_base_sha=current_base_sha)
    if base_decision.outcome != OUTCOME_ASSIGNABLE:
        return DispatchDecision(base_decision.outcome, base_decision.reason)

    required = set(required_capabilities)
    if required and not required.issubset(set(agent.capabilities)):
        missing = sorted(required - set(agent.capabilities))
        return DispatchDecision(OUTCOME_CAPABILITY_MISMATCH, f"agent '{agent.agent_key}' is missing required capabilities: {missing}")

    if assignment.read_write_mode == WorkAssignmentReadWriteMode.read_write and not (assignment.branch and assignment.worktree_path):
        return DispatchDecision(
            OUTCOME_SCOPE_NOT_EXPLICIT, "a read_write dispatch requires an explicit branch AND worktree_path -- neither may be inferred"
        )

    if assignment.task_id is not None:
        task = db.get(MainAITask, assignment.task_id)
        goal = db.get(MainAIGoal, assignment.goal_id)
        if task is None or goal is None:
            # task_id is set but the row(s) it points at are gone -- MainAITask.owner_id's
            # ondelete="SET NULL" means this SHOULDN'T be reachable in steady state (a deleted
            # task nulls task_id first), but if that invariant is ever broken elsewhere, this
            # must fail closed, never silently fall through as if there were nothing to check.
            return DispatchDecision(
                OUTCOME_APPROVAL_REQUIRED, f"assignment.task_id={assignment.task_id} does not resolve to a real task/goal -- fails closed"
            )
        try:
            require_task_approval(db, task=task, goal_approval_policy=goal.approval_policy)
        except ApprovalRequiredError:
            return DispatchDecision(OUTCOME_APPROVAL_REQUIRED, f"task {task.id} requires founder approval before dispatch")
    elif assignment.approval_required:
        return DispatchDecision(
            OUTCOME_APPROVAL_REQUIRED,
            "assignment.approval_required is set but has no linked task_id to check the real approval gate against -- fails closed",
        )

    return DispatchDecision(OUTCOME_ASSIGNABLE, "passes every dispatch-gate check")


# ============================================================================ dispatch(agent_id,
# assignment_id, authority_envelope) -- the control-plane entry point

async def dispatch_assignment(
    db: Session,
    *,
    assignment: AgentWorkAssignment,
    agent: CoordinationAgent,
    adapter: "AgentAdapter",
    authority_envelope: dict | None = None,
    required_capabilities: tuple[str, ...] = (),
) -> DispatchDecision:
    """`dispatch(agent_id, assignment_id, authority_envelope)` -- the canonical control-plane
    entry point. ALWAYS re-runs `evaluate_dispatch_readiness()` immediately before touching the
    adapter (never trusts a decision computed earlier by a different call) -- on failure,
    returns that decision WITHOUT calling the adapter or mutating the assignment at all.

    `authority_envelope`, if supplied, is validated to be a SUBSET of the assignment's own
    already-narrowed `allowed_paths` -- a caller can never be handed more authority than the
    coordination layer itself already granted through `bind_execution_context`-style narrowing
    upstream of this call. Today the envelope can only REJECT a dispatch (widening is never
    granted), never itself grant anything downstream -- `start_assignment(assignment_id)`
    intentionally receives only the assignment's own id, never the envelope, since a REAL
    Agent Runtime implementation must derive its own working authority from the assignment row
    itself (its `allowed_paths`/`branch`/`worktree_path`, already the source of truth), not
    from a caller-supplied dict a future adapter could otherwise be tempted to trust as-is.

    Transitions `ready -> waiting_agent` ("DISPATCHING") BEFORE calling the adapter, and only
    to `running` AFTER `adapter.start_assignment()` returns without raising -- so an assignment
    that failed to actually start is never left looking like it is running. On
    `ProviderNotConfiguredError` (see `app.agent_coordination.adapters`), transitions to
    `blocked` with a structured `REAL_PROVIDER_NOT_CONFIGURED` reason and returns that outcome.
    On ANY OTHER exception from the adapter (a genuine bug in a real implementation, not the
    documented not-configured signal), also transitions to `blocked` -- with a generic
    `adapter_start_assignment_failed` reason -- before RE-RAISING the original exception, so
    the caller still sees the real error, but the assignment is never left stuck in
    `waiting_agent` indefinitely. `waiting_agent` maps to `RuntimeStatus.IDLE` in
    `app.agent_coordination.runtime_view` -- a crashed dispatch left in that status would
    silently read as "idle," never as "broken," which is exactly what this branch prevents.
    NEVER silently reports success, never fabricates a running state for a dispatch that never
    actually reached a real external agent."""

    from app.agent_coordination.adapters import ProviderNotConfiguredError

    decision = evaluate_dispatch_readiness(db, assignment=assignment, agent=agent, required_capabilities=required_capabilities)
    if decision.outcome != OUTCOME_ASSIGNABLE:
        return decision

    if authority_envelope is not None:
        envelope_paths = set(authority_envelope.get("allowed_paths") or ())
        if envelope_paths and not envelope_paths.issubset(set(assignment.allowed_paths)):
            escaping = sorted(envelope_paths - set(assignment.allowed_paths))
            return DispatchDecision(
                OUTCOME_SCOPE_NOT_EXPLICIT, f"authority_envelope requests paths outside the assignment's own allowed_paths: {escaping}"
            )

    transition_status(db, assignment=assignment, new_status="waiting_agent", detail={"reason": "dispatching"})
    try:
        await adapter.start_assignment(assignment.id)
    except ProviderNotConfiguredError as exc:
        transition_status(db, assignment=assignment, new_status="blocked", detail={"reason": OUTCOME_REAL_PROVIDER_NOT_CONFIGURED, "detail": str(exc)})
        return DispatchDecision(OUTCOME_REAL_PROVIDER_NOT_CONFIGURED, str(exc))
    except Exception as exc:
        transition_status(db, assignment=assignment, new_status="blocked", detail={"reason": "adapter_start_assignment_failed", "detail": str(exc)})
        raise

    transition_status(db, assignment=assignment, new_status="running")
    return DispatchDecision(OUTCOME_ASSIGNABLE, "dispatched -- adapter confirmed start_assignment")


# ============================================================================ 3: result
# handoff -- structured evidence, never a fabricated quality score.

@dataclass(frozen=True)
class DispatchResult:
    """What a completed/failed dispatched worker hands back -- observed facts and pointers
    only, recorded VERBATIM, never interpreted into a rating. See
    `app.agent_coordination.service.build_agent_outcome_payload`'s own "never a fabricated
    placeholder for what it did not observe" doctrine, reused here."""

    succeeded: bool
    base_sha: str | None = None
    head_sha: str | None = None
    branch: str | None = None
    worktree_path: str | None = None
    changed_paths: tuple[str, ...] = ()
    tests_passed: bool | None = None
    tests_run: int | None = None
    tests_failed: int | None = None
    ci_conclusion: str | None = None
    ci_refs: tuple[str, ...] = ()
    pr_ref: str | None = None
    failure_reason: str | None = None
    block_reason: str | None = None
    duration_seconds: float | None = None
    cost_tokens: int | None = None
    cost_usd: float | None = None


def apply_dispatch_result(db: Session, *, assignment: AgentWorkAssignment, agent: CoordinationAgent, result: DispatchResult) -> None:
    """The ONE place a dispatched worker's result is handed back into durable state. Updates
    the assignment's own pointer fields (`head_sha`, `output_refs`, `upstream_pr_ref`) only
    when the caller actually observed a value -- never overwrites with a guess. Records
    evidence through the EXISTING `record_assignment_execution()`/`record_assignment_outcome()`
    (idempotent by construction -- calling this twice for the same assignment does not
    duplicate the underlying `IntelligenceExecution` row), never a second evidence store.
    Transitions status through the EXISTING state machine (`transition_status()`) -- never a
    raw status assignment; a caller invoking this from a non-`running` state gets
    `InvalidTransitionError`, fail-closed by construction, not silently accepted."""

    if result.head_sha is not None:
        assignment.head_sha = result.head_sha
    if result.pr_ref is not None:
        assignment.upstream_pr_ref = result.pr_ref
    if result.changed_paths:
        assignment.output_refs = [*assignment.output_refs, {"kind": "changed_paths", "paths": list(result.changed_paths)}]
    if result.ci_refs:
        assignment.output_refs = [*assignment.output_refs, {"kind": "ci_refs", "refs": list(result.ci_refs)}]
    db.flush()

    if assignment.intelligence_execution_id is None:
        record_assignment_execution(db, assignment=assignment, agent=agent)
    payload = build_agent_outcome_payload(
        tests_passed=result.tests_passed,
        tests_run=result.tests_run,
        tests_failed=result.tests_failed,
        duration_seconds=result.duration_seconds,
        cost_tokens=result.cost_tokens,
        cost_usd=result.cost_usd,
        ci_conclusion=result.ci_conclusion,
        failure_reason=result.failure_reason,
        merge_result="merged" if (result.succeeded and result.pr_ref) else None,
    )
    record_assignment_outcome(db, assignment=assignment, evidence_kind="dispatch_result", payload=payload, deterministic=True)

    if result.succeeded:
        transition_status(db, assignment=assignment, new_status="completed")
    elif result.block_reason is not None:
        transition_status(db, assignment=assignment, new_status="blocked", detail={"reason": result.block_reason})
    else:
        transition_status(db, assignment=assignment, new_status="failed", detail={"reason": result.failure_reason or "unspecified failure"})
