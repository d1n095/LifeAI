"""Agent conversation turns -- MainAI issues an instruction to a NEW agent session, collects
the agent's actual answer, and continues that SAME session with a follow-up instruction.

Pure composition over what already exists, never a second dispatcher or store:
- `dispatch.dispatch_assignment()` -- the fail-closed gate (readiness, lease, capability,
  explicit scope, adapter enablement, Founder approval) and the start of the first turn;
- `execution_control` -- one `AgentDispatchExecution` tracking row per turn (its documented
  opaque `process_ref` records the MainAI-bound session id for audit) and the instruction
  audit event (append-only -- also the turn counter);
- `dispatch.collect_dispatch_result()` -- result ingestion, appending the agent's bounded
  final message (and an audit record of the bound session) to `AgentWorkAssignment.output_refs`;
- the EXISTING review loop of `WorkAssignmentStatus`: an answered turn moves
  `running -> waiting_review`; a follow-up moves `waiting_review -> reviewing ->
  changes_requested -> running`; `close_agent_session()` ends it. No new status, no migration.

Session authority: MainAI derives the session id itself, from the server secret
(`adapters.mainai_session_id` over owner + assignment + provider), and binds it BEFORE the
session starts. Continuation re-derives the same id. NOTHING stored is ever read back as a
session id -- not `output_refs`, not `process_ref`, not the agent's own report (that is only
compared) -- so forged or poisoned ids have no effect, a worker restart resumes correctly, and a
Founder-owned session can never be selected. The CLI additionally runs with an isolated HOME
(`adapter_config.isolated_agent_env`), so Founder sessions are not even in its session store.
Providers whose session semantics are unverified (Cursor Agent, Codex) are single-shot only.

The agent's final message is its self-report: recorded verbatim (bounded, redacted), never
treated as verified repository truth."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_coordination.adapter_config import adapter_availability, adapter_max_turns
from app.agent_coordination.adapters import (
    AdapterInstructionError,
    AdapterProcessLostError,
    AdapterSessionUnknownError,
    AgentAdapter,
    build_prompt,
    get_real_adapter,
)
from app.agent_coordination.dispatch import OUTCOME_INSTRUCTION_REJECTED, dispatch_assignment
from app.agent_coordination.execution_control import (
    OUTCOME_OK,
    ExecutionEvent,
    collect_and_ingest_execution_result,
    mark_execution_lost,
    mark_execution_running,
    record_execution_event,
    send_execution_instruction,
    start_execution_tracking,
)
from app.agent_coordination.service import OUTCOME_ASSIGNABLE, transition_status
from app.mainai_execution.approval import ApprovalRequiredError, require_task_approval
from app.models.agent_coordination import (
    AgentDispatchExecution,
    AgentWorkAssignment,
    AgentWorkAssignmentEvent,
    AgentWorkAssignmentEventType,
    CoordinationAgent,
    WorkAssignmentReadWriteMode,
    WorkAssignmentStatus,
)
from app.models.mainai_execution import MainAIGoal, MainAITask

OUTCOME_SESSION_EXISTS = "SESSION_EXISTS"
OUTCOME_SESSION_UNKNOWN = "SESSION_UNKNOWN"
OUTCOME_NOT_AWAITING_INSTRUCTION = "NOT_AWAITING_INSTRUCTION"
OUTCOME_UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
OUTCOME_ADAPTER_DISABLED = "ADAPTER_DISABLED"
OUTCOME_APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
OUTCOME_TURN_FAILED = "TURN_FAILED"
OUTCOME_TURN_LIMIT = "TURN_LIMIT_REACHED"

_AUDIT_INSTRUCTION_CHARS = 4_000
_AUDIT_SUMMARY_PREFIX = "instruction delivered"


class AgentSessionError(RuntimeError):
    """A turn that could not be run for the Path A `agent_assignment` handler -- carries the
    structured outcome so the caller can surface it, never a fabricated success."""

    def __init__(self, outcome: str, detail: str):
        super().__init__(f"{outcome}: {detail}")
        self.outcome = outcome
        self.detail = detail


@dataclass(frozen=True)
class TurnResult:
    outcome: str
    detail: str
    assignment_status: str
    attempt_id: uuid.UUID | None = None
    session_id: str | None = None
    final_message: str | None = None


def _latest_ref(assignment: AgentWorkAssignment, kind: str, provider_key: str) -> dict | None:
    for ref in reversed(list(assignment.output_refs or [])):
        if isinstance(ref, dict) and ref.get("kind") == kind and ref.get("provider") == provider_key:
            return ref
    return None


def delivered_turns(db: Session, assignment: AgentWorkAssignment) -> int:
    """Instructions already delivered for this assignment, counted from the append-only
    instruction audit events (the runtime role cannot UPDATE or DELETE them) -- never from the
    mutable tracking rows or `output_refs`."""

    rows = db.execute(
        select(AgentWorkAssignmentEvent.detail).where(
            AgentWorkAssignmentEvent.assignment_id == assignment.id,
            AgentWorkAssignmentEvent.owner_id == assignment.owner_id,
            AgentWorkAssignmentEvent.event_type == AgentWorkAssignmentEventType.execution_observed,
        )
    ).scalars().all()
    return sum(1 for detail in rows if isinstance(detail, dict) and str(detail.get("summary", "")).startswith(_AUDIT_SUMMARY_PREFIX))


def _turn_limit_refusal(db: Session, assignment: AgentWorkAssignment, provider_key: str) -> tuple[str, str] | None:
    limit = adapter_max_turns(provider_key)
    if limit is None:
        return OUTCOME_TURN_LIMIT, "turn limit is misconfigured -- fails closed"
    used = delivered_turns(db, assignment)
    if used >= limit:
        return OUTCOME_TURN_LIMIT, f"{used} of {limit} instruction turns already used for this assignment"
    return None


def _bind_mainai_session(adapter: AgentAdapter, assignment: AgentWorkAssignment) -> str | None:
    bind = getattr(adapter, "bind_mainai_session", None)
    return bind(assignment.id, assignment.owner_id) if bind is not None else None


def _turn(assignment: AgentWorkAssignment, outcome: str, detail: str, **extra) -> TurnResult:
    return TurnResult(outcome=outcome, detail=detail, assignment_status=assignment.status.value, **extra)


def _audit_instruction(db: Session, *, execution: AgentDispatchExecution, assignment: AgentWorkAssignment, instruction: str, kind: str) -> None:
    record_execution_event(
        db, execution=execution, assignment=assignment,
        event=ExecutionEvent(
            kind="status", summary=f"{_AUDIT_SUMMARY_PREFIX} ({kind})",
            detail={
                "instruction_sha256": hashlib.sha256(instruction.encode()).hexdigest(),
                "instruction_excerpt": instruction[:_AUDIT_INSTRUCTION_CHARS],
                "instruction_chars": len(instruction),
            },
        ),
    )


async def _collect_turn(
    db: Session, *, execution: AgentDispatchExecution, assignment: AgentWorkAssignment, agent: CoordinationAgent, adapter: AgentAdapter, adapter_key: str
) -> TurnResult:
    decision = await collect_and_ingest_execution_result(
        db, execution=execution, assignment=assignment, agent=agent, adapter=adapter, adapter_key=adapter_key, on_success="waiting_review"
    )
    # Reporting only: the bound session confirmed by THIS attempt's result (never used as authority).
    attempt = str(execution.attempt_id)
    session_ref = _latest_ref(assignment, "agent_session", adapter_key)
    session_id = session_ref.get("session_id") if session_ref and session_ref.get("attempt_id") == attempt else None
    message_ref = _latest_ref(assignment, "agent_final_message", adapter_key)
    message = message_ref.get("text") if message_ref and message_ref.get("attempt_id") == attempt else None
    record_execution_event(
        db, execution=execution, assignment=assignment,
        event=ExecutionEvent(kind="final_result", summary=decision.reason, detail={"session_id": session_id, "answer_chars": len(message or "")}),
    )
    if assignment.status != WorkAssignmentStatus.waiting_review:
        return _turn(assignment, OUTCOME_TURN_FAILED if decision.outcome == OUTCOME_ASSIGNABLE else decision.outcome, decision.reason,
                     attempt_id=execution.attempt_id, session_id=session_id, final_message=message)
    return _turn(assignment, OUTCOME_ASSIGNABLE, "agent answered; awaiting review", attempt_id=execution.attempt_id, session_id=session_id, final_message=message)


async def start_agent_session(
    db: Session, *, assignment: AgentWorkAssignment, agent: CoordinationAgent, adapter: AgentAdapter, instruction: str,
    adapter_key: str | None = None, require_adapter_enabled: bool = True,
) -> TurnResult:
    """Turn 1: bind MainAI's own session id, deliver `instruction` to a NEW session through the
    full dispatch gate, then collect the answer. One session per assignment: once any
    instruction has been delivered (append-only audit), the assignment can only be continued
    with `continue_agent_session()`, never restarted."""

    key = adapter_key or agent.agent_key
    try:
        build_prompt(assignment.id, instruction)
    except AdapterInstructionError as exc:
        return _turn(assignment, OUTCOME_INSTRUCTION_REJECTED, str(exc))
    if delivered_turns(db, assignment) > 0:
        return _turn(assignment, OUTCOME_SESSION_EXISTS, "assignment already has an agent session; continue it instead")
    refusal = _turn_limit_refusal(db, assignment, key)
    if refusal is not None:
        return _turn(assignment, *refusal)
    try:
        session_id = _bind_mainai_session(adapter, assignment)
    except AdapterInstructionError as exc:
        return _turn(assignment, OUTCOME_INSTRUCTION_REJECTED, str(exc))

    decision = await dispatch_assignment(
        db, assignment=assignment, agent=agent, adapter=adapter, require_adapter_enabled=require_adapter_enabled, instruction=instruction
    )
    if decision.outcome != OUTCOME_ASSIGNABLE:
        return _turn(assignment, decision.outcome, decision.reason, attempt_id=decision.attempt_id)

    execution = start_execution_tracking(db, assignment=assignment, adapter_key=key, attempt_id=decision.attempt_id)
    mark_execution_running(db, execution=execution, process_ref=session_id)
    _audit_instruction(db, execution=execution, assignment=assignment, instruction=instruction, kind="new session")
    return await _collect_turn(db, execution=execution, assignment=assignment, agent=agent, adapter=adapter, adapter_key=key)


def _continuation_gate(db: Session, *, assignment: AgentWorkAssignment, agent: CoordinationAgent, require_adapter_enabled: bool) -> tuple[str, str] | None:
    """Authority is re-checked for every follow-up -- a session that was authorized once is not
    authorized forever: the adapter must still be enabled and the linked task still approved."""

    if require_adapter_enabled:
        availability = adapter_availability(agent.agent_key)
        if not availability.enabled or not availability.executable_found:
            return OUTCOME_ADAPTER_DISABLED, availability.reason
    if assignment.task_id is not None:
        task = db.get(MainAITask, assignment.task_id)
        goal = db.get(MainAIGoal, assignment.goal_id)
        if task is None or goal is None:
            return OUTCOME_APPROVAL_REQUIRED, "linked task/goal no longer resolves -- fails closed"
        try:
            require_task_approval(db, task=task, goal_approval_policy=goal.approval_policy)
        except ApprovalRequiredError:
            return OUTCOME_APPROVAL_REQUIRED, f"task {task.id} requires founder approval"
    elif assignment.approval_required:
        return OUTCOME_APPROVAL_REQUIRED, "approval_required is set with no linked task to check -- fails closed"
    return None


async def continue_agent_session(
    db: Session, *, assignment: AgentWorkAssignment, agent: CoordinationAgent, adapter: AgentAdapter, instruction: str,
    adapter_key: str | None = None, require_adapter_enabled: bool = True,
) -> TurnResult:
    """Turn N: deliver a follow-up `instruction` to the SAME session -- resumed ONLY by the id
    MainAI re-derives for this assignment, never one read from storage -- then collect the
    answer."""

    key = adapter_key or agent.agent_key
    if assignment.status != WorkAssignmentStatus.waiting_review:
        return _turn(assignment, OUTCOME_NOT_AWAITING_INSTRUCTION, f"assignment is {assignment.status.value}, not waiting_review")
    if not adapter.control_capabilities().supports_instruction:
        return _turn(assignment, OUTCOME_UNSUPPORTED_CAPABILITY, "adapter has no verified MainAI-owned session resume")
    if delivered_turns(db, assignment) == 0:
        return _turn(assignment, OUTCOME_SESSION_UNKNOWN, "no instruction was ever delivered for this assignment")
    try:
        build_prompt(assignment.id, instruction)
    except AdapterInstructionError as exc:
        return _turn(assignment, OUTCOME_INSTRUCTION_REJECTED, str(exc))
    refusal = _turn_limit_refusal(db, assignment, key) or _continuation_gate(
        db, assignment=assignment, agent=agent, require_adapter_enabled=require_adapter_enabled
    )
    if refusal is not None:
        return _turn(assignment, *refusal)
    try:
        session_id = _bind_mainai_session(adapter, assignment)
    except AdapterInstructionError as exc:
        return _turn(assignment, OUTCOME_INSTRUCTION_REJECTED, str(exc))
    if session_id is None:
        return _turn(assignment, OUTCOME_SESSION_UNKNOWN, "adapter cannot bind a MainAI-owned session")

    attempt_id = uuid.uuid4()
    transition_status(db, assignment=assignment, new_status="reviewing", detail={"reason": "agent_answer_reviewed", "attempt_id": str(attempt_id)})
    transition_status(db, assignment=assignment, new_status="changes_requested", detail={"reason": "follow_up_instruction", "attempt_id": str(attempt_id)})
    transition_status(db, assignment=assignment, new_status="running", detail={"reason": "follow_up_delivered", "attempt_id": str(attempt_id)})
    execution = start_execution_tracking(db, assignment=assignment, adapter_key=key, attempt_id=attempt_id)
    execution.process_ref = session_id
    db.flush()

    try:
        outcome = await send_execution_instruction(db, execution=execution, assignment=assignment, adapter=adapter, instruction=instruction)
    except (AdapterInstructionError, AdapterSessionUnknownError, AdapterProcessLostError) as exc:
        mark_execution_lost(db, execution=execution)
        transition_status(db, assignment=assignment, new_status="blocked", detail={"reason": type(exc).__name__, "detail": str(exc), "attempt_id": str(attempt_id)})
        return _turn(assignment, OUTCOME_TURN_FAILED, str(exc), attempt_id=attempt_id, session_id=session_id)
    if outcome.outcome != OUTCOME_OK:
        mark_execution_lost(db, execution=execution)
        transition_status(db, assignment=assignment, new_status="blocked", detail={"reason": outcome.outcome, "attempt_id": str(attempt_id)})
        return _turn(assignment, outcome.outcome, outcome.detail, attempt_id=attempt_id, session_id=session_id)

    mark_execution_running(db, execution=execution, process_ref=session_id)
    _audit_instruction(db, execution=execution, assignment=assignment, instruction=instruction, kind="follow-up")
    return await _collect_turn(db, execution=execution, assignment=assignment, agent=agent, adapter=adapter, adapter_key=key)


def close_agent_session(db: Session, *, assignment: AgentWorkAssignment, accepted: bool, reason: str) -> None:
    """Ends a conversation after review: `waiting_review -> reviewing -> completed` when the
    answer is accepted, `-> cancelled` otherwise. Never resumes the agent."""

    transition_status(db, assignment=assignment, new_status="reviewing", detail={"reason": reason})
    transition_status(db, assignment=assignment, new_status="completed" if accepted else "cancelled", detail={"reason": reason})


async def run_agent_assignment_task(db: Session, task: MainAITask) -> dict:
    """The Path A `agent_assignment` task handler: delivers `task.description` (the Founder-
    approved instruction) to a NEW session of the agent named by the ONE ready assignment
    already linked to this task, and returns the agent's answer as the task's work result.

    It never creates an assignment, never chooses an agent, and never continues an existing
    session -- those remain explicit, separately authorized decisions. `agent_assignment` is not
    in any approval policy, so `approval.requires_approval()` fails closed: every such task needs
    explicit Founder approval before dispatch."""

    assignments = db.execute(
        select(AgentWorkAssignment).where(
            AgentWorkAssignment.task_id == task.id,
            AgentWorkAssignment.owner_id == task.owner_id,
            AgentWorkAssignment.status == WorkAssignmentStatus.ready,
        )
    ).scalars().all()
    if len(assignments) != 1:
        raise AgentSessionError("NO_UNIQUE_READY_ASSIGNMENT", f"task {task.id} has {len(assignments)} ready agent assignments; exactly one is required")
    assignment = assignments[0]
    agent = db.get(CoordinationAgent, assignment.agent_id)
    if agent is None:
        raise AgentSessionError("AGENT_MISSING", f"assignment {assignment.id} references no registered agent")
    if not assignment.worktree_path:
        raise AgentSessionError("SCOPE_NOT_EXPLICIT", f"assignment {assignment.id} has no worktree_path for the agent to work in")

    adapter = get_real_adapter(
        agent.agent_key, cwd=assignment.worktree_path, read_only=assignment.read_write_mode == WorkAssignmentReadWriteMode.read_only
    )
    turn = await start_agent_session(db, assignment=assignment, agent=agent, adapter=adapter, instruction=task.description, adapter_key=agent.agent_key)
    if turn.outcome != OUTCOME_ASSIGNABLE:
        raise AgentSessionError(turn.outcome, turn.detail)
    return {
        "assignment_id": str(assignment.id),
        "agent_key": agent.agent_key,
        "attempt_id": str(turn.attempt_id),
        "session_id": turn.session_id,
        "agent_final_message": turn.final_message,
        "agent_final_message_is_self_report": True,
        "assignment_status": turn.assignment_status,
    }
