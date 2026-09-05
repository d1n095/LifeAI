"""MainAI Operating Shell -- types (MainAI V2, Stages V2-I1..I7).

Standalone, isolated, NOT imported by any production runtime path, and does NOT import
app.guardian, app.privacy_boundary, app.sentinel, app.sovereign_identity, or
app.life_recovery (same independence discipline those five packages already hold toward
each other). See docs/mainai_v2/MAINAI_V2_ORB_OPERATING_SHELL.md for the design this
implements.

CORE PRODUCT RULE: MAINAI IS THE PRIMARY UI. SCREENS ARE TOOLS, NOT THE PRODUCT.

COMMAND DESCRIPTION != OS AUTHORITY: WorkspaceCommand/WorkspaceAction are pure data --
neither this module nor any other module in this package defines an execute()/run()/apply()
method that performs a real OS-level effect. See test_no_execute_method_exists_anywhere for
the structural proof.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Declarative action vocabulary. ------------------------------------------------------


class WorkspaceActionType(str, Enum):
    OPEN_DOCUMENT = "OPEN_DOCUMENT"
    FOCUS_WINDOW = "FOCUS_WINDOW"
    MOVE_WINDOW = "MOVE_WINDOW"
    RESIZE_WINDOW = "RESIZE_WINDOW"
    CLOSE_WINDOW = "CLOSE_WINDOW"
    SHOW_ITEM = "SHOW_ITEM"
    HIDE_ITEM = "HIDE_ITEM"
    SELECT_ITEM = "SELECT_ITEM"
    SCROLL_TO = "SCROLL_TO"
    COMPARE_ITEMS = "COMPARE_ITEMS"
    HIGHLIGHT_REGION = "HIGHLIGHT_REGION"
    TYPE_TEXT = "TYPE_TEXT"
    NAVIGATE = "NAVIGATE"
    LAUNCH_APP = "LAUNCH_APP"
    RESTORE_WORKSPACE = "RESTORE_WORKSPACE"
    # Non-UI-shaped examples needed to have something concrete at the higher risk tiers
    # (the 15 actions above are all UI-shaped, per the spec's own note that a couple of
    # extra examples are needed for EXTERNAL_EFFECT/DESTRUCTIVE/ROOT_SECURITY_SENSITIVE).
    EDIT_DOCUMENT = "EDIT_DOCUMENT"
    SUBMIT_FORM = "SUBMIT_FORM"
    SEND_MESSAGE = "SEND_MESSAGE"
    DELETE_FILE = "DELETE_FILE"
    WIPE_DATA = "WIPE_DATA"
    CHANGE_SECURITY_POLICY = "CHANGE_SECURITY_POLICY"


class ActionRiskLevel(str, Enum):
    OBSERVATIONAL = "OBSERVATIONAL"
    REVERSIBLE_LOW_RISK = "REVERSIBLE_LOW_RISK"
    REVERSIBLE_CONSEQUENTIAL = "REVERSIBLE_CONSEQUENTIAL"
    EXTERNAL_EFFECT = "EXTERNAL_EFFECT"
    DESTRUCTIVE = "DESTRUCTIVE"
    ROOT_SECURITY_SENSITIVE = "ROOT_SECURITY_SENSITIVE"


ACTION_RISK_ORDER: dict[ActionRiskLevel, int] = {
    ActionRiskLevel.OBSERVATIONAL: 0,
    ActionRiskLevel.REVERSIBLE_LOW_RISK: 1,
    ActionRiskLevel.REVERSIBLE_CONSEQUENTIAL: 2,
    ActionRiskLevel.EXTERNAL_EFFECT: 3,
    ActionRiskLevel.DESTRUCTIVE: 4,
    ActionRiskLevel.ROOT_SECURITY_SENSITIVE: 5,
}

# Consequential-and-above: REVERSIBLE_CONSEQUENTIAL or higher requires an ActionPreview
# before a result may be marked executed (see risk.require_preview_for_consequential_action).
CONSEQUENTIAL_AND_ABOVE = frozenset(
    {
        ActionRiskLevel.REVERSIBLE_CONSEQUENTIAL,
        ActionRiskLevel.EXTERNAL_EFFECT,
        ActionRiskLevel.DESTRUCTIVE,
        ActionRiskLevel.ROOT_SECURITY_SENSITIVE,
    }
)

DEFAULT_ACTION_RISK: dict[WorkspaceActionType, ActionRiskLevel] = {
    WorkspaceActionType.OPEN_DOCUMENT: ActionRiskLevel.OBSERVATIONAL,
    WorkspaceActionType.FOCUS_WINDOW: ActionRiskLevel.OBSERVATIONAL,
    WorkspaceActionType.SHOW_ITEM: ActionRiskLevel.OBSERVATIONAL,
    WorkspaceActionType.SELECT_ITEM: ActionRiskLevel.OBSERVATIONAL,
    WorkspaceActionType.SCROLL_TO: ActionRiskLevel.OBSERVATIONAL,
    WorkspaceActionType.COMPARE_ITEMS: ActionRiskLevel.OBSERVATIONAL,
    WorkspaceActionType.HIGHLIGHT_REGION: ActionRiskLevel.OBSERVATIONAL,
    WorkspaceActionType.NAVIGATE: ActionRiskLevel.OBSERVATIONAL,
    WorkspaceActionType.MOVE_WINDOW: ActionRiskLevel.REVERSIBLE_LOW_RISK,
    WorkspaceActionType.RESIZE_WINDOW: ActionRiskLevel.REVERSIBLE_LOW_RISK,
    WorkspaceActionType.HIDE_ITEM: ActionRiskLevel.REVERSIBLE_LOW_RISK,
    WorkspaceActionType.CLOSE_WINDOW: ActionRiskLevel.REVERSIBLE_LOW_RISK,
    WorkspaceActionType.LAUNCH_APP: ActionRiskLevel.REVERSIBLE_LOW_RISK,
    WorkspaceActionType.RESTORE_WORKSPACE: ActionRiskLevel.REVERSIBLE_LOW_RISK,
    WorkspaceActionType.TYPE_TEXT: ActionRiskLevel.REVERSIBLE_CONSEQUENTIAL,
    WorkspaceActionType.EDIT_DOCUMENT: ActionRiskLevel.REVERSIBLE_CONSEQUENTIAL,
    WorkspaceActionType.SUBMIT_FORM: ActionRiskLevel.EXTERNAL_EFFECT,
    WorkspaceActionType.SEND_MESSAGE: ActionRiskLevel.EXTERNAL_EFFECT,
    WorkspaceActionType.DELETE_FILE: ActionRiskLevel.DESTRUCTIVE,
    WorkspaceActionType.WIPE_DATA: ActionRiskLevel.DESTRUCTIVE,
    WorkspaceActionType.CHANGE_SECURITY_POLICY: ActionRiskLevel.ROOT_SECURITY_SENSITIVE,
}


class ControlState(str, Enum):
    MAINAI_CONTROL = "MAINAI_CONTROL"
    SHARED_CONTROL = "SHARED_CONTROL"
    USER_TAKEOVER = "USER_TAKEOVER"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    RECOVERY_MODE = "RECOVERY_MODE"


class IntentState(str, Enum):
    CAPTURED = "CAPTURED"
    UNDERSTANDING = "UNDERSTANDING"
    PLANNED = "PLANNED"
    ACTIVE = "ACTIVE"
    WAITING = "WAITING"
    BLOCKED = "BLOCKED"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"
    SUPERSEDED = "SUPERSEDED"


NON_TERMINAL_INTENT_STATES = frozenset(
    {IntentState.ACTIVE, IntentState.WAITING, IntentState.BLOCKED, IntentState.PAUSED}
)
TERMINAL_INTENT_STATES = frozenset({IntentState.COMPLETED, IntentState.ABANDONED, IntentState.SUPERSEDED})


class ReferenceKind(str, Enum):
    THIS = "THIS"
    THAT = "THAT"
    THE_OLD_ONE = "THE_OLD_ONE"
    COMPARE_TARGETS = "COMPARE_TARGETS"
    CONTINUE = "CONTINUE"
    REOPEN_LAST = "REOPEN_LAST"
    SHOW_REASONING = "SHOW_REASONING"
    RECENT_FILE = "RECENT_FILE"


class EvidenceSurfaceKind(str, Enum):
    INCIDENT_EVIDENCE = "INCIDENT_EVIDENCE"
    DIFF_VIEW = "DIFF_VIEW"
    MEMORY_EVIDENCE = "MEMORY_EVIDENCE"


class ResourceAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    MISSING = "MISSING"
    STALE = "STALE"


class RestoreResult(str, Enum):
    RESTORABLE = "RESTORABLE"
    PARTIALLY_RESTORABLE = "PARTIALLY_RESTORABLE"
    BLOCKED = "BLOCKED"
    STALE = "STALE"


# --- Operating shell core primitives. -----------------------------------------------------


@dataclass(frozen=True)
class WorkspaceTarget:
    """A generic, opaque reference to something in the workspace -- a window, document,
    selection, etc. `target_id` is the real identity; `title` is a human-readable label that
    must NEVER be used alone as a lookup key (see "same-named windows do not collapse")."""

    target_id: uuid.UUID
    kind: str  # "window" | "document" | "selection" | "app" | "region"
    title: str


@dataclass(frozen=True)
class WorkspaceWindow:
    window_id: uuid.UUID
    title: str
    app_name: str
    geometry: tuple[int, int, int, int]  # x, y, width, height
    vault_linked: bool = False


@dataclass(frozen=True)
class WorkspaceDocument:
    document_id: uuid.UUID
    title: str
    path_or_ref: str
    version: int
    content_hash: str | None = None
    vault_linked: bool = False


@dataclass(frozen=True)
class WorkspaceSelection:
    selection_id: uuid.UUID
    target_ref: uuid.UUID
    kind: str  # "file" | "text" | "object"
    description: str  # short, e.g. "paragraph 2" -- never full selected content


@dataclass(frozen=True)
class WorkspaceCommand:
    """Pure data. No execute()/run()/apply() method -- see module docstring."""

    command_id: uuid.UUID
    action_type: WorkspaceActionType
    target_ref: uuid.UUID | None
    params: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class WorkspaceAction:
    """Pure data. No execute()/run()/apply() method -- see module docstring."""

    action_id: uuid.UUID
    command: WorkspaceCommand
    risk: ActionRiskLevel
    created_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class ActionPreview:
    """Required before a REVERSIBLE_CONSEQUENTIAL-or-above action's result may be marked
    executed -- see risk.require_preview_for_consequential_action()."""

    preview_id: uuid.UUID
    action_id: uuid.UUID
    description: str
    target_ref: uuid.UUID | None
    data_affected: tuple[str, ...]
    external_effect: bool
    external_effect_description: str
    reversible: bool
    reversal_description: str
    authority_required: ActionRiskLevel
    confirmation_required: bool
    rollback_possible: bool
    rollback_description: str
    created_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class WorkspaceActionResult:
    """Pure data. `executed` is only ever set True by result-construction functions that
    have already checked the preview/policy gate -- see risk.py."""

    result_id: uuid.UUID
    action_id: uuid.UUID
    executed: bool
    preview_ref: uuid.UUID | None
    outcome_summary: str
    executed_at: datetime = field(default_factory=_utcnow)


@dataclass
class ActionReceipt:
    """Hash-chained, append-only -- same discipline as Guardian's ContainmentReceipt chain
    and Sentinel's EventReceipt chain."""

    receipt_id: uuid.UUID
    action_id: uuid.UUID
    risk: ActionRiskLevel
    decision: str  # "allowed" | "denied" | "not_wired"
    prev_hash: str
    this_hash: str = ""
    created_at: datetime = field(default_factory=_utcnow)


