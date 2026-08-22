"""Interactive Agent Execution Control -- extends the bounded dispatch foundation
(`app.agent_coordination.dispatch`) from a "start-then-collect" model into a provider-neutral
model that can track a real, long-running agent process/session: output arriving over time,
heartbeats, an interactive control contract (instruction/status/cancel/resume), and honest
reconnect/recovery semantics after a restart or disconnect.

This is a layer ABOVE `dispatch.py`, never a replacement or a second supervisor: every
function here either CALLS `dispatch.py`'s already-reviewed functions (`dispatch_assignment`,
`collect_dispatch_result`, `evaluate_dispatch_readiness`) or reads/writes the NEW
`AgentDispatchExecution` tracking row (migration 0047) that dispatch.py itself does not touch.
`WorkAssignmentStatus` (the canonical, coarser state machine `transition_status()` enforces)
remains the single source of truth for an assignment's own lifecycle; `ExecutionAdapterState`
tracks the FINER-grained, per-ATTEMPT process/session state underneath it -- the two are kept
in sync by this module's own functions, never left to drift independently, and neither is ever
inferred from the other.

DURABILITY POLICY (requirement 1's "events must be durable/auditable where appropriate"):
structured events (`status`, `progress`, `tool_action`, `heartbeat`, `partial_result`,
`final_result`) are ALWAYS persisted as an `execution_observed` `AgentWorkAssignmentEvent` --
the same append-only, RLS-protected, trigger-enforced history every other assignment event
already goes through. Raw `stdout`/`stderr` chunks are, by default, NOT persisted this way --
only their ARRIVAL TIME updates `AgentDispatchExecution.last_output_at` -- because a real CLI
agent's raw output can be arbitrarily large and high-frequency; a caller that wants a specific
stdout/stderr chunk durably recorded may pass `persist=True` explicitly to `record_execution_
event()`, an opt-in, bounded exception, never the default.

CAPABILITY POLICY (requirement 2): every adapter declares its own `AdapterCapabilities`
(`app.agent_coordination.adapters.AdapterCapabilities`) truthfully. `send_execution_
instruction()`/`cancel_execution()`/`resume_execution()` below check the relevant flag BEFORE
ever calling the adapter's own method -- attempting an unsupported operation returns a
structured `ControlOutcome(OUTCOME_UNSUPPORTED_CAPABILITY, ...)`, never a raised
`NotImplementedError` a caller would have to specifically catch, and never a silent no-op.

RECOVERY POLICY (requirement 4): `reconcile_execution_state()` NEVER marks an assignment
`completed` -- that remains exclusively `apply_dispatch_result()`'s job, called only through
`dispatch.py`'s own `collect_dispatch_result()`. This function only OBSERVES and CLASSIFIES;
distinguishing process-still-alive / exited / adapter-disconnected / session-lost /
result-pending-ingestion / result-unavailable is its entire job."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.agent_coordination.dispatch import (
    OUTCOME_ADAPTER_PROCESS_LOST,
    OUTCOME_ADAPTER_TIMEOUT,
    DispatchDecision,
    collect_dispatch_result,
)
from app.agent_coordination.service import transition_status
from app.models.agent_coordination import (
    AgentDispatchExecution,
    AgentWorkAssignment,
    AgentWorkAssignmentEvent,
    AgentWorkAssignmentEventType,
    CoordinationAgent,
    ExecutionAdapterState,
    ExecutionResultIngestionStatus,
)

if TYPE_CHECKING:
    from app.agent_coordination.adapters import AgentAdapter

# ============================================================================ 1: the output-
# streaming event shape -- provider-neutral, UNKNOWN/UNSUPPORTED always valid.

DURABLE_EVENT_KINDS = frozenset({"status", "progress", "tool_action", "heartbeat", "partial_result", "final_result"})
EPHEMERAL_EVENT_KINDS = frozenset({"stdout", "stderr"})
ALL_EVENT_KINDS = DURABLE_EVENT_KINDS | EPHEMERAL_EVENT_KINDS


@dataclass(frozen=True)
class ExecutionEvent:
    """One point-in-time observation from a running adapter -- see module docstring's
    durability policy for which `kind`s are persisted by default. `kind` outside
    `ALL_EVENT_KINDS` is still accepted (a future provider-specific kind this module does not
    yet know about is not itself an error) but is treated as ephemeral unless `persist=True`."""

    kind: str
    summary: str = ""
    detail: dict = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=datetime.utcnow)


def record_execution_event(
    db: Session, *, execution: AgentDispatchExecution, assignment: AgentWorkAssignment, event: ExecutionEvent, persist: bool | None = None
) -> None:
    """Applies ONE `ExecutionEvent`'s side effects: always updates the live tracking row's
    `last_heartbeat_at` (for `heartbeat`/`status`/`progress`) or `last_output_at` (for
    `stdout`/`stderr`), then durably records it as an `execution_observed`
    `AgentWorkAssignmentEvent` when `persist` resolves true -- defaults to
    `event.kind in DURABLE_EVENT_KINDS` when the caller does not pass `persist` explicitly, so
    a caller can still force a specific ephemeral-by-default stdout/stderr chunk to be
    durable (e.g. a final short summary line) without this module inventing a second
    durability rule to work around."""

    now = event.occurred_at
    if event.kind == "heartbeat" or event.kind in ("status", "progress"):
        execution.last_heartbeat_at = now
    if event.kind in ("stdout", "stderr"):
        execution.last_output_at = now
    db.flush()

    should_persist = event.kind in DURABLE_EVENT_KINDS if persist is None else persist
    if not should_persist:
        return
    db.add(AgentWorkAssignmentEvent(
        assignment_id=assignment.id, owner_id=assignment.owner_id, event_type=AgentWorkAssignmentEventType.execution_observed,
        detail={"kind": event.kind, "summary": event.summary, "attempt_id": str(execution.attempt_id), **event.detail},
    ))
    db.flush()


# ============================================================================ 2: the
# interactive control contract -- fail closed on every unsupported capability.

OUTCOME_OK = "OK"
OUTCOME_UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"


@dataclass(frozen=True)
class ControlOutcome:
    outcome: str
    detail: str = ""


async def send_execution_instruction(
    db: Session, *, execution: AgentDispatchExecution, assignment: AgentWorkAssignment, adapter: "AgentAdapter", instruction: str
) -> ControlOutcome:
    """Fails closed, structurally, when `adapter.control_capabilities().supports_instruction`
    is `False` -- the adapter's own `send_instruction()` is never even called in that case,
    matching this module's "check the flag, then call, never call-and-catch" doctrine (a
    `NotImplementedError` from a well-behaved adapter would ALSO be a bug in whichever caller
    skipped this check, not something routine code should have to catch)."""

    caps = adapter.control_capabilities()
    if not caps.supports_instruction:
        return ControlOutcome(OUTCOME_UNSUPPORTED_CAPABILITY, "adapter does not declare supports_instruction")
    await adapter.send_instruction(execution.assignment_id, instruction)
    record_execution_event(
        db, execution=execution, assignment=assignment,
        event=ExecutionEvent(kind="status", summary="instruction sent", detail={"instruction": instruction}),
    )
    return ControlOutcome(OUTCOME_OK)


async def request_execution_status(*, execution: AgentDispatchExecution, adapter: "AgentAdapter"):
    """`observe()` is part of the BASE `AgentAdapter` Protocol (not capability-gated) -- every
    adapter, including `NotConfiguredAdapter`, must answer a status request, even if that
    answer is itself a `ProviderNotConfiguredError`-shaped honest refusal. Never gated on a
    capability flag; there is no `supports_status` flag by design. Read-only -- does not touch
    `db` at all, unlike the other control-contract functions in this section."""

    return await adapter.observe(execution.assignment_id)


async def cancel_execution(db: Session, *, execution: AgentDispatchExecution, assignment: AgentWorkAssignment, adapter: "AgentAdapter") -> ControlOutcome:
    caps = adapter.control_capabilities()
    if not caps.supports_cancel:
        return ControlOutcome(OUTCOME_UNSUPPORTED_CAPABILITY, "adapter does not declare supports_cancel")
    await adapter.cancel(execution.assignment_id)
    mark_execution_cancelled(db, execution=execution)
    if assignment.status.value in ("waiting_agent", "running"):
        transition_status(db, assignment=assignment, new_status="cancelled", detail={"reason": "execution_cancelled", "attempt_id": str(execution.attempt_id)})
    return ControlOutcome(OUTCOME_OK)


async def resume_execution(db: Session, *, execution: AgentDispatchExecution, assignment: AgentWorkAssignment, adapter: "AgentAdapter") -> ControlOutcome:
    caps = adapter.control_capabilities()
    if not caps.supports_resume:
        return ControlOutcome(OUTCOME_UNSUPPORTED_CAPABILITY, "adapter does not declare supports_resume")
    await adapter.resume(execution.assignment_id)
    record_execution_event(
        db, execution=execution, assignment=assignment,
        event=ExecutionEvent(kind="status", summary="resumed"),
    )
    return ControlOutcome(OUTCOME_OK)


# ============================================================================ 3: long-running
# process tracking -- the live AgentDispatchExecution row.

def start_execution_tracking(
    db: Session, *, assignment: AgentWorkAssignment, adapter_key: str, attempt_id: uuid.UUID
) -> AgentDispatchExecution:
    """Creates the live tracking row for ONE dispatch attempt -- call this AFTER
    `dispatch.dispatch_assignment()` returns `OUTCOME_ASSIGNABLE` (its actual success outcome
    constant), never merely because its `attempt_id` is non-`None`: `attempt_id` is generated
    BEFORE `adapter.start_assignment()` is even attempted, so a crashed start
    (`ADAPTER_PROCESS_LOST`/`ADAPTER_TIMEOUT`/`REAL_PROVIDER_NOT_CONFIGURED`) still carries one
    -- correctly correlating the crash's own `blocked` transition with that specific attempt --
    but nothing actually started running, so it gets no tracking row here."""

    execution = AgentDispatchExecution(
        owner_id=assignment.owner_id, assignment_id=assignment.id, attempt_id=attempt_id,
        adapter_key=adapter_key, adapter_state=ExecutionAdapterState.starting,
    )
    db.add(execution)
    db.flush()
    return execution


def mark_execution_running(db: Session, *, execution: AgentDispatchExecution, process_ref: str | None = None) -> None:
    execution.adapter_state = ExecutionAdapterState.running
    if process_ref is not None:
        execution.process_ref = process_ref
    db.flush()


def mark_execution_exited(db: Session, *, execution: AgentDispatchExecution) -> None:
    execution.adapter_state = ExecutionAdapterState.exited
    execution.ended_at = datetime.utcnow()
    db.flush()


def mark_execution_lost(db: Session, *, execution: AgentDispatchExecution) -> None:
    execution.adapter_state = ExecutionAdapterState.lost
    execution.ended_at = datetime.utcnow()
    db.flush()


def mark_execution_timeout(db: Session, *, execution: AgentDispatchExecution) -> None:
    execution.adapter_state = ExecutionAdapterState.timeout
    execution.ended_at = datetime.utcnow()
    db.flush()


def mark_execution_cancelled(db: Session, *, execution: AgentDispatchExecution) -> None:
    execution.adapter_state = ExecutionAdapterState.cancelled
    execution.ended_at = datetime.utcnow()
    db.flush()


def mark_result_ingested(db: Session, *, execution: AgentDispatchExecution) -> None:
    execution.result_ingestion_status = ExecutionResultIngestionStatus.ingested
    db.flush()


def mark_result_ingestion_failed(db: Session, *, execution: AgentDispatchExecution) -> None:
    execution.result_ingestion_status = ExecutionResultIngestionStatus.failed
    db.flush()


# ============================================================================ collection side:
# wraps dispatch.collect_dispatch_result() with execution-tracking bookkeeping, never
# duplicates its own crash handling.

async def collect_and_ingest_execution_result(
    db: Session, *, execution: AgentDispatchExecution, assignment: AgentWorkAssignment, agent: CoordinationAgent, adapter: "AgentAdapter",
    adapter_key: str | None = None,
) -> DispatchDecision:
    """The companion to `start_execution_tracking()` on the collection side -- calls
    `dispatch.collect_dispatch_result()` (never reimplements its crash handling) and mirrors
    its outcome onto the tracking row. If `collect_dispatch_result()` itself raises (a genuine
    "agent completed but result ingestion failed" case -- see that function's own docstring),
    marks `result_ingestion_status=failed` and re-raises; the caller's own transaction boundary
    still decides what happens next, exactly as `collect_dispatch_result()` already documents
    for itself."""

    try:
        decision = await collect_dispatch_result(
            db, assignment=assignment, agent=agent, adapter=adapter, adapter_key=adapter_key, dispatch_attempt_id=execution.attempt_id
        )
    except Exception:
        mark_result_ingestion_failed(db, execution=execution)
        raise

    if decision.outcome == OUTCOME_ADAPTER_PROCESS_LOST:
        mark_execution_lost(db, execution=execution)
        mark_result_ingestion_failed(db, execution=execution)
    elif decision.outcome == OUTCOME_ADAPTER_TIMEOUT:
        mark_execution_timeout(db, execution=execution)
        mark_result_ingestion_failed(db, execution=execution)
    else:
        mark_execution_exited(db, execution=execution)
        mark_result_ingested(db, execution=execution)
    return decision


# ============================================================================ 4: reconnect /
# recovery -- observe and classify only; NEVER marks an assignment completed.

RECONCILE_PROCESS_ALIVE = "PROCESS_ALIVE"
RECONCILE_PROCESS_EXITED = "PROCESS_EXITED"
RECONCILE_ADAPTER_DISCONNECTED = "ADAPTER_DISCONNECTED"
RECONCILE_SESSION_LOST = "SESSION_LOST"
RECONCILE_RESULT_UNAVAILABLE = "RESULT_UNAVAILABLE"
RECONCILE_RESULT_PENDING_INGESTION = "RESULT_PENDING_INGESTION"


@dataclass(frozen=True)
class ReconciliationDecision:
    outcome: str
    detail: str = ""


async def reconcile_execution_state(
    db: Session, *, execution: AgentDispatchExecution, assignment: AgentWorkAssignment, adapter: "AgentAdapter"
) -> ReconciliationDecision:
    """Distinguishes, never conflates: process still alive / exited-but-not-yet-ingested /
    exited-and-already-ingested / adapter disconnected (never configured) / session lost
    (tracked process no longer traceable) / result unavailable (never actually started).
    Calling this NEVER changes `AgentWorkAssignment.status` -- only `collect_dispatch_result()`
    (via `apply_dispatch_result()`) is ever allowed to advance an assignment to `completed`;
    this function's entire job is classification a caller then acts on, exactly like
    `evaluate_dispatch_readiness()` never dispatches anything itself."""

    from app.agent_coordination.adapters import AdapterProcessLostError, ProviderNotConfiguredError

    if execution.adapter_state == ExecutionAdapterState.exited:
        if execution.result_ingestion_status == ExecutionResultIngestionStatus.pending:
            return ReconciliationDecision(RECONCILE_RESULT_PENDING_INGESTION, "process exited but its result has not been ingested yet")
        return ReconciliationDecision(RECONCILE_PROCESS_EXITED, "process exited and its result was already ingested")
    if execution.adapter_state in (ExecutionAdapterState.lost, ExecutionAdapterState.timeout, ExecutionAdapterState.cancelled):
        return ReconciliationDecision(RECONCILE_SESSION_LOST, f"tracking row already recorded a terminal adapter_state={execution.adapter_state.value}")

    try:
        observation = await adapter.observe(execution.assignment_id)
    except ProviderNotConfiguredError as exc:
        return ReconciliationDecision(RECONCILE_ADAPTER_DISCONNECTED, str(exc))
    except AdapterProcessLostError as exc:
        mark_execution_lost(db, execution=execution)
        return ReconciliationDecision(RECONCILE_SESSION_LOST, str(exc))

    if observation.raw_status == "running":
        record_execution_event(
            db, execution=execution, assignment=assignment, event=ExecutionEvent(kind="heartbeat", summary="reconciled: still running")
        )
        return ReconciliationDecision(RECONCILE_PROCESS_ALIVE, "adapter reports the process is still running")
    if observation.raw_status == "exited":
        mark_execution_exited(db, execution=execution)
        return ReconciliationDecision(RECONCILE_RESULT_PENDING_INGESTION, "adapter reports the process exited; result not yet ingested")
    return ReconciliationDecision(RECONCILE_RESULT_UNAVAILABLE, f"adapter reports raw_status={observation.raw_status!r} -- no result to ingest")
