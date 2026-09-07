"""Attachment Chamber -- archive safety (Milestone 4).

Mirrors app.rag.zip_import's already-proven bounds and algorithmic approach EXACTLY (same
numbers, same shared-mutable-budget technique) -- that module solved this problem correctly
for its own (library-import) purpose; this is the general-purpose ingress-boundary version,
built fresh rather than importing zip_import's own private underscore-prefixed functions
across an unrelated package boundary (this is a NEW upstream trust boundary, not a
projection over existing rows).

ARCHIVE CONTENT != TRUSTED CONTENT: every member of an archive is itself just another
untrusted file -- this module's job is bounding the COST of finding that out (zip bombs,
path traversal, symlink entries, unbounded nesting), never deciding an archive's contents
are safe.
"""

from __future__ import annotations

import io
import time
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.attachment_chamber.path_safety import is_safe_relative_path
from app.attachment_chamber.types import AttachmentChamberError

# Identical to app.rag.zip_import's own bounds -- intentionally consistent, not re-derived
# or guessed. See that module's own module docstring for the measured-capacity reasoning
# behind these exact numbers.
MAX_FILES = 500
MAX_TOTAL_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_SINGLE_FILE_UNCOMPRESSED_BYTES = 25 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
MIN_COMPRESSED_SIZE_FOR_RATIO_CHECK = 256
MAX_NESTING_DEPTH = 3
CHUNK_SIZE = 64 * 1024

# NEW bound, not present in app.rag.zip_import (confirmed by reading it: it bounds bytes/
# files/depth/ratio but not wall-clock time) -- flagged here as a genuine improvement over
# that existing code, not copied from it. A maliciously crafted archive with many tiny,
# individually-legal entries could still cost excessive CPU/time even within the byte/file
# bounds; this closes that gap for THIS module. Worth backporting to zip_import.py in a
# future, separately-scoped round -- not done here (out of scope, that file is live
# production code this round must not modify).
MAX_PROCESSING_SECONDS = 30.0


class ArchiveSecurityError(AttachmentChamberError):
    """A violation severe enough to abort the whole archive inspection -- mirrors
    app.rag.zip_import.ZipSecurityError's own package-level-vs-per-entry distinction."""


@dataclass
class ArchiveEntryResult:
    member_path: str
    status: str  # "safe" | "rejected" | "skipped"
    reason: str = ""
    uncompressed_size: int = 0


@dataclass
class ArchiveInspectionResult:
    entries: tuple[ArchiveEntryResult, ...]
    total_uncompressed_bytes: int
    total_files: int
    max_nesting_depth_reached: int


@dataclass
class _ExtractionBudget:
    """Same shared-mutable-accumulator technique as app.rag.zip_import._ExtractionBudget:
    a SINGLE instance threaded through every recursive call, with no concept of "nesting
    level" at all -- this is what makes "small per level, huge in total" bombs structurally
    impossible, not merely unlikely."""

    __slots__ = ("total_uncompressed_bytes", "total_files", "started_at", "max_depth_reached")

    def __init__(self) -> None:
        self.total_uncompressed_bytes = 0
        self.total_files = 0
        self.started_at = time.monotonic()
        self.max_depth_reached = 0

    def check_time_budget(self) -> None:
        if time.monotonic() - self.started_at > MAX_PROCESSING_SECONDS:
            raise ArchiveSecurityError(f"archive inspection exceeded {MAX_PROCESSING_SECONDS}s -- aborting")


def _is_symlink_entry(info: "zipfile.ZipInfo") -> bool:
    """A zip entry can be marked as a symlink via its external_attr Unix mode bits (the
    upper 16 bits, matching how Unix zip tools store st_mode). S_ISLNK is octal 0o120000.
    Rejected outright -- never followed, regardless of what it points to."""
    unix_mode = info.external_attr >> 16
    return (unix_mode & 0o170000) == 0o120000


def _read_with_hard_cap(zf: zipfile.ZipFile, info: zipfile.ZipInfo, max_bytes: int) -> bytes:
    """Identical technique to app.rag.zip_import._read_with_hard_cap: streams in fixed-size
    chunks and aborts the INSTANT more than max_bytes has been produced -- never zf.read(info)
    outright, which would decompress the entire entry into memory first regardless of what
    the central directory claims about its size."""
    produced = 0
    chunks: list[bytes] = []
    with zf.open(info) as fh:
        while True:
            chunk = fh.read(CHUNK_SIZE)
            if not chunk:
                break
            produced += len(chunk)
            if produced > max_bytes:
                raise ArchiveSecurityError(f"{info.filename!r} decompresses to more than {max_bytes} bytes -- possible zip bomb or forged size metadata")
            chunks.append(chunk)
    return b"".join(chunks)


