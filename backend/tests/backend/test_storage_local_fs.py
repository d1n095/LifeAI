"""Life Library durable-worker package: unit tests for the local filesystem storage backend
(app/storage/local_fs.py). Pure filesystem tests — no DB, no app fixtures — so they run fast
and isolate the storage-layer safety properties (atomicity, path-traversal/symlink defense,
size limits) from everything built on top of them."""

import hashlib
import os

import pytest

from app.storage.base import InvalidStorageKey, StorageIntegrityError, StorageSizeLimitExceeded
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
    doesn't blindly trust "same key exists" without at least a cheap size sanity check."""
    storage = LocalFilesystemStorage(str(tmp_path))
    payload = b"originalinnehall"
    blob = storage.write_stream(_chunks(payload), max_bytes=10_000_000)

    disk_path = tmp_path / blob.storage_key
    disk_path.write_bytes(payload + b"extra-bytes-that-shouldnt-be-there")

    with pytest.raises(StorageIntegrityError):
        storage.write_stream(_chunks(payload), max_bytes=10_000_000)
