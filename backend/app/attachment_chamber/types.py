"""Attachment Chamber -- core types (MainAI V2, File Ingest Quarantine).

Standalone, isolated, NOT imported by any production runtime path. Does NOT import
app.guardian/app.privacy_boundary/app.sentinel/app.sovereign_identity/app.life_recovery/
app.operating_shell -- same mutual-independence discipline all six already hold toward each
other. Composition with any of them happens only in a future cross-layer test, never here.

FILE RECEIVED != FILE TRUSTED: every AttachmentIdentity starts at QuarantineState.RECEIVED,
never anywhere implying safety. FILE PARSED != FILE SAFE, ATTACHMENT != EXECUTION PERMISSION,
PREVIEW != OPEN, OPEN != EXECUTE, ARCHIVE CONTENT != TRUSTED CONTENT, MODEL INTERPRETATION !=
SECURITY VERDICT -- see service.py/risk.py/preview.py for where each of these is enforced
structurally, not just documented.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Lifecycle. -------------------------------------------------------------------------


class QuarantineState(str, Enum):
    RECEIVED = "RECEIVED"
    QUARANTINED = "QUARANTINED"
    INSPECTING = "INSPECTING"
    SAFE_FOR_PREVIEW = "SAFE_FOR_PREVIEW"
    SAFE_FOR_PARSE = "SAFE_FOR_PARSE"
    REQUIRES_USER_APPROVAL = "REQUIRES_USER_APPROVAL"
    BLOCKED = "BLOCKED"
    MALICIOUS = "MALICIOUS"
    SANITIZED = "SANITIZED"
    RELEASED = "RELEASED"
    DELETED = "DELETED"


# Same module-level-dict-of-allowed-targets convention as app.strategy_evaluation.service's
# EXPERIMENT_TRANSITIONS and app.life_intents.service's LIFE_INTENT_TRANSITIONS (this same
# campaign's own prior fix) -- not a new pattern. DELETED is fully terminal (nothing leaves
# it). MALICIOUS may only ever move to DELETED -- never back to any state implying safety.
QUARANTINE_TRANSITIONS: dict[QuarantineState, set[QuarantineState]] = {
    QuarantineState.RECEIVED: {QuarantineState.QUARANTINED, QuarantineState.DELETED},
    QuarantineState.QUARANTINED: {QuarantineState.INSPECTING, QuarantineState.DELETED},
    QuarantineState.INSPECTING: {
        QuarantineState.SAFE_FOR_PREVIEW,
        QuarantineState.SAFE_FOR_PARSE,
        QuarantineState.REQUIRES_USER_APPROVAL,
        QuarantineState.BLOCKED,
        QuarantineState.MALICIOUS,
        QuarantineState.DELETED,
    },
    QuarantineState.SAFE_FOR_PREVIEW: {
        QuarantineState.SAFE_FOR_PARSE,
        QuarantineState.SANITIZED,
        QuarantineState.RELEASED,
        QuarantineState.DELETED,
    },
    QuarantineState.SAFE_FOR_PARSE: {QuarantineState.SANITIZED, QuarantineState.RELEASED, QuarantineState.DELETED},
    QuarantineState.REQUIRES_USER_APPROVAL: {
        QuarantineState.SAFE_FOR_PREVIEW,
        QuarantineState.SAFE_FOR_PARSE,
        QuarantineState.BLOCKED,
        QuarantineState.DELETED,
    },
    QuarantineState.BLOCKED: {QuarantineState.DELETED},
    QuarantineState.MALICIOUS: {QuarantineState.DELETED},
    QuarantineState.SANITIZED: {QuarantineState.RELEASED, QuarantineState.DELETED},
    QuarantineState.RELEASED: {QuarantineState.DELETED},
    QuarantineState.DELETED: set(),
}

TERMINAL_QUARANTINE_STATES = frozenset({QuarantineState.DELETED})


class AttachmentSource(str, Enum):
    UPLOAD = "UPLOAD"
    CHAT_ATTACHMENT = "CHAT_ATTACHMENT"
    DOWNLOAD = "DOWNLOAD"
    ARCHIVE_MEMBER = "ARCHIVE_MEMBER"


class AttachmentChamberError(ValueError):
    pass


class InvalidQuarantineTransitionError(AttachmentChamberError):
    """FROM/TO pair is not a legitimate transition per QUARANTINE_TRANSITIONS."""


class TerminalQuarantineStateError(InvalidQuarantineTransitionError):
    """The identity's current state has no outbound transitions -- DELETED is fully
    terminal. No reopen operation exists for a deleted attachment."""


class OwnerMismatchError(AttachmentChamberError):
    """Raised by every lookup/mutation when the caller's owner_id does not match the
    identity's own -- never silently reassigned, never a partial/blank result."""


