"""Local, private-VPS-volume storage backend — see app/storage/base.py for the interface
contract this implements.

Content-addressed by design: a blob's storage_key is always `{sha256[:2]}/{sha256}`, derived
purely from its own bytes, NEVER from a user-controlled filename — the concrete defense
against path traversal (there is no user input in the path at all) and the mechanism that
makes reference-counted deduplication trivial (identical content from two different
documents/jobs always resolves to the exact same key, see app/rag/library_import.py's
`maybe_purge_blob`).
"""

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import BinaryIO, Callable

from app.storage.base import InvalidStorageKey, StorageBackend, StorageIntegrityError, StorageSizeLimitExceeded, StoredBlob

_KEY_PATTERN = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{64}$")


class LocalFilesystemStorage(StorageBackend):
    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "tmp").mkdir(parents=True, exist_ok=True)

    def _resolve(self, storage_key: str) -> Path:
        # Validated BEFORE any filesystem access, on every call — write_stream constructs
        # keys itself (always safe by construction) but every other method receives a key
        # read back from the database, which this validates just as strictly, so a
        # corrupted/tampered DB row can never turn into a path-traversal read/delete.
        if not _KEY_PATTERN.match(storage_key):
            raise InvalidStorageKey(f"Ogiltig storage-nyckel: {storage_key!r}")
        root_resolved = self.root.resolve()
        candidate = (self.root / storage_key).resolve()
        try:
            candidate.relative_to(root_resolved)
        except ValueError:
            raise InvalidStorageKey("Storage-nyckeln pekar utanför lagringsroten.") from None
        # Path.resolve() already follows symlinks when computing `candidate` — the
        # containment check above is what catches an intermediate symlink escaping the root.
        # This second check specifically blocks the case where the final path component
        # itself is a symlink (e.g. planted at a key's expected on-disk location before the
        # real blob was ever written there) — belt-and-suspenders alongside O_NOFOLLOW at
        # actual open time in open_read().
        if candidate.is_symlink():
            raise InvalidStorageKey("Storage-nyckeln pekar på en symbolisk länk, vilket inte är tillåtet.")
        return candidate

    def write_stream(self, read_chunk: Callable[[], bytes], *, max_bytes: int, chunk_size: int = 1 << 20) -> StoredBlob:
        tmp_dir = self.root / "tmp"
        fd, tmp_name = tempfile.mkstemp(dir=tmp_dir, prefix="upload-")
        tmp_path = Path(tmp_name)
        hasher = hashlib.sha256()
        size = 0
        wrote_final = False
        try:
            with os.fdopen(fd, "wb") as f:
                while True:
                    chunk = read_chunk()
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise StorageSizeLimitExceeded(
                            f"Uppladdningen överskrider maxstorleken ({max_bytes} bytes) — avbruten."
                        )
                    hasher.update(chunk)
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())

            sha256_hex = hasher.hexdigest()
            final_key = f"{sha256_hex[:2]}/{sha256_hex}"
            final_path = self._resolve(final_key)
            final_path.parent.mkdir(parents=True, exist_ok=True)

            if final_path.exists():
                # Content-addressed dedup: identical bytes are already durably stored (by
                # this or any other document/job) — verify the existing blob's size agrees
                # as a cheap corruption check, then discard this temp copy instead of
                # writing a second identical file.
                if final_path.stat().st_size != size:
                    raise StorageIntegrityError(
                        f"En blob med samma sha256 ({sha256_hex}) finns redan men har fel storlek — misstänkt korruption."
                    )
            else:
                os.chmod(tmp_path, 0o600)
                os.rename(tmp_path, final_path)  # atomic: tmp/ and the blob dir share self.root's filesystem
                wrote_final = True

            return StoredBlob(storage_key=final_key, sha256=sha256_hex, size_bytes=size)
        finally:
            if not wrote_final:
                tmp_path.unlink(missing_ok=True)

    def open_read(self, storage_key: str) -> BinaryIO:
        path = self._resolve(storage_key)
        # O_NOFOLLOW: refuse to open if the final path component is a symlink, independent
        # of and in addition to _resolve()'s own symlink check above (defense in depth —
        # this one is enforced atomically by the kernel at open() time, closing any
        # theoretical TOCTOU gap between the check and the open).
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        return os.fdopen(fd, "rb")

    def exists(self, storage_key: str) -> bool:
        try:
            path = self._resolve(storage_key)
        except InvalidStorageKey:
            return False
        return path.is_file()

    def delete(self, storage_key: str) -> None:
        path = self._resolve(storage_key)
        path.unlink(missing_ok=True)

    def verify(self, storage_key: str, *, expected_sha256: str, expected_size: int | None = None) -> bool:
        try:
            path = self._resolve(storage_key)
        except InvalidStorageKey:
            return False
        if not path.is_file():
            return False
        if expected_size is not None and path.stat().st_size != expected_size:
            return False
        hasher = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                hasher.update(chunk)
        return hasher.hexdigest() == expected_sha256
