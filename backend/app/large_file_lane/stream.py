"""Bounded-memory streaming import with durable checkpoints.

A stored file is not indexed or understood. HTTP upload limits are unchanged.
5 GB is a target, not a measured capability.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

DEFAULT_CHUNK = 64 * 1024
TARGET_FILE_BYTES = 5 * 1024 * 1024 * 1024
ACCEPTANCE_FILE_COUNT = 5


def five_gb_not_supported() -> dict[str, object]:
    return {
        "five_gb_supported": False,
        "five_by_five_gb_measured": False,
        "stored_is_not_indexed": True,
        "http_upload_limit_unchanged": True,
        "target_file_bytes": TARGET_FILE_BYTES,
        "acceptance_file_count": ACCEPTANCE_FILE_COUNT,
        "first_cycle_go": False,
    }


@dataclass
class ImportCheckpoint:
    path: str
    offset: int
    sha256: str
    bytes_seen: int
    indexed: bool = False
    understood: bool = False


class StreamingImportLane:
    """Process a file in bounded chunks. Never load the whole blob."""

    def __init__(self, *, chunk_size: int = DEFAULT_CHUNK, max_buffer_chunks: int = 2) -> None:
        self.chunk_size = chunk_size
        self.max_buffer_bytes = chunk_size * max_buffer_chunks
        self.peak_buffer = 0

    def process(
        self,
        source: BinaryIO | Path,
        *,
        checkpoint: ImportCheckpoint | None = None,
        on_chunk: Callable[[bytes, ImportCheckpoint], None] | None = None,
    ) -> ImportCheckpoint:
        handle = source if hasattr(source, "read") else Path(source).open("rb")
        close = handle is not source
        hasher = hashlib.sha256()
        offset = 0
        if checkpoint is not None:
            offset = checkpoint.offset
            if checkpoint.sha256:
                hasher = hashlib.sha256()
            handle.seek(offset)
        path = str(getattr(source, "name", checkpoint.path if checkpoint else "stream"))
        bytes_seen = checkpoint.bytes_seen if checkpoint else 0
        try:
            while True:
                chunk = handle.read(self.chunk_size)
                if not chunk:
                    break
                self.peak_buffer = max(self.peak_buffer, len(chunk))
                if self.peak_buffer > self.max_buffer_bytes:
                    raise MemoryError("streaming import exceeded bounded buffer")
                hasher.update(chunk)
                bytes_seen += len(chunk)
                offset += len(chunk)
                state = ImportCheckpoint(
                    path=path,
                    offset=offset,
                    sha256=hasher.hexdigest(),
                    bytes_seen=bytes_seen,
                    indexed=False,
                    understood=False,
                )
                if on_chunk is not None:
                    on_chunk(chunk, state)
            return ImportCheckpoint(
                path=path,
                offset=offset,
                sha256=hasher.hexdigest(),
                bytes_seen=bytes_seen,
                indexed=False,
                understood=False,
            )
        finally:
            if close:
                handle.close()

    def stored_vs_understood(self, checkpoint: ImportCheckpoint) -> dict[str, object]:
        stored = checkpoint.bytes_seen > 0
        return {
            "stored": stored,
            "indexed": checkpoint.indexed,
            "understood": checkpoint.understood,
            "peak_buffer_bytes": self.peak_buffer,
            "bounded": self.peak_buffer <= self.max_buffer_bytes,
            **five_gb_not_supported(),
        }
