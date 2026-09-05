"""Workspace state + workspace memory (MainAI V2, Stage V2-I2).

WORKSPACE STATE != EXECUTION AUTHORITY: nothing here grants permission to do anything.
Restoring a WorkspaceState with a populated `paused_action` does NOT re-execute or
re-authorize it -- see restore_workspace_state_does_not_reauthorize() and the corresponding
test.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.operating_shell.types import (
    ControlState,
    MalformedWorkspaceSnapshotError,
    PausedAction,
    WorkspaceAction,
    WorkspaceDocument,
    WorkspaceSecretShapedContentError,
    WorkspaceSelection,
    WorkspaceWindow,
)

_MAX_RECENT_COMMANDS = 50

# Denylist + shape check mirroring app.sentinel.service._validate_details's discipline:
# reject anything that looks like a password/secret field or a long opaque token, rather
# than trying to enumerate every possible secret format.
_DENYLISTED_FIELD_NAME_FRAGMENTS = ("password", "passwd", "secret", "api_key", "apikey", "token", "credential")
_MAX_FIELD_LENGTH = 200
_TOKEN_SHAPE_RE = re.compile(r"^[A-Za-z0-9_\-]{32,}$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def reject_secret_shaped_content(field_name: str, value: str) -> None:
    lowered = field_name.lower()
    for fragment in _DENYLISTED_FIELD_NAME_FRAGMENTS:
        if fragment in lowered:
            raise WorkspaceSecretShapedContentError(f"field name {field_name!r} looks like a secret field, not a workspace reference")
    if len(value) > _MAX_FIELD_LENGTH:
        raise WorkspaceSecretShapedContentError(f"value under {field_name!r} is {len(value)} chars -- too long for a workspace reference/summary")
    if _TOKEN_SHAPE_RE.match(value):
        raise WorkspaceSecretShapedContentError(f"value under {field_name!r} looks like an opaque secret/token, not a workspace reference")


@dataclass
class WorkspaceState:
    """Mutable; owned/mutated only via this module's functions."""

    owner_id: uuid.UUID
    open_windows: tuple[WorkspaceWindow, ...] = ()
    focused_target_ref: uuid.UUID | None = None
    selected_targets: tuple[WorkspaceSelection, ...] = ()
    open_document: WorkspaceDocument | None = None
    document_position: str | None = None  # e.g. "page 4" -- a reference, never content
    browser_tab_ref: str | None = None
    working_set: tuple[uuid.UUID, ...] = ()
    current_task: str | None = None
    related_intent_id: uuid.UUID | None = None
    pending_action: WorkspaceAction | None = None
    paused_action: PausedAction | None = None
    recent_commands: tuple[uuid.UUID, ...] = ()
    control_state: ControlState = ControlState.MAINAI_CONTROL
    updated_at: datetime = field(default_factory=_utcnow)


def new_workspace_state(*, owner_id: uuid.UUID) -> WorkspaceState:
    return WorkspaceState(owner_id=owner_id)


def set_current_task(state: WorkspaceState, *, task: str) -> WorkspaceState:
    reject_secret_shaped_content("current_task", task)
    state.current_task = task
    state.updated_at = _utcnow()
    return state


def record_recent_command(state: WorkspaceState, command_id: uuid.UUID) -> WorkspaceState:
    """Bounded: keeps at most _MAX_RECENT_COMMANDS, dropping the oldest -- never grows
    unbounded (see "large workspace bounded" test)."""
    updated = (*state.recent_commands, command_id)
    if len(updated) > _MAX_RECENT_COMMANDS:
        updated = updated[-_MAX_RECENT_COMMANDS:]
    state.recent_commands = updated
    state.updated_at = _utcnow()
    return state


def restore_workspace_state_does_not_reauthorize(state: WorkspaceState) -> WorkspaceAction | None:
    """WORKSPACE STATE != EXECUTION AUTHORITY: reading a restored WorkspaceState's
    `paused_action` returns the action for a caller's OWN INFORMATION only -- it does not
    mark anything executed, does not call any policy function, and does not clear the
    paused_action field. A caller wanting to actually act on it must go through
    risk.evaluate_action_authority()/build_action_result() from scratch, exactly as if the
    action were brand new."""
    return state.paused_action.action if state.paused_action else None


