"""Life Library durable-worker package: unit tests for the local filesystem storage backend
(app/storage/local_fs.py). Pure filesystem tests — no DB, no app fixtures — so they run fast
and isolate the storage-layer safety properties (atomicity, path-traversal/symlink defense,
size limits) from everything built on top of them."""

import hashlib
import os
import threading

import pytest

from app.storage.base import InvalidStorageKey, StorageError, StorageIntegrityError, StorageSizeLimitExceeded
from app.storage.local_fs import LocalFilesystemStorage


def _chunks(data: bytes, size: int = 8):
    """A read_chunk() callable that hands back `data` in `size`-byte pieces, then b""."""
    pos = 0

    def _read():
        nonlocal pos
        chunk = data[pos : pos + size]
        pos += len(chunk)
        return chunk

    return _read


def test_write_then_read_round_trips_exact_bytes(tmp_path):
    storage = LocalFilesystemStorage(str(tmp_path))
    payload = b"hej hej " * 1000
    blob = storage.write_stream(_chunks(payload), max_bytes=10_000_000)

    assert blob.sha256 == hashlib.sha256(payload).hexdigest()
    assert blob.size_bytes == len(payload)
    assert blob.storage_key == f"{blob.sha256[:2]}/{blob.sha256}"

    with storage.open_read(blob.storage_key) as f:
        assert f.read() == payload


def test_a_blob_survives_being_read_by_a_brand_new_storage_instance(tmp_path):
    """Durability doesn't depend on any single process's in-memory state: a fresh
    LocalFilesystemStorage constructed against the same root (standing in for a backend or
    worker process restart, since a real restart is exactly 'a new process constructs a new
    instance against the same persistent volume') must see everything an earlier instance
    wrote."""
    payload = b"overlever en omstart" * 500
    writer = LocalFilesystemStorage(str(tmp_path))
    blob = writer.write_stream(_chunks(payload), max_bytes=10_000_000)
    del writer

    reader = LocalFilesystemStorage(str(tmp_path))
    assert reader.exists(blob.storage_key)
    assert reader.verify(blob.storage_key, expected_sha256=blob.sha256, expected_size=blob.size_bytes)
    with reader.open_read(blob.storage_key) as f:
        assert f.read() == payload


def test_identical_content_deduplicates_to_the_same_key_and_a_single_file_on_disk(tmp_path):
    storage = LocalFilesystemStorage(str(tmp_path))
    payload = b"samma innehall varje gang"
    blob1 = storage.write_stream(_chunks(payload), max_bytes=10_000_000)
    blob2 = storage.write_stream(_chunks(payload), max_bytes=10_000_000)

    assert blob1.storage_key == blob2.storage_key
    matches = list(tmp_path.rglob(f"{blob1.sha256}"))
    assert len(matches) == 1


def test_exceeding_max_bytes_aborts_immediately_and_leaves_no_final_blob(tmp_path):
    storage = LocalFilesystemStorage(str(tmp_path))
    payload = b"x" * 1000

    with pytest.raises(StorageSizeLimitExceeded):
        storage.write_stream(_chunks(payload, size=100), max_bytes=250)

    # Nothing durable was written: no blob directories beyond the empty "tmp" scaffold, and
    # the temp file used mid-write was cleaned up rather than left behind.
    entries = [p for p in tmp_path.iterdir() if p.name != "tmp"]
    assert entries == []
    assert list((tmp_path / "tmp").iterdir()) == []


def test_a_read_chunk_error_partway_through_leaves_no_partial_blob_and_cleans_up_the_temp_file(tmp_path):
    storage = LocalFilesystemStorage(str(tmp_path))
    calls = {"n": 0}

    def _flaky_read():
        calls["n"] += 1
        if calls["n"] > 2:
            raise ConnectionError("simulerat avbrott")
        return b"partiellt "

    with pytest.raises(ConnectionError):
        storage.write_stream(_flaky_read, max_bytes=10_000_000)

    entries = [p for p in tmp_path.iterdir() if p.name != "tmp"]
    assert entries == []
    assert list((tmp_path / "tmp").iterdir()) == []


def test_path_traversal_key_is_rejected(tmp_path):
    storage = LocalFilesystemStorage(str(tmp_path))
    for bad_key in ["../../../etc/passwd", "..%2f..%2fetc%2fpasswd", "aa/" + "f" * 64 + "/../../../etc/passwd", "not-a-valid-key"]:
        with pytest.raises(InvalidStorageKey):
            storage.open_read(bad_key)
        assert storage.exists(bad_key) is False
        with pytest.raises(InvalidStorageKey):
            storage.delete(bad_key)