# --- Control arbitration. -----------------------------------------------------------------


@dataclass(frozen=True)
class UserInputSignal:
    signal_id: uuid.UUID
    kind: str  # "mouse" | "keyboard" | "voice"
    occurred_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class PausedAction:
    """USER TAKEOVER != TASK CANCEL: a takeover records the in-flight action here rather
    than deleting it. `workspace_snapshot_ref` points at the WorkspaceState snapshot taken
    at pause time, so resume_from_current_state() can detect drift (see control.py)."""

    paused_action_id: uuid.UUID
    action: WorkspaceAction
    workspace_snapshot_ref: uuid.UUID
    paused_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class WorkspaceFocus:
    focused_target_ref: uuid.UUID | None
    focused_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class WorkspaceContext:
    """A bundle of resolution evidence for context.py's referring-expression resolver --
    NOT itself execution authority (WORKSPACE STATE != EXECUTION AUTHORITY)."""

    owner_id: uuid.UUID
    focus: WorkspaceFocus | None
    selection: WorkspaceSelection | None
    recent_action_refs: tuple[uuid.UUID, ...]
    active_intent_id: uuid.UUID | None
    known_targets: tuple[WorkspaceTarget, ...]


# --- Intent objects. -----------------------------------------------------------------------


@dataclass(frozen=True)
class IntentBlocker:
    description: str
    created_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class IntentHistoryEntry:
    from_state: IntentState | None
    to_state: IntentState
    at: datetime
    note: str


