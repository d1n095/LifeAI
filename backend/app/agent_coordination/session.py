"""Agent conversation turns -- MainAI issues an instruction to a NEW agent session, collects
the agent's actual answer, and continues that SAME session with a follow-up instruction.

Pure composition over what already exists, never a second dispatcher or store:
- `dispatch.dispatch_assignment()` -- the fail-closed gate (readiness, lease, capability,
  explicit scope, adapter enablement, Founder approval) and the start of the first turn;
- `execution_control` -- one `AgentDispatchExecution` tracking row per turn (its documented
  opaque `process_ref` holds the agent's session id) and the instruction audit event;
- `dispatch.collect_dispatch_result()` -- result ingestion, appending the agent's bounded
  final message and session id to `AgentWorkAssignment.output_refs`;
- the EXISTING review loop of `WorkAssignmentStatus`: an answered turn moves
  `running -> waiting_review`; a follow-up moves `waiting_review -> reviewing ->
  changes_requested -> running`; `close_agent_session()` ends it. No new status, no migration.

Session safety: a session is only ever continued by its explicit id, and that id is only ever
read from THIS assignment's own `agent_session` output ref -- produced by this adapter's own
earlier turn. Nothing here accepts a caller-supplied session id, and nothing uses
`--continue`/`--last`, so a Founder-owned agent session can never be resumed by MainAI.

The agent's final message is its self-report: recorded verbatim (bounded, redacted), never
treated as verified repository truth."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_coordination.adapter_config import adapter_availability
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

_AUDIT_INSTRUCTION_CHARS = 4_000


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


def recorded_session_id(assignment: AgentWorkAssignment, provider_key: str) -> str | None:
    """The session this assignment's own earlier turn produced -- the ONLY session id this
    module will ever continue."""

    ref = _latest_ref(assignment, "agent_session", provider_key)
    return ref.get("session_id") if ref else None


def _turn(assignment: AgentWorkAssignment, outcome: str, detail: str, **extra) -> TurnResult:
    return TurnResult(outcome=outcome, detail=detail, assignment_status=assignment.status.value, **extra)


def _audit_instruction(db: Session, *, execution: AgentDispatchExecution, assignment: AgentWorkAssignment, instruction: str, kind: str) -> None:
    record_execution_event(
        db, execution=execution, assignment=assignment,
        event=ExecutionEvent(
            kind="status", summary=f"instruction delivered ({kind})",
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
    session_id = recorded_session_id(assignment, adapter_key)
    if session_id is not None:
        execution.process_ref = session_id
        db.flush()
    message_ref = _latest_ref(assignment, "agent_final_message", adapter_key)
    message = message_ref.get("text") if message_ref and message_ref.get("attempt_id") == str(execution.attempt_id) else None
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
    """Turn 1: deliver `instruction` to a NEW session through the full dispatch gate, then
    collect the answer. One session per assignment: an assignment that already recorded a
    session must be continued with `continue_agent_session()`, never restarted silently."""

    key = adapter_key or agent.agent_key
    try:
        build_prompt(assignment.id, instruction)
    except AdapterInstructionError as exc:
        return _turn(assignment, OUTCOME_INSTRUCTION_REJECTED, str(exc))
    if recorded_session_id(assignment, key) is not None:
        return _turn(assignment, OUTCOME_SESSION_EXISTS, "assignment already has an agent session; continue it instead")

    decision = await dispatch_assignment(
        db, assignment=assignment, agent=agent, adapter=adapter, require_adapter_enabled=require_adapter_enabled, instruction=instruction
    )
    if decision.outcome != OUTCOME_ASSIGNABLE:
        return _turn(assignment, decision.outcome, decision.reason, attempt_id=decision.attempt_id)

    execution = start_execution_tracking(db, assignment=assignment, adapter_key=key, attempt_id=decision.attempt_id)
    mark_execution_running(db, execution=execution)
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
    """Turn N: deliver a follow-up `instruction` to the SAME session (resume by the session id
    this assignment itself recorded), then collect the answer."""

    key = adapter_key or agent.agent_key
    if assignment.status != WorkAssignmentStatus.waiting_review:
        return _turn(assignment, OUTCOME_NOT_AWAITING_INSTRUCTION, f"assignment is {assignment.status.value}, not waiting_review")
    session_id = recorded_session_id(assignment, key)
    if session_id is None:
        return _turn(assignment, OUTCOME_SESSION_UNKNOWN, "assignment has no recorded agent session to continue")
    try:
        build_prompt(assignment.id, instruction)
    except AdapterInstructionError as exc:
        return _turn(assignment, OUTCOME_INSTRUCTION_REJECTED, str(exc))
    if not adapter.control_capabilities().supports_instruction:
        return _turn(assignment, OUTCOME_UNSUPPORTED_CAPABILITY, "adapter does not declare supports_instruction")
    refusal = _continuation_gate(db, assignment=assignment, agent=agent, require_adapter_enabled=require_adapter_enabled)
    if refusal is not None:
        return _turn(assignment, *refusal)

    attempt_id = uuid.uuid4()
    transition_status(db, assignment=assignment, new_status="reviewing", detail={"reason": "agent_answer_reviewed", "attempt_id": str(attempt_id)})
    transition_status(db, assignment=assignment, new_status="changes_requested", detail={"reason": "follow_up_instruction", "attempt_id": str(attempt_id)})
    transition_status(db, assignment=assignment, new_status="running", detail={"reason": "follow_up_delivered", "attempt_id": str(attempt_id)})
    execution = start_execution_tracking(db, assignment=assignment, adapter_key=key, attempt_id=attempt_id)
    execution.process_ref = session_id
    db.flush()

    bind = getattr(adapter, "bind_session", None)
    try:
        if bind is not None:
            bind(assignment.id, session_id)
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
