"""Local, private-VPS-volume storage backend — see app/storage/base.py for the interface
contract this implements.

Content-addressed by design: a blob's storage_key is always `{sha256[:2]}/{sha256}`, derived
purely from its own bytes, NEVER from a user-controlled filename — the concrete defense
against path traversal (there is no user input in the path at all) and the mechanism that
makes reference-counted deduplication trivial (identical content from two different
documents/jobs always resolves to the exact same key, see app/rag/library_import.py's
`maybe_purge_blob`).

Pass 31 (a sixth founder review round): `write_stream()`'s publish step used to be

    if final_path.exists():
        verify size, raise on mismatch
    else:
        os.rename(tmp_path, final_path)

which has a real TOCTOU against a concurrent `delete()`'s `unlink()`: if the check observes
"already exists" but a concurrent delete removes it a moment later, this method skips writing
its OWN tmp file to `final_path` at all (believing an existing copy already covers it) and
returns a `StoredBlob` whose `storage_key` no longer resolves to anything on disk. The
DB-level advisory lock (`app/storage/references.py::acquire_storage_key_lock`) cannot close
this: content-addressing means the key isn't even known until the bytes are fully hashed, so
callers structurally cannot take that lock before `write_stream()` runs, and this race is
entirely BETWEEN two raw filesystem calls, never touching the database at all.

Closed via `os.link()` (a hardlink) as the PRIMARY publish mechanism instead of a plain
existence check: `link(2)` is atomic and fails with `FileExistsError` if and only if something
is ALREADY at the destination at the exact instant of the syscall — there is no "check, then
separately act" gap to race a concurrent `unlink()` into. If it succeeds, this call's own
bytes are, atomically and unambiguously, the published blob; no verification needed. If it
fails because something is already there, the existing file's size is checked (the same cheap
corruption check as before).

Pass 32 (a seventh founder review round): Pass 31's own docstring admitted the size-check
branch above still wasn't atomic against a concurrent `delete()` — the founder confirmed this
is real and required an actual OS-level mutual-exclusion primitive, not another retry loop.
`_key_lock()` below is that primitive: a real `fcntl.flock()` held by `write_stream()` for
`_publish()`'s entire body AND by `delete()` around its own `unlink()`, so the two can never
interleave for the same shard anymore — see `_key_lock()`'s docstring for why it locks per
two-hex-character shard (the same directory sharding blobs already use) rather than per exact
sha256, and why the lock files are never deleted. This is a SEPARATE, narrower lock than
`app/storage/references.py::acquire_storage_key_lock()` (a DB advisory lock): this one
protects raw filesystem publish/unlink from each other; the DB lock protects the
reference-check + DB-commit decision from a concurrent physical delete. Both are still
necessary — a legitimate delete can still fully complete in the gap between a persistent
writer's `write_stream()` return and that same writer's own later DB-lock acquisition, which
is exactly why every persistent writer (`app/storage/references.py::
store_content_with_reference_lock()`, `app/rag/library_import.py::
_store_bytes_with_reference_lock()`) must hold the DB lock from verification through the
reference commit, not just rely on this filesystem lock."""

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Callable, Iterator

from app.storage.base import (
    InvalidStorageKey,
    StorageBackend,
    StorageError,
    StorageIntegrityError,
    StorageSizeLimitExceeded,
    StagedBlob,
    StoredBlob,
)

