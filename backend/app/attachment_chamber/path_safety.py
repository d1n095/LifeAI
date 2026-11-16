"""Attachment Chamber -- path safety (Milestone 2).

Mirrors app.storage.local_fs's already-hardened approach (content-addressed storage, strict
key regex, resolve()+relative_to() containment check, symlink rejection) and
app.rag.zip_import's already-proven `_is_safe_member_name()` (PurePosixPath(...).parts
segment check, never a substring search for ".." -- that exact "substring match instead of
exact/segment match" bug shape has recurred elsewhere in this codebase's history and is not
repeated here).

FILE RECEIVED != FILE TRUSTED: nothing in this module trusts a caller-supplied filename or
path for anything beyond display -- every actual on-disk path is derived from a validated,
owner-scoped, attachment_id-keyed layout, never from untrusted input directly.
"""

from __future__ import annotations

import os
import stat
import unicodedata
import uuid
from pathlib import Path, PurePosixPath

from app.attachment_chamber.types import AttachmentChamberError


class UnsafePathError(AttachmentChamberError):
    pass


# Windows reserved device names -- this backend is POSIX-only (matching app.storage.local_fs's
# own documented scope), but quarantine metadata (original_filename/normalized_filename) may
# end up referenced by future cross-platform tooling, so these are normalized away defensively
# even though the local filesystem itself would not choke on them today.
_RESERVED_WINDOWS_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
)


def is_safe_relative_path(name: str) -> bool:
    """Same algorithm as app.rag.zip_import._is_safe_member_name(): rejects absolute paths
    (POSIX or Windows drive-letter/UNC), and any path SEGMENT equal to ".." -- checked via
    PurePosixPath(...).parts, never a naive substring search (which would false-positive on
    a legitimate filename like "my..file.pdf")."""
    if not name or name.strip() == "":
        return False
    if name.startswith("/") or name.startswith("\\"):
        return False
    if len(name) >= 2 and name[1] == ":":  # C:\... or C:/...
        return False
    if name.startswith("\\\\"):  # \\server\share UNC path
        return False
    parts = PurePosixPath(name.replace("\\", "/")).parts
    return ".." not in parts


def normalize_filename(name: str) -> str:
    """Unicode-normalizes (NFC) before any comparison/sanitization -- a raw-bytes-vs-
    normalized-form mismatch is a real, documented Unicode-security bug shape (two visually
    identical filenames that compare unequal, or a combining-character sequence that hides
    a different effective name than what's displayed). Takes only the final path component
    (mirrors app.rag.zip_import._sanitize_outer_filename()'s exact reasoning: strips any
    directory prefix, including "..", any absolute-path prefix, and any separator that could
    form a forged boundary), then strips a leading "." (hidden-file marker) for the
    NORMALIZED name specifically -- the ORIGINAL name is preserved verbatim on
    AttachmentIdentity.original_filename for display/audit, this function only ever produces
    the safe, on-disk-safe NORMALIZED name."""
    normalized_form = unicodedata.normalize("NFC", name)
    posix_style = normalized_form.replace("\\", "/")
    base = PurePosixPath(posix_style).name or "attachment"
    while base.startswith("."):
        base = base[1:]
    if not base:
        base = "attachment"
    stem = PurePosixPath(base).stem
    if stem.upper() in _RESERVED_WINDOWS_NAMES:
        base = f"_{base}"
    return base


def detect_double_extension(filename: str) -> tuple[str, str | None]:
    """Returns (last_extension, hidden_extension_segment). The extension used for any
    type-based decision must be the LAST suffix; an earlier "hidden" extension segment
    (e.g. "invoice.pdf.exe" -> last=".exe", hidden=".pdf") is surfaced as a signal, never
    silently treated as the real type -- neither one alone is trusted."""
    path = PurePosixPath(filename)
    suffixes = path.suffixes
    if not suffixes:
        return "", None
    last = suffixes[-1].lower()
    hidden = suffixes[-2].lower() if len(suffixes) >= 2 else None
    return last, hidden


def canonical_case(name: str) -> str:
    """For COMPARISON purposes only (case-folding collision detection on a hypothetical
    future case-insensitive filesystem) -- never used to construct an actual on-disk path or
    for display; the original casing is always preserved on AttachmentIdentity.original_filename."""
    return name.casefold()


def quarantine_relative_location(*, owner_id: uuid.UUID, attachment_id: uuid.UUID) -> str:
    """The ONLY function that produces a quarantine_location. Deliberately ignores the
    original filename entirely -- the on-disk path is owner_id/attachment_id-keyed, exactly
    like app.storage.local_fs's own content-addressed keys have no user-controlled path
    component at all. This is what makes path traversal structurally impossible here: there
    is no untrusted string anywhere in the path."""
    return f"{owner_id}/{attachment_id}"


def resolve_quarantine_path(quarantine_root: Path, relative_location: str) -> Path:
    """Same containment discipline as app.storage.local_fs._resolve(): resolve() the
    candidate, then relative_to() the resolved root -- catches an intermediate symlink
    escaping the root. A final-component symlink is rejected separately (belt-and-suspenders,
    matching local_fs's own is_symlink() check + O_NOFOLLOW-at-open-time pattern)."""
    if not is_safe_relative_path(relative_location):
        raise UnsafePathError(f"quarantine location {relative_location!r} is not a safe relative path")
    root_resolved = quarantine_root.resolve()
    candidate = (quarantine_root / relative_location).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        raise UnsafePathError("quarantine location resolves outside the quarantine root") from None
    if candidate.is_symlink():
        raise UnsafePathError("quarantine location is a symlink, which is not allowed")
    return candidate


def is_hardlinked_elsewhere(path: Path) -> bool:
    """A hardlinked file shares an inode with something outside quarantine -- st_nlink > 1
    means at least one other directory entry points at the same inode. This alone cannot
    prove the OTHER entry is outside quarantine, but a freshly-written quarantine file
    should always have st_nlink == 1; anything else is a real anomaly worth flagging/
    refusing rather than silently trusting."""
    try:
        return os.stat(path, follow_symlinks=False).st_nlink > 1
    except FileNotFoundError:
        return False


def is_regular_file_not_symlink(path: Path) -> bool:
    """Kernel-level check (matching local_fs.py's O_NOFOLLOW-at-open-time defense-in-depth)
    -- used right before any read, independent of the earlier resolve()-time symlink check,
    to close the TOCTOU gap between validation and actual open."""
    try:
        st = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(st.st_mode)