def inspect_zip_archive(archive_bytes: bytes, *, budget: "_ExtractionBudget | None" = None, depth: int = 0) -> ArchiveInspectionResult:
    """Top-level entry point. `budget` is None on the initial (depth=0) call and created
    here; a recursive nested-archive call passes the SAME budget instance it was given, so
    nested bytes/files always draw from the one shared total."""
    if budget is None:
        budget = _ExtractionBudget()
    if depth > MAX_NESTING_DEPTH:
        raise ArchiveSecurityError(f"archive nesting exceeds MAX_NESTING_DEPTH ({MAX_NESTING_DEPTH})")
    budget.max_depth_reached = max(budget.max_depth_reached, depth)

    results: list[ArchiveEntryResult] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(archive_bytes))
    except zipfile.BadZipFile as exc:
        raise ArchiveSecurityError(f"not a valid zip archive: {exc}") from exc

    with zf:
        seen_normalized_paths: set[str] = set()
        for info in zf.infolist():
            budget.check_time_budget()

            budget.total_files += 1
            if budget.total_files > MAX_FILES:
                raise ArchiveSecurityError(f"archive contains more than MAX_FILES ({MAX_FILES}) entries")

            name = info.filename
            if not is_safe_relative_path(name):
                results.append(ArchiveEntryResult(member_path=name, status="rejected", reason="unsafe path (traversal/absolute/drive-letter)"))
                continue

            if _is_symlink_entry(info):
                results.append(ArchiveEntryResult(member_path=name, status="rejected", reason="symlink entry -- never followed"))
                continue

            normalized = PurePosixPath(name.replace("\\", "/")).as_posix()
            if normalized in seen_normalized_paths:
                results.append(ArchiveEntryResult(member_path=name, status="rejected", reason="duplicate normalized path within archive -- possible overwrite attack"))
                continue
            seen_normalized_paths.add(normalized)

            if info.is_dir():
                continue

            declared_size = info.file_size
            if declared_size > MAX_SINGLE_FILE_UNCOMPRESSED_BYTES:
                results.append(ArchiveEntryResult(member_path=name, status="rejected", reason="declared size exceeds MAX_SINGLE_FILE_UNCOMPRESSED_BYTES", uncompressed_size=declared_size))
                continue

            compressed_size = info.compress_size
            if compressed_size >= MIN_COMPRESSED_SIZE_FOR_RATIO_CHECK and declared_size / max(compressed_size, 1) > MAX_COMPRESSION_RATIO:
                results.append(ArchiveEntryResult(member_path=name, status="rejected", reason="compression ratio exceeds MAX_COMPRESSION_RATIO -- possible zip bomb", uncompressed_size=declared_size))
                continue

            try:
                content = _read_with_hard_cap(zf, info, MAX_SINGLE_FILE_UNCOMPRESSED_BYTES)
            except (RuntimeError, NotImplementedError):
                results.append(ArchiveEntryResult(member_path=name, status="skipped", reason="unreadable (likely password-protected)"))
                continue

            budget.total_uncompressed_bytes += len(content)
            if budget.total_uncompressed_bytes > MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise ArchiveSecurityError(f"archive's total uncompressed size exceeds MAX_TOTAL_UNCOMPRESSED_BYTES ({MAX_TOTAL_UNCOMPRESSED_BYTES})")

            if content[:4] == b"PK\x03\x04" and depth < MAX_NESTING_DEPTH:
                nested = inspect_zip_archive(content, budget=budget, depth=depth + 1)
                results.append(ArchiveEntryResult(member_path=name, status="safe", reason=f"nested archive, {len(nested.entries)} inner entries", uncompressed_size=len(content)))
                continue

            results.append(ArchiveEntryResult(member_path=name, status="safe", uncompressed_size=len(content)))

    return ArchiveInspectionResult(
        entries=tuple(results), total_uncompressed_bytes=budget.total_uncompressed_bytes,
        total_files=budget.total_files, max_nesting_depth_reached=budget.max_depth_reached,
    )