class NotFoundError(AttachmentChamberError):
    pass


class MalformedSnapshotError(AttachmentChamberError):
    pass


# --- File identity. -----------------------------------------------------------------------


@dataclass(frozen=True)
class DetectedType:
    """Milestone 3. CONFLICTS MUST BE SURFACED, NEVER SILENTLY RESOLVED ONE WAY."""

    declared_extension: str
    detected_kind: str | None  # a key into ATTACHMENT_MAGIC_BYTES, or None if undetermined
    conflict: bool
    double_extension_detected: bool
    hidden_extension_segment: str | None = None


class ActiveContentRisk(str, Enum):
    MACRO = "MACRO"
    SCRIPT = "SCRIPT"
    EMBEDDED_OBJECT = "EMBEDDED_OBJECT"
    EXTERNAL_REFERENCE = "EXTERNAL_REFERENCE"
    HTML_JS = "HTML_JS"
    PDF_ACTION = "PDF_ACTION"
    OFFICE_AUTOMATION = "OFFICE_AUTOMATION"
    EXECUTABLE = "EXECUTABLE"
    SHELL_SCRIPT = "SHELL_SCRIPT"
    INSTALLER = "INSTALLER"


@dataclass(frozen=True)
class ActiveContentSignal:
    risk: ActiveContentRisk
    detail: str
    source_path: str | None = None  # e.g. an archive member path, if applicable


@dataclass(frozen=True)
class ScanResult:
    """One inspection pass's outcome -- append-only, a fresh AttachmentIdentity.scan_results
    tuple entry per pass, never overwritten."""

    scanned_at: datetime
    detected_type: DetectedType | None
    active_content_signals: tuple[ActiveContentSignal, ...]
    verdict: str  # "clean" | "conflict" | "active_content" | "malicious" -- see risk.py


@dataclass(frozen=True)
class ParserFailure:
    exception_type: str
    message: str


@dataclass(frozen=True)
class ParserResult:
    parsed_at: datetime
    succeeded: bool
    failure: ParserFailure | None
    output_ref: str | None  # opaque reference to a ParserOutput, never the raw output itself


@dataclass
class AttachmentIdentity:
    """Mutable; owned/mutated only via app.attachment_chamber.service's functions (mirrors
    GuardianState's/SecurityIncident's "no public mutation outside the service module"
    discipline)."""

    attachment_id: uuid.UUID
    owner_id: uuid.UUID
    source: AttachmentSource
    ingest_timestamp: datetime
    size_bytes: int
    declared_mime: str | None
    extension: str
    content_hash: str
    original_filename: str
    normalized_filename: str
    quarantine_location: str  # a path RELATIVE to the bounded quarantine root, never absolute
    lifecycle_state: QuarantineState = QuarantineState.RECEIVED
    detected_mime: str | None = None
    parent_archive_id: uuid.UUID | None = None
    scan_results: tuple[ScanResult, ...] = ()
    parser_results: tuple[ParserResult, ...] = ()
    sanitization_status: str = "not_sanitized"
    provenance: str = "top-level upload"
    tracked_artifact_paths: tuple[str, ...] = ()  # every on-disk path ever created for this identity
    updated_at: datetime = field(default_factory=_utcnow)


@dataclass
class AttachmentEvent:
    """Hash-chained, append-only -- same discipline as Guardian's ContainmentReceipt chain
    and Sentinel's EventReceipt chain (see service.py's _event_hash/_record_event)."""

    event_id: uuid.UUID
    attachment_id: uuid.UUID
    event_type: str
    from_state: QuarantineState | None
    to_state: QuarantineState | None
    detail: dict[str, Any]
    prev_hash: str
    this_hash: str = ""
    created_at: datetime = field(default_factory=_utcnow)