def test_symlink_planted_at_a_blobs_expected_path_is_refused_not_followed(tmp_path):
    storage = LocalFilesystemStorage(str(tmp_path))
    secret = tmp_path.parent / "secret-outside-storage-root.txt"
    secret.write_text("hemligt innehall som aldrig ska lasas via storage")

    fake_sha = "b" * 64
    key = f"{fake_sha[:2]}/{fake_sha}"
    (tmp_path / fake_sha[:2]).mkdir(parents=True, exist_ok=True)
    os.symlink(secret, tmp_path / fake_sha[:2] / fake_sha)

    with pytest.raises(InvalidStorageKey):
        storage.open_read(key)
    with pytest.raises(InvalidStorageKey):
        storage.delete(key)
    assert storage.exists(key) is False


def test_verify_detects_corruption_and_missing_blobs(tmp_path):
    storage = LocalFilesystemStorage(str(tmp_path))
    payload = b"ett dokument som senare korrumperas pa disk"
    blob = storage.write_stream(_chunks(payload), max_bytes=10_000_000)

    assert storage.verify(blob.storage_key, expected_sha256=blob.sha256, expected_size=blob.size_bytes) is True

    # Corrupt the blob directly on disk (bypassing the storage API, simulating real disk
    # corruption or an out-of-band edit) — verify() must catch it, not silently trust the
    # database's recorded checksum.
    disk_path = tmp_path / blob.storage_key
    disk_path.write_bytes(payload + b"corrupted-tail")
    assert storage.verify(blob.storage_key, expected_sha256=blob.sha256, expected_size=blob.size_bytes) is False

    missing_sha = "c" * 64
    assert storage.verify(f"{missing_sha[:2]}/{missing_sha}", expected_sha256=missing_sha) is False


def test_delete_is_idempotent(tmp_path):
    storage = LocalFilesystemStorage(str(tmp_path))
    payload = b"raderas snart, sedan raderas igen"
    blob = storage.write_stream(_chunks(payload), max_bytes=10_000_000)

    storage.delete(blob.storage_key)
    assert storage.exists(blob.storage_key) is False
    storage.delete(blob.storage_key)  # second delete must not raise


def test_dedup_size_mismatch_against_an_existing_blob_raises_integrity_error(tmp_path):
    """A defensive check, not an expected real-world path (a sha256 collision with a
    different length is not something that happens by accident) — proves write_stream
    doesn't blindly trust "same key exists" without at least a cheap size sanity check.
    Test F (Pass 31, founder's lettering): also proves this holds under the new link()-based
    retry loop -- a genuinely, persistently corrupt existing blob must raise immediately, not
    get silently "healed" by retrying, and not get accepted after exhausting retries either."""
    storage = LocalFilesystemStorage(str(tmp_path))
    payload = b"originalinnehall"
    blob = storage.write_stream(_chunks(payload), max_bytes=10_000_000)

    disk_path = tmp_path / blob.storage_key
    disk_path.write_bytes(payload + b"extra-bytes-that-shouldnt-be-there")

    with pytest.raises(StorageIntegrityError):
        storage.write_stream(_chunks(payload), max_bytes=10_000_000)


# --- Pass 31 (a sixth founder review round): write_stream()/delete() concurrency ------------
#
# The old `if final_path.exists(): ... else: os.rename(...)` publish step had a real TOCTOU
# against a concurrent delete() landing between the check and the (skipped) write -- write_
# stream() could return a StoredBlob whose storage_key resolves to nothing on disk. Closed via
# an os.link()-based retry loop (see app/storage/local_fs.py's module + _publish() docstrings).
# Tests below are the founder's own lettering; F lives on the pre-existing corruption test
# just above (extended, not duplicated).