@dataclass
class IntentObject:
    """Mutable; owned/mutated only via app.operating_shell.intent's functions (mirrors
    SecurityIncident's "no public mutation outside the service module" discipline).

    RAW USER EXPRESSION != INTERPRETED TRUTH: `raw_user_expression` and `interpreted_goal`
    are genuinely separate fields -- see intent.create_intent_from_expression() and
    intent.record_understanding(), the only two functions that set them.

    FUTURE PLAN != FUTURE AUTHORITY: `next_actions` holds WorkspaceAction references only --
    nothing here is itself executable authority. See intent.py's module docstring.
    """

    intent_id: uuid.UUID
    owner_id: uuid.UUID
    title: str
    raw_user_expression: str
    state: IntentState
    created_at: datetime
    updated_at: datetime
    interpreted_goal: str | None = None
    priority: str = "normal"
    linked_workspace: uuid.UUID | None = None
    linked_files: tuple[uuid.UUID, ...] = ()
    linked_memories: tuple[uuid.UUID, ...] = ()
    linked_projects: tuple[uuid.UUID, ...] = ()
    assigned_agents: tuple[str, ...] = ()
    dependencies: tuple[uuid.UUID, ...] = ()
    blockers: tuple[IntentBlocker, ...] = ()
    next_actions: tuple[WorkspaceAction, ...] = ()
    risk: ActionRiskLevel | None = None
    authority_snapshot_ref: str | None = None
    completion_definition: str | None = None
    history: tuple[IntentHistoryEntry, ...] = ()
    current_summary: str = ""
    confidence: float | None = None
    assumptions: tuple[str, ...] = ()
    evidence: tuple[uuid.UUID, ...] = ()
    superseded_by: uuid.UUID | None = None