# --- Snapshot round-trip. -----------------------------------------------------------------


def _window_to_dict(w: WorkspaceWindow) -> dict:
    return {
        "window_id": str(w.window_id), "title": w.title, "app_name": w.app_name,
        "geometry": list(w.geometry), "vault_linked": w.vault_linked,
    }


def _window_from_dict(d: dict) -> WorkspaceWindow:
    return WorkspaceWindow(
        window_id=uuid.UUID(d["window_id"]), title=d["title"], app_name=d["app_name"],
        geometry=tuple(d["geometry"]), vault_linked=d["vault_linked"],
    )


def to_snapshot(state: WorkspaceState) -> dict:
    return {
        "owner_id": str(state.owner_id),
        "open_windows": [_window_to_dict(w) for w in state.open_windows],
        "focused_target_ref": str(state.focused_target_ref) if state.focused_target_ref else None,
        "current_task": state.current_task,
        "related_intent_id": str(state.related_intent_id) if state.related_intent_id else None,
        "recent_commands": [str(c) for c in state.recent_commands],
        "control_state": state.control_state.value,
        "updated_at": state.updated_at.isoformat(),
    }


_REQUIRED_SNAPSHOT_KEYS = {"owner_id", "open_windows", "control_state", "updated_at"}


def from_snapshot(snapshot: dict) -> WorkspaceState:
    """Fails closed: raises MalformedWorkspaceSnapshotError on any missing required key or
    malformed value, rather than silently producing a half-valid WorkspaceState (see
    "malformed persisted workspace fails closed" / "crash during state write recovers
    safely" tests)."""
    missing = _REQUIRED_SNAPSHOT_KEYS - snapshot.keys()
    if missing:
        raise MalformedWorkspaceSnapshotError(f"snapshot missing required keys: {sorted(missing)}")
    try:
        return WorkspaceState(
            owner_id=uuid.UUID(snapshot["owner_id"]),
            open_windows=tuple(_window_from_dict(w) for w in snapshot["open_windows"]),
            focused_target_ref=uuid.UUID(snapshot["focused_target_ref"]) if snapshot.get("focused_target_ref") else None,
            current_task=snapshot.get("current_task"),
            related_intent_id=uuid.UUID(snapshot["related_intent_id"]) if snapshot.get("related_intent_id") else None,
            recent_commands=tuple(uuid.UUID(c) for c in snapshot.get("recent_commands", [])),
            control_state=ControlState(snapshot["control_state"]),
            updated_at=datetime.fromisoformat(snapshot["updated_at"]),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise MalformedWorkspaceSnapshotError(f"snapshot is malformed: {exc}") from exc


# --- Workspace memory: safe semantic summary. ----------------------------------------------


@dataclass(frozen=True)
class WorkspaceMemorySummary:
    """A genuinely semantic summary -- NOT a full OS process snapshot. Sufficient to answer
    "what was I doing this morning" without claiming to literally resurrect process state."""

    task_description: str | None
    document_or_app_refs: tuple[str, ...]
    comparison_targets: tuple[str, ...]
    last_known_location: str | None
    unfinished_items: tuple[str, ...]
    summarized_at: datetime = field(default_factory=_utcnow)


def summarize_workspace_memory(state: WorkspaceState, *, comparison_targets: tuple[str, ...] = ()) -> WorkspaceMemorySummary:
    document_or_app_refs = tuple(w.title for w in state.open_windows)
    unfinished: list[str] = []
    if state.pending_action is not None:
        unfinished.append(f"pending: {state.pending_action.command.action_type.value}")
    if state.paused_action is not None:
        unfinished.append(f"paused: {state.paused_action.action.command.action_type.value}")
    last_known_location = state.open_document.title if state.open_document else None
    return WorkspaceMemorySummary(
        task_description=state.current_task,
        document_or_app_refs=document_or_app_refs,
        comparison_targets=comparison_targets,
        last_known_location=last_known_location,
        unfinished_items=tuple(unfinished),
    )
