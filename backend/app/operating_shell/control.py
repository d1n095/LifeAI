"""Control arbitration (MainAI V2, Stage V2-I1 / V2-I4 of the founder's header numbering).

USER INPUT > AUTOMATION: on_user_input() always and immediately transitions to
USER_TAKEOVER, with no negotiation and no "MainAI finishes its current step first" -- the
transition function itself has no branch that preserves MAINAI_CONTROL when a real
UserInputSignal arrives.

USER TAKEOVER != TASK CANCEL: begin_takeover() records the in-flight action as a
PausedAction rather than discarding it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.operating_shell.types import (
    ControlState,
    PausedAction,
    UserInputSignal,
    WorkspaceAction,
)


@dataclass
class ControlArbitrationState:
    owner_id: uuid.UUID
    state: ControlState = ControlState.MAINAI_CONTROL
    paused_action: PausedAction | None = None


def new_control_state(*, owner_id: uuid.UUID) -> ControlArbitrationState:
    return ControlArbitrationState(owner_id=owner_id)


def on_user_input(
    state: ControlArbitrationState,
    signal: UserInputSignal,
    *,
    in_flight_action: WorkspaceAction | None = None,
    workspace_snapshot_ref: uuid.UUID | None = None,
) -> ControlArbitrationState:
    """USER INPUT > AUTOMATION: unconditional. `signal` being real and present is the only
    precondition -- there is no risk/state check that could keep MainAI in control here.
    If MainAI had an in-flight action, it is paused (never cancelled/discarded)."""
    if in_flight_action is not None:
        if workspace_snapshot_ref is None:
            raise ValueError("workspace_snapshot_ref is required to pause an in-flight action")
        state.paused_action = PausedAction(
            paused_action_id=uuid.uuid4(), action=in_flight_action, workspace_snapshot_ref=workspace_snapshot_ref
        )
    state.state = ControlState.USER_TAKEOVER
    return state


def end_takeover(state: ControlArbitrationState) -> ControlArbitrationState:
    """Takeover ends; control returns to SHARED_CONTROL (never silently back to full
    MAINAI_CONTROL -- a real new instruction is required to re-activate automation, per the
    design doc's "resuming requires an explicit new instruction" rule). The paused action
    is NOT cleared here -- resume_from_current_state() is the only function that consumes
    (and clears) it, and only after re-validating it."""
    if state.state == ControlState.USER_TAKEOVER:
        state.state = ControlState.SHARED_CONTROL
    return state


@dataclass(frozen=True)
class ResumeDecision:
    """What resume_from_current_state() decided. `stale` is True when the paused action's
    target no longer matches current workspace state -- resume must not blindly re-issue a
    stale action."""

    can_resume: bool
    stale: bool
    reason: str
    action: WorkspaceAction | None


def resume_from_current_state(
    state: ControlArbitrationState,
    *,
    current_known_target_ids: frozenset[uuid.UUID],
) -> ResumeDecision:
    """Resuming reads CURRENT workspace state (the live set of known target ids) and
    decides what to do next -- it never blindly replays the exact interrupted step. If the
    paused action's target_ref is no longer a known target, the action is considered stale
    and is NOT returned as resumable; the caller must re-plan, not re-issue it verbatim."""
    paused = state.paused_action
    if paused is None:
        return ResumeDecision(can_resume=False, stale=False, reason="no paused action", action=None)

    target_ref = paused.action.command.target_ref
    if target_ref is not None and target_ref not in current_known_target_ids:
        state.paused_action = None
        return ResumeDecision(
            can_resume=False, stale=True, reason=f"paused action's target {target_ref} no longer exists", action=None
        )

    action = paused.action
    state.paused_action = None
    if state.state != ControlState.USER_TAKEOVER:
        state.state = ControlState.MAINAI_CONTROL
    return ResumeDecision(can_resume=True, stale=False, reason="current state still supports this action", action=action)