@dataclass(frozen=True)
class AmbiguousResolution:
    """DO NOT silently guess: returned whenever context/intent resolution finds more than
    one plausible high-confidence candidate."""

    reference_kind: ReferenceKind | str
    candidates: tuple[WorkspaceTarget, ...]
    reason: str


@dataclass(frozen=True)
class ResolvedReference:
    reference_kind: ReferenceKind | str
    target: WorkspaceTarget


# --- Show-don't-tell evidence surfaces. -----------------------------------------------------


@dataclass(frozen=True)
class EvidenceSurfaceRequest:
    kind: EvidenceSurfaceKind
    target_refs: tuple[uuid.UUID, ...]
    reason: str


@dataclass(frozen=True)
class EvidenceSurfaceResult:
    """The orb remains the primary UI; this is a subordinate, temporary surface -- reflected
    structurally via dismissible/returns_focus_to_orb rather than only in prose."""

    kind: EvidenceSurfaceKind
    target_refs: tuple[uuid.UUID, ...]
    dismissible: bool = True
    returns_focus_to_orb: bool = True


# --- Internal agent delegation boundary. ----------------------------------------------------


@dataclass(frozen=True)
class InternalDelegationResult:
    """AGENT RESULT != USER-FACING TRUTH: this type is deliberately NOT user-facing. The
    only path from this to something shown to the user is delegation.aggregate_for_user()."""

    delegation_id: uuid.UUID
    specialist_key: str
    finding_summary: str
    evidence_refs: tuple[str, ...]
    confidence: float | None


@dataclass(frozen=True)
class UserFacingAnswer:
    """The only type user-facing code may present as MainAI's answer. Constructed
    exclusively by delegation.aggregate_for_user()."""

    text: str
    source_delegation_ids: tuple[uuid.UUID, ...]
    intent_id: uuid.UUID | None = None


# --- Workspace restore planning. -------------------------------------------------------------


@dataclass(frozen=True)
class RestoreResourceStatus:
    target_ref: uuid.UUID
    kind: str
    availability: ResourceAvailability
    vault_linked: bool = False
    note: str = ""


@dataclass(frozen=True)
class WorkspaceRestorePlan:
    result: RestoreResult
    resources: tuple[RestoreResourceStatus, ...]
    vault_locked_refs: tuple[uuid.UUID, ...]
    notes: str = ""


# --- Errors. -----------------------------------------------------------------------------


class OperatingShellError(Exception):
    """Base class for this package's errors."""


class PolicyNotWiredError(OperatingShellError):
    """Raised by risk.py when an ActionAuthorityPolicy decision is required but no real
    policy has been supplied -- ACTION REQUEST != AUTHORITY, never a silent default-allow."""


class ConsequentialActionRequiresPreviewError(OperatingShellError):
    """Raised when a caller tries to mark a REVERSIBLE_CONSEQUENTIAL-or-above action's
    result as executed without an attached ActionPreview having been generated first."""


class MalformedWorkspaceSnapshotError(OperatingShellError):
    """Raised by workspace.from_snapshot() on a corrupted/incomplete snapshot dict."""


class MalformedIntentSnapshotError(OperatingShellError):
    """Raised by intent.py's snapshot round-trip on a corrupted/incomplete snapshot dict."""


class IntentGraphError(OperatingShellError):
    """Raised when an intent dependency graph is malformed (missing reference) or cyclic."""


class WorkspaceSecretShapedContentError(OperatingShellError):
    """Raised when a caller tries to stuff password-/secret-shaped content into a
    workspace-state field."""