_KEY_PATTERN = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{64}$")
_STAGING_KEY_PATTERN = re.compile(
    r"^staging/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


def _hash_file(path: Path) -> str:
    """The same incremental sha256 `verify()` already used, factored out so `_publish()`
    (Pass 32, an eighth founder review round) can use the identical hashing logic to check an
    already-present, same-size dedup candidate's ACTUAL content -- not just its size -- before
    accepting it as a match for the key it sits at."""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _fsync_dir(path: Path) -> None:
    """fsyncs a directory's own metadata (its entries), not any file's contents -- see
    `_publish()`'s docstring for why a fresh `os.link()` needs this for real crash durability.
    Directories can only be opened read-only; `os.fsync` on that fd still flushes the
    directory's own on-disk data. POSIX/Linux only, matching this backend's own "private VPS
    volume" scope (see module docstring) -- no Windows fallback needed."""
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class LocalFilesystemStorage(StorageBackend):
    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "tmp").mkdir(parents=True, exist_ok=True)
        (self.root / "locks").mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _key_lock(self, shard: str) -> Iterator[None]:
        """Real, OS-level mutual exclusion between `_publish()` and `delete()` for every blob
        sharing `shard` (the same two-hex-character prefix already used for the blob directory
        layout) -- an `fcntl.flock()` on a small, dedicated lock file, NEVER the blob itself.

        Locked per SHARD, not per exact sha256: bounds the lock directory to at most 256
        files total (one per possible two-hex-char prefix), created lazily and never removed.
        Deliberately never deleted -- unlinking a lock file while another holder still has it
        open would let a second, unrelated fd (and therefore a second, independent flock) get
        created for what callers believe is "the same" lock, silently reintroducing the exact
        race this exists to close. A per-exact-sha256 lock file would need the same
        never-delete rule to be safe, but would then grow forever (one file per distinct
        content hash ever uploaded, even after the blob itself is purged); reusing the
        existing 256-way shard keeps this bounded without needing any cleanup logic at all.
        The coarser granularity only costs unrelated same-shard keys some avoidable
        contention, never correctness.

        Always the innermost, shortest-held lock on every call path in this codebase, and its
        critical section never touches the database -- so it can never participate in a
        lock-ordering cycle with `acquire_storage_key_lock()`'s DB advisory lock. `delete()`
        callers already hold that DB lock (an outer lock, held across the reference-check +
        commit decision) before calling `delete()`, which then takes this filesystem lock only
        around the `unlink()` itself -- DB-lock-then-fs-lock. `write_stream()`'s own use of
        this lock happens with no DB lock held at all (content-addressing means the key isn't
        even known until the bytes are fully hashed, so callers structurally cannot hold the DB
        lock yet) and releases it before returning, well before any caller could go on to take
        the DB lock -- so the reverse order (fs-lock-then-block-on-db-lock) never happens
        anywhere, which is what would be needed for a deadlock cycle."""
        lock_dir = self.root / "locks"
        lock_path = lock_dir / f"{shard}.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

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

    def _resolve_staging(self, staging_key: str) -> Path:
        match = _STAGING_KEY_PATTERN.fullmatch(staging_key)
        if match is None:
            raise InvalidStorageKey("Ogiltig staging-nyckel.")
        owner_id, job_id = match.groups()
        root_resolved = self.root.resolve()
        candidate = self.root / "staging" / owner_id / job_id
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root_resolved)
        except ValueError:
            raise InvalidStorageKey("Staging-nyckeln pekar utanför lagringsroten.") from None
        if candidate.is_symlink() or candidate.parent.is_symlink():
            raise InvalidStorageKey("Staging-nyckeln pekar på en symbolisk länk.")
        return candidate

    @contextmanager
    def _staging_lock(self, staging_key: str) -> Iterator[tuple[str, Path]]:
        match = _STAGING_KEY_PATTERN.fullmatch(staging_key)
        if match is None:
            raise InvalidStorageKey("Ogiltig staging-nyckel.")
        owner_id, job_id = match.groups()
        lock_dir = self.root / "locks"
        lock_path = lock_dir / f"staging-{owner_id[:2]}.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield job_id, self._resolve_staging(staging_key)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def trusted_sha256_for_key(self, storage_key: str) -> str | None:
        """Extract the integrity anchor only from this backend's exact canonical key shape.

        ``write_stream`` is the only producer of storage keys and always uses
        ``{digest[:2]}/{digest}``.  Requiring the shard to agree with the digest is important:
        a merely regex-shaped database value is not evidence that the backend produced it.
        No filesystem path is resolved and no user-controlled filename participates.
        """
        if not _KEY_PATTERN.fullmatch(storage_key):
            return None
        shard, digest = storage_key.split("/", 1)
        if shard != digest[:2]:
            return None
        return digest

    def write_stream(self, read_chunk: Callable[[], bytes], *, max_bytes: int, chunk_size: int = 1 << 20) -> StoredBlob:
        tmp_dir = self.root / "tmp"
        fd, tmp_name = tempfile.mkstemp(dir=tmp_dir, prefix="upload-")
        tmp_path = Path(tmp_name)
        hasher = hashlib.sha256()
        size = 0
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
            os.chmod(tmp_path, 0o600)

            with self._key_lock(sha256_hex[:2]):
                self._publish(tmp_path, final_path, size, sha256_hex)

            return StoredBlob(storage_key=final_key, sha256=sha256_hex, size_bytes=size)
        finally:
            # Pass 31: unconditional now (was `if not wrote_final`) -- os.link() (the new
            # publish primitive below) never consumes/removes its source the way os.rename()
            # used to; the temp file always needs its own separate cleanup regardless of
            # whether _publish() ended up using it as the final blob or discarding it in
            # favor of an already-present, verified-identical one.
            tmp_path.unlink(missing_ok=True)

    def write_staged_stream(
        self, staging_key: str, read_chunk: Callable[[], bytes], *, max_bytes: int, chunk_size: int = 1 << 20
    ) -> StagedBlob:
        with self._staging_lock(staging_key) as (job_id, final_path):
            existing = self._inspect_staged_unlocked(staging_key)
            if existing is not None:
                return existing

            tmp_path = self.root / "tmp" / f"staging-{job_id}.tmp"
            hasher = hashlib.sha256()
            size = 0
            try:
                shutil.rmtree(tmp_path, ignore_errors=True)
                tmp_path.mkdir(mode=0o700)
                content_path = tmp_path / "content"
                with open(content_path, "wb") as stream:
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
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())

                digest = hasher.hexdigest()
                metadata_path = tmp_path / "manifest.json"
                metadata_path.write_text(json.dumps({"sha256": digest, "size_bytes": size}, separators=(",", ":")))
                os.chmod(metadata_path, 0o600)
                with open(metadata_path, "rb") as metadata:
                    os.fsync(metadata.fileno())
                _fsync_dir(tmp_path)
                final_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp_path, final_path)
                _fsync_dir(final_path.parent)
                return StagedBlob(staging_key, digest, size)
            finally:
                try:
                    shutil.rmtree(tmp_path)
                except FileNotFoundError:
                    pass

    def _inspect_staged_unlocked(self, staging_key: str) -> StagedBlob | None:
        path = self._resolve_staging(staging_key)
        if not path.exists():
            return None
        manifest_path = path / "manifest.json"
        content_path = path / "content"
        try:
            if manifest_path.is_symlink() or content_path.is_symlink():
                raise StorageIntegrityError("Staged original contains an unexpected symbolic link.")
            metadata = json.loads(manifest_path.read_text())
            expected_sha256 = metadata["sha256"]
            expected_size = metadata["size_bytes"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StorageIntegrityError("Staging metadata is missing or invalid.") from exc
        if (
            not isinstance(expected_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
            or not isinstance(expected_size, int)
            or expected_size < 0
            or content_path.stat().st_size != expected_size
            or _hash_file(content_path) != expected_sha256
        ):
            raise StorageIntegrityError("Staged original failed hash/size verification.")
        return StagedBlob(staging_key, expected_sha256, expected_size)

    def inspect_staged(self, staging_key: str) -> StagedBlob | None:
        with self._staging_lock(staging_key):
            return self._inspect_staged_unlocked(staging_key)

    def verify_staged(self, staging_key: str, *, expected_sha256: str, expected_size: int | None = None) -> bool:
        try:
            staged = self.inspect_staged(staging_key)
        except (InvalidStorageKey, StorageIntegrityError):
            return False
        if staged is None:
            return False
        return staged.sha256 == expected_sha256 and (expected_size is None or staged.size_bytes == expected_size)

    def open_staged_read(self, staging_key: str) -> BinaryIO:
        if self.inspect_staged(staging_key) is None:
            raise FileNotFoundError(staging_key)
        path = self._resolve_staging(staging_key)
        if (path / "content").is_symlink():
            raise InvalidStorageKey("Staged content cannot be a symbolic link.")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path / "content", flags)
        return os.fdopen(fd, "rb")

    def delete_staged(self, staging_key: str) -> None:
        with self._staging_lock(staging_key) as (job_id, path):
            for candidate in (path, self.root / "tmp" / f"staging-{job_id}.tmp"):
                try:
                    shutil.rmtree(candidate)
                except FileNotFoundError:
                    pass

    def _publish(self, tmp_path: Path, final_path: Path, size: int, sha256_hex: str) -> None:
        """Makes `final_path` durably, verifiably contain `tmp_path`'s bytes -- either by
        atomically publishing THIS call's own tmp file there, or by confirming an
        already-present file there genuinely has the right content. See this module's own
        docstring for the TOCTOU this replaces.

        Pass 32: the caller (`write_stream()`) now holds `_key_lock(sha256_hex[:2])` for this
        method's entire body, and `delete()` holds the exact same lock around its own
        `unlink()` -- the two can never interleave anymore, so once `os.link()` fails with
        `FileExistsError`, whatever is at `final_path` cannot vanish out from under the
        `stat()` below before the size check completes. Pass 31's version of this method had
        to retry in a loop specifically because no such lock existed yet; that loop is gone
        now that real mutual exclusion makes the race it defended against structurally
        impossible from anything this process does.

        Directory fsync after linking: `os.link()` updates an in-memory directory entry that
        is not guaranteed to survive an unclean shutdown until the directory itself is
        fsynced -- the same durability caveat that already motivated fsyncing the temp file's
        own data above. Only done on the branch that actually changed the directory (a fresh
        link); reusing an already-present, previously-published file has nothing new to
        flush.

        Pass 32 (an eighth founder review round): the SAME-SIZE dedup branch below used to
        accept an existing file purely on a size match -- a founder review correctly pointed
        out this breaks content-addressing's own contract: a file with the right path and the
        right size but the WRONG bytes (disk corruption, or a manual out-of-band edit) would
        be silently treated as "the" blob for `sha256_hex`, and every future reader would get
        back bytes that don't hash to the key they asked for. Now hashes the existing file
        (`_hash_file()`) and, on a mismatch, REPAIRS it: `tmp_path` is this call's own
        freshly-hashed, known-correct bytes, and this method holds the shard lock for its
        entire body, so atomically overwriting whatever is at `final_path` with `tmp_path`
        (`os.replace()`, not `os.link()` -- the destination already exists) is safe. Re-hashes
        after repairing rather than trusting the overwrite blindly; a repair that still
        doesn't match (a write error, or some other application-level bug) raises
        `StorageIntegrityError` rather than pretending it succeeded. A genuine SIZE mismatch
        is left exactly as before -- immediately fatal, no repair attempt -- since that shape
        (same sha256, different length) was never expected to legitimately recur and Pass 31's
        own tests already lock in "fail immediately, never silently heal" for it."""
        try:
            os.link(tmp_path, final_path)
            _fsync_dir(final_path.parent)
            return  # our own bytes are now durably, atomically the published blob
        except FileExistsError:
            pass

        try:
            existing_size = final_path.stat().st_size
        except FileNotFoundError as exc:
            # Cannot happen from anything this application does under the lock -- delete()
            # holds the identical shard lock around its own unlink(). Only reachable via
            # something entirely outside this process's control (e.g. manual filesystem
            # interference on the host) -- fail loudly rather than silently guessing.
            raise StorageError(
                f"Blob {sha256_hex} försvann oväntat under publicering (utanför processens kontroll)."
            ) from exc

        if existing_size != size:
            # Content-addressed dedup: identical bytes are already durably stored (by this or
            # any other document/job) — verify the existing blob's size agrees as a cheap
            # corruption check.
            raise StorageIntegrityError(
                f"En blob med samma sha256 ({sha256_hex}) finns redan men har fel storlek — misstänkt korruption."
            )

        if _hash_file(final_path) == sha256_hex:
            return  # genuinely identical content already durably published -- true dedup

        # Right path, right size, WRONG bytes -- repair from this call's own known-correct
        # tmp_path, still under the shard lock, then verify the repair actually worked.
        os.replace(tmp_path, final_path)
        _fsync_dir(final_path.parent)
        if final_path.stat().st_size != size or _hash_file(final_path) != sha256_hex:
            raise StorageIntegrityError(
                f"Kunde inte reparera blob {sha256_hex} -- innehållet matchar fortfarande inte "
                f"den förväntade sha256:n efter ompublicering."
            )

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
        if storage_key.startswith("staging/"):
            try:
                return self._resolve_staging(storage_key).is_dir()
            except InvalidStorageKey:
                return False
        try:
            path = self._resolve(storage_key)
        except InvalidStorageKey:
            return False
        return path.is_file()

    def delete(self, storage_key: str) -> None:
        if storage_key.startswith("staging/"):
            self.delete_staged(storage_key)
            return
        path = self._resolve(storage_key)
        with self._key_lock(storage_key[:2]):
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
        return _hash_file(path) == expected_sha256
