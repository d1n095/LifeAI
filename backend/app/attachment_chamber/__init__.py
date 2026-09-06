"""Attachment Chamber (MainAI V2): a hostile-file ingress trust boundary.

Standalone, isolated, NOT imported by any production runtime path (no app.main import, no
app.guardian/app.privacy_boundary/app.sentinel/app.sovereign_identity/app.life_recovery/
app.operating_shell import from this package). See docs/mainai_v2/
MAINAI_V2_FILE_INGEST_ATTACHMENT_CHAMBER.md for the design.
"""

from app.attachment_chamber.path_safety import (
    UnsafePathError,
    canonical_case,
    detect_double_extension,
    is_hardlinked_elsewhere,
    is_regular_file_not_symlink,
    is_safe_relative_path,
    normalize_filename,
    quarantine_relative_location,
    resolve_quarantine_path,
)
from app.attachment_chamber.service import (
    AttachmentChamberState,
    events_for_attachment,
    new_chamber_state,
    receive_attachment,
    track_artifact,
    transition_quarantine_state,
    verify_receipt_chain_intact,
)
from app.attachment_chamber.service import from_snapshot as identity_from_snapshot
from app.attachment_chamber.service import to_snapshot as identity_to_snapshot
from app.attachment_chamber.types import (
    QUARANTINE_TRANSITIONS,
    TERMINAL_QUARANTINE_STATES,
    ActiveContentRisk,
    ActiveContentSignal,
    AttachmentChamberError,
    AttachmentEvent,
    AttachmentIdentity,
    AttachmentSource,
    DetectedType,
    InvalidQuarantineTransitionError,
    MalformedSnapshotError,
    NotFoundError,
    OwnerMismatchError,
    ParserFailure,
    ParserResult,
    QuarantineState,
    ScanResult,
    TerminalQuarantineStateError,
)

__all__ = [
    "QUARANTINE_TRANSITIONS",
    "TERMINAL_QUARANTINE_STATES",
    "ActiveContentRisk",
    "ActiveContentSignal",
    "AttachmentChamberError",
    "AttachmentChamberState",
    "AttachmentEvent",
    "AttachmentIdentity",
    "AttachmentSource",
    "DetectedType",
    "InvalidQuarantineTransitionError",
    "MalformedSnapshotError",
    "NotFoundError",
    "OwnerMismatchError",
    "ParserFailure",
    "ParserResult",
    "QuarantineState",
    "ScanResult",
    "TerminalQuarantineStateError",
    "UnsafePathError",
    "canonical_case",
    "detect_double_extension",
    "events_for_attachment",
    "identity_from_snapshot",
    "identity_to_snapshot",
    "is_hardlinked_elsewhere",
    "is_regular_file_not_symlink",
    "is_safe_relative_path",
    "new_chamber_state",
    "normalize_filename",
    "quarantine_relative_location",
    "receive_attachment",
    "resolve_quarantine_path",
    "track_artifact",
    "transition_quarantine_state",
    "verify_receipt_chain_intact",
]