def test_a_real_concurrent_delete_landing_between_the_failed_link_and_the_stat_recovers(tmp_path, monkeypatch):
    """Test A (founder's lettering): deterministically reproduces the exact race window --
    write_stream()'s os.link() observes "already exists" (FileExistsError), and a concurrent
    delete() removes that file before this call's own follow-up stat() runs. Old code had no
    such follow-up at all (a bare `if exists(): skip write`); new code must retry its own
    link() instead of trusting the stale observation, and its own bytes must end up published."""
    storage = LocalFilesystemStorage(str(tmp_path))
    payload = b"pass 31 test A: race window" * 20
    first_blob = storage.write_stream(_chunks(payload), max_bytes=10_000_000)

    real_link = os.link
    call_count = {"n": 0}

    def _flaky_link(src, dst, *a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # A concurrent delete() races in and removes the file that made THIS link()
            # attempt collide, exactly in the window between the failed link() and the
            # follow-up stat() that would otherwise trust it's still there.
            os.unlink(dst)
            raise FileExistsError()
        return real_link(src, dst, *a, **kw)

    monkeypatch.setattr(os, "link", _flaky_link)

    second_blob = storage.write_stream(_chunks(payload), max_bytes=10_000_000)

    assert second_blob.storage_key == first_blob.storage_key
    assert storage.exists(second_blob.storage_key) is True
    assert call_count["n"] == 2, "expected exactly one failed link() attempt, then one that succeeded on retry"


def test_two_identical_concurrent_writers_both_succeed_with_a_single_file_on_disk(tmp_path):
    """Test B (founder's lettering): two REAL threads writing byte-identical content at the
    same time -- whichever wins the atomic os.link(), the other must observe a verified,
    correct existing file and succeed too, never raising and never leaving two copies."""
    storage = LocalFilesystemStorage(str(tmp_path))
    payload = b"pass 31 test B: two identical concurrent writers" * 30

    start = threading.Barrier(2)
    results: list = []
    errors: list[Exception] = []

    def _writer():
        try:
            start.wait(timeout=5)
            results.append(storage.write_stream(_chunks(payload), max_bytes=10_000_000))
        except Exception as exc:  # noqa: BLE001 - captured for the assertion below, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_writer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not any(t.is_alive() for t in threads), "a writer thread never finished -- possible deadlock"
    assert errors == [], f"unexpected exception(s): {errors}"
    assert len(results) == 2
    assert results[0].storage_key == results[1].storage_key
    assert results[0].sha256 == hashlib.sha256(payload).hexdigest()

    matches = list(tmp_path.rglob(results[0].sha256))
    assert len(matches) == 1, "two concurrent identical writers must publish exactly one file, not two"
    assert list((tmp_path / "tmp").iterdir()) == [], "no leftover tempfiles after two concurrent writers"


def test_write_stream_vs_delete_never_returns_a_blob_missing_from_disk(tmp_path):
    """Tests C/D/E (founder's lettering) together: real threads, real filesystem,
    write_stream() and delete() for the SAME content-addressed key racing repeatedly. The
    invariant that must hold regardless of scheduling: whenever write_stream() returns
    successfully, the blob it names genuinely exists on disk at that moment (C/D), no run ever
    hangs (no deadlock), and no tempfiles are left behind afterward (E)."""
    storage = LocalFilesystemStorage(str(tmp_path))
    payload = b"pass 31 tests C/D/E: write_stream vs delete" * 15

    failures: list[str] = []

    def _writer():
        try:
            blob = storage.write_stream(_chunks(payload), max_bytes=10_000_000)
            if not storage.exists(blob.storage_key):
                failures.append("write_stream() returned success but the blob is missing from disk")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"writer raised unexpectedly: {exc!r}")

    def _deleter(storage_key: str):
        try:
            storage.delete(storage_key)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"deleter raised unexpectedly: {exc!r}")

    # Publish once up front so the very first iteration's deleter has something real to race
    # against too, not just an already-empty key.
    seed = storage.write_stream(_chunks(payload), max_bytes=10_000_000)

    for _ in range(20):
        t1 = threading.Thread(target=_writer)
        t2 = threading.Thread(target=_deleter, args=(seed.storage_key,))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert not t1.is_alive() and not t2.is_alive(), "a race participant never finished -- possible deadlock"

    assert failures == [], f"race invariant violated: {failures}"
    assert list((tmp_path / "tmp").iterdir()) == [], "no leftover tempfiles after the write/delete race"


def test_publish_gives_up_after_too_many_concurrent_attempts(tmp_path, monkeypatch):
    """A pathological, never-terminating back-and-forth (nothing in this codebase's real call
    patterns can actually sustain this -- see _PUBLISH_MAX_ATTEMPTS's own comment) must fail
    loudly with StorageError rather than looping forever. os.link() is made to always fail
    with FileExistsError WITHOUT ever actually creating anything at the destination -- so the
    follow-up `final_path.stat()` genuinely (not mocked) raises FileNotFoundError every single
    iteration, exhausting the retry budget exactly like a real, relentless back-and-forth
    would."""
    storage = LocalFilesystemStorage(str(tmp_path))
    payload = b"pass 31: publish gives up eventually"

    def _always_conflicting_link(src, dst, *a, **kw):
        raise FileExistsError()

    monkeypatch.setattr(os, "link", _always_conflicting_link)

    with pytest.raises(StorageError):
        storage.write_stream(_chunks(payload), max_bytes=10_000_000)
