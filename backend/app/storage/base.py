"""Persistent original-file storage abstraction (Life Library durable-worker package).

Only `LocalFilesystemStorage` (app/storage/local_fs.py) is implemented — a single private
Docker volume on the VPS, per this package's explicit scope. The abstraction exists so a
future backend (object storage, etc.) is a new class implementing this interface, not a
rewrite of every caller (app/rag/library_import.py, app/worker.py, app/routers/library.py).

Every implementation must uphold the same contract:
  - `write_stream` never buffers the whole input in memory — it streams chunk by chunk,
    computing size/sha256 as it goes, and aborts as soon as `max_bytes` is exceeded.
  - The final blob is only visible at its content-addressed key (sha256-based) after a
    verified, atomic write — a caller can never observe a partially-written blob.
  - `storage_key` values are never derived from user-controlled filenames — see
    LocalFilesystemStorage's key-validation for the path-traversal/symlink defense this
    enables.
  - Stored bytes are always the ORIGINAL content (never derived indexes/chunks). Future
    compression or encryption are separate envelope/metadata concerns applied around this
    layer — encryption is not compression, and neither changes the content hash of the
    underlying source bytes this interface addresses.
  - `sha256` / `Document.checksum` always identify canonical plaintext source bytes, never
    stored envelope bytes — one invariant for dedup, provenance, and future key rotation.
  - `open_read()` returns decrypted plaintext to callers today (whole-blob). Future chunk/
    range reads and envelope metadata (format version, compression scheme, key reference id)
    belong in this interface or a decorator implementing it — not scattered in import callers.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import BinaryIO, Callable


class StorageError(Exception):
    """Base class for every storage-layer failure."""


class InvalidStorageKey(StorageError):
    """A storage_key failed validation (wrong shape, path traversal, or a symlink) —
    always a permanent, non-retryable error; the key itself is what's wrong, not the
    storage backend's availability."""


class StorageIntegrityError(StorageError):
    """A blob's actual bytes don't match its recorded checksum/size — either real disk
    corruption or a content-addressing collision so astronomically unlikely it's treated as
    corruption. Permanent, non-retryable."""


class StorageSizeLimitExceeded(StorageError):
    """Raised mid-stream the moment more than `max_bytes` has been read — the caller must
    have stopped reading further input by the time this is raised (see
    LocalFilesystemStorage.write_stream). Permanent, non-retryable: retrying with the same
    oversized input will exceed the limit again every time."""


@dataclass(frozen=True)
class StoredBlob:
    storage_key: str
    sha256: str  # SHA-256 of canonical PLAINTEXT source bytes — dedup/provenance identity.
    size_bytes: int  # Plaintext byte length; never encrypted/compressed envelope size.


class StorageBackend(ABC):
    @abstractmethod
    def write_stream(self, read_chunk: Callable[[], bytes], *, max_bytes: int, chunk_size: int = 1 << 20) -> StoredBlob:
        """Streams from `read_chunk()` (called repeatedly; an empty bytes return means EOF)
        into durable storage, computing size and sha256 incrementally. Raises
        StorageSizeLimitExceeded the instant the running total exceeds `max_bytes`, without
        ever holding more than `chunk_size` bytes of the input in memory at once. The
        returned StoredBlob's `storage_key` is only valid to read once this call has
        returned successfully — a caller must never persist a storage_key for a write that
        raised."""

    @abstractmethod
    def open_read(self, storage_key: str) -> BinaryIO:
        """Opens an existing blob for reading. Raises InvalidStorageKey for a malformed/
        unsafe key, FileNotFoundError if the key is well-formed but nothing is stored there."""

    @abstractmethod
    def exists(self, storage_key: str) -> bool:
        """False for a malformed/unsafe key too — never raises for "not found" the way
        open_read does, since callers use this specifically to check before deciding
        whether to treat a document as missing/corrupted (see app/worker.py's recovery
        pass)."""

    @abstractmethod
    def delete(self, storage_key: str) -> None:
        """Idempotent: deleting an already-absent (or never-existing, but validly-shaped)
        key is not an error — physical cleanup can safely run twice. Raises InvalidStorageKey
        for a malformed/unsafe key, since that's a caller bug worth surfacing loudly rather
        than silently no-op-ing."""

    @abstractmethod
    def verify(self, storage_key: str, *, expected_sha256: str, expected_size: int | None = None) -> bool:
        """Re-reads the blob and confirms its actual sha256 (and, if given, size) match what
        the database recorded — the recovery-time integrity check (DEL 6's "kontrollera att
        storage_key faktiskt finns och checksumma stämmer"). False (not an exception) for a
        missing file or a mismatch; exceptions are reserved for malformed keys."""
