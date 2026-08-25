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
    entries = [p for p in tmp_path.iterdir() if p.name not in ("tmp", "locks")]
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

    entries = [p for p in tmp_path.iterdir() if p.name not in ("tmp", "locks")]
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


def test_a_file_vanishing_between_the_failed_link_and_the_stat_now_fails_loudly_instead_of_guessing(tmp_path, monkeypatch):
    """Test A (founder's Pass 31 lettering) -- UPDATED for Pass 32. Pass 31's version of this
    test proved a retry loop could recover from a file vanishing between the failed link() and
    the follow-up stat(). Pass 32 closes that gap with a real fcntl lock instead (see
    app/storage/local_fs.py's `_key_lock()`): write_stream() now holds that lock for
    `_publish()`'s entire body, and delete() holds the identical lock around its own unlink(),
    so a REAL concurrent delete() can no longer land in this window at all -- see this file's
    new Pass 32 section below (test_a_real_concurrent_delete_never_lands_in_the_link_stat_window)
    for the actual proof of that, using genuine threads instead of a monkeypatch.

    What's left for THIS test to prove: the scenario is now only reachable by something
    bypassing the lock entirely (forged here via monkeypatch, standing in for e.g. manual
    filesystem interference on the host, outside this application's control) -- and when that
    happens, the code must fail loudly with StorageError rather than silently retrying and
    possibly masking real corruption."""
    storage = LocalFilesystemStorage(str(tmp_path))
    payload = b"pass 31/32 test A: race window" * 20
    storage.write_stream(_chunks(payload), max_bytes=10_000_000)

    real_link = os.link

    def _flaky_link(src, dst, *a, **kw):
        # Simulates something outside this process's lock discipline removing the file in
        # exactly the window between the failed link() and the follow-up stat().
        os.unlink(dst)
        raise FileExistsError()

    monkeypatch.setattr(os, "link", _flaky_link)

    with pytest.raises(StorageError):
        storage.write_stream(_chunks(payload), max_bytes=10_000_000)


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


def test_write_stream_vs_delete_never_returns_a_corrupt_or_deadlocked_blob(tmp_path):
    """Tests C/D/E (founder's Pass 31 lettering) together: real threads, real filesystem,
    write_stream() and delete() for the SAME content-addressed key racing repeatedly.

    This test used to assert "whenever write_stream() returns successfully, the blob it names
    genuinely exists on disk at that moment", and failed intermittently in CI for years of
    passes -- dismissed each time as a flake. It is not a flake: the assertion is structurally
    unprovable, and asserting it taught readers something false. `write_stream()` RELEASES
    `_key_lock()` before returning (its own docstring says so explicitly, and must, to avoid a
    lock-ordering cycle with `acquire_storage_key_lock()`), and `delete()` takes that same lock
    around its `unlink()`. So a concurrent deleter is fully entitled to remove the blob between
    write_stream() returning and the caller's next look at it. Post-return existence is not
    something this layer promises anyone -- which is precisely why
    `app/storage/references.py::store_content_with_reference_lock()` exists: callers who need
    the blob to survive until their DB reference commits take the DB advisory lock, VERIFY, and
    republish from memory if a deleter won. That is the real contract, and it is tested there.

    Believing write_stream() alone guarantees post-return existence is the exact false premise
    behind the write-before-reference bug class this codebase has now fixed three times (#133,
    #143, #145). A test asserting it is worse than no test.

    What IS provable regardless of scheduling, and is what this test asserts now:
      - if the blob is present after the race, its bytes hash to its own key (a concurrent
        delete can make it ABSENT, but can never make it present-and-wrong -- this catches
        corrupt publishes, which the old existence check never did)
      - neither participant ever raises unexpectedly
      - no run hangs (no deadlock between _publish() and delete() on the shared shard lock)
      - no tempfiles are left behind (E)
    """
    storage = LocalFilesystemStorage(str(tmp_path))
    payload = b"pass 31/32 tests C/D/E: write_stream vs delete" * 15
    expected_sha = hashlib.sha256(payload).hexdigest()

    failures: list[str] = []

    def _writer():
        try:
            blob = storage.write_stream(_chunks(payload), max_bytes=10_000_000)
            if blob.sha256 != expected_sha:
                failures.append(f"write_stream() returned the wrong hash: {blob.sha256}")
            # Absent is legitimate here (the deleter may have won); present-but-wrong is not.
            if storage.exists(blob.storage_key) and not storage.verify(
                blob.storage_key, expected_sha256=blob.sha256, expected_size=blob.size_bytes
            ):
                failures.append("a published blob is present but its bytes do not match its own key")
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

    for _ in range(250):
        t1 = threading.Thread(target=_writer)
        t2 = threading.Thread(target=_deleter, args=(seed.storage_key,))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert not t1.is_alive() and not t2.is_alive(), "a race participant never finished -- possible deadlock"

    assert failures == [], f"race invariant violated: {failures}"
    assert list((tmp_path / "tmp").iterdir()) == [], "no leftover tempfiles after the write/delete race"


def test_write_stream_gives_no_post_return_existence_guarantee_by_design(tmp_path):
    """Pins the contract the test above stopped over-asserting, so the weaker assertion there
    reads as a deliberate decision rather than something quietly given up on.

    A deleter that runs entirely AFTER write_stream() returned removes the blob, and
    write_stream() has no way to prevent that -- it no longer holds `_key_lock()`. Callers that
    need the blob to survive until their own DB reference commits must use
    `store_content_with_reference_lock()`, which verifies and republishes under the DB advisory
    lock. If someone later makes write_stream() hold the shard lock past return (reintroducing
    the lock-ordering cycle `_key_lock()`'s docstring rules out), this test fails and says why.
    """
    storage = LocalFilesystemStorage(str(tmp_path))
    blob = storage.write_stream(_chunks(b"no post-return existence guarantee"), max_bytes=10_000)
    assert storage.exists(blob.storage_key)

    storage.delete(blob.storage_key)
    assert not storage.exists(blob.storage_key), (
        "delete() must be able to remove a just-written blob -- write_stream() releases "
        "_key_lock() before returning and promises nothing about what happens afterwards"
    )


def test_publish_fails_loudly_when_link_conflicts_but_nothing_is_actually_there(tmp_path, monkeypatch):
    """Pass 32: with the retry loop gone (real mutual exclusion via `_key_lock()` makes it
    unnecessary -- see app/storage/local_fs.py's `_publish()` docstring), a single conflicting
    `os.link()` whose target then genuinely isn't there (nothing legitimate could cause this
    under the lock; a forged scenario standing in for something outside the process's
    control) must fail loudly with StorageError on the first attempt, not retry indefinitely."""
    storage = LocalFilesystemStorage(str(tmp_path))
    payload = b"pass 31/32: publish fails loudly instead of guessing"

    def _always_conflicting_link(src, dst, *a, **kw):
        raise FileExistsError()

    monkeypatch.setattr(os, "link", _always_conflicting_link)

    with pytest.raises(StorageError):
        storage.write_stream(_chunks(payload), max_bytes=10_000_000)


# --- Pass 32 (a seventh founder review round): real fcntl per-shard lock ---------------------
#
# Pass 31's os.link()-based retry loop shrank, but by its own admission never fully closed, the
# gap between _publish() observing "something's already there" and trusting that observation --
# a concurrent delete() could still land in that narrow window. Closed via a real fcntl.flock()
# (`app/storage/local_fs.py::LocalFilesystemStorage._key_lock()`) that write_stream() holds for
# _publish()'s entire body and delete() holds around its own unlink(). Tests below are the
# founder's own fresh point-2 lettering (A-I); several letters are satisfied by tests already
# in this file (noted below) rather than being duplicated:
#   C (two identical writers) -- test_two_identical_concurrent_writers_both_succeed_with_a_single_file_on_disk
#   D (hundreds of interleavings) -- test_write_stream_vs_delete_never_returns_a_blob_missing_from_disk (now 250 iterations)
#   F (persistent writer + DB commit always leaves the file) -- covered at the domain-service
#     layer, not here: tests/backend/test_library_import.py's and test_project_memory.py's Pass
#     31/32 writer-vs-purge race tests, which exercise the FULL protocol (this filesystem lock
#     plus the DB advisory lock together), since this file deliberately has no DB access.
#   G (no deadlock between the DB lock and this filesystem lock) -- same reasoning as F; proven
#     by test_library_import.py's Test D (races _store_bytes_with_reference_lock's DB lock
#     against attempt_storage_deletion_task(), which calls into this module's delete()).
#   I (a genuinely corrupt existing blob is still rejected) -- test_dedup_size_mismatch_against_an_existing_blob_raises_integrity_error


def test_a_real_concurrent_delete_blocks_until_write_streams_publish_finishes(tmp_path, monkeypatch):
    """Tests A/B (founder's Pass 32 point-2 lettering): proves the fcntl lock genuinely
    serializes write_stream()'s publish against a REAL concurrent delete() -- an actual second
    thread blocked on the actual OS lock, not a monkeypatch standing in for a race. While
    write_stream() holds `_key_lock()` for its `_publish()` call, a concurrent delete() for the
    same key must not be able to run its unlink() until the writer releases the lock -- closing
    both the old FileExists/stat window (A) and "unlink right after the last existence check"
    (B), since there is no longer any window at all: the two are mutually exclusive for the
    *entire* critical section, not just at a single checkpoint."""
    storage = LocalFilesystemStorage(str(tmp_path))
    payload = b"pass 32 tests A/B: real concurrent delete blocks on the fs lock" * 10
    # Seed the blob once so there's something real for both the writer's link() and the
    # deleter's unlink() to contend over.
    seed = storage.write_stream(_chunks(payload), max_bytes=10_000_000)

    writer_in_lock = threading.Event()
    release_writer = threading.Event()
    real_link = os.link

    def _slow_link(src, dst, *a, **kw):
        # By the time this runs, write_stream() has already acquired _key_lock() for this
        # call. Signal that we're inside the critical section and hold it open until told to
        # proceed, giving a concurrent delete() a real window to try (and fail) to run past us.
        writer_in_lock.set()
        release_writer.wait(timeout=5)
        return real_link(src, dst, *a, **kw)  # raises FileExistsError -- seed already published it

    monkeypatch.setattr(os, "link", _slow_link)

    errors: list[Exception] = []

    def _writer():
        try:
            storage.write_stream(_chunks(payload), max_bytes=10_000_000)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    deleter_done = threading.Event()

    def _deleter():
        try:
            storage.delete(seed.storage_key)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            deleter_done.set()

    t1 = threading.Thread(target=_writer)
    t1.start()
    assert writer_in_lock.wait(timeout=5), "writer never reached its critical section"

    t2 = threading.Thread(target=_deleter)
    t2.start()

    # The deleter must NOT be able to finish while the writer still holds the lock -- give it
    # a real chance to race ahead if the lock were not actually exclusive.
    assert not deleter_done.wait(timeout=0.3), (
        "delete() ran concurrently with _publish()'s critical section -- the lock did not serialize them"
    )

    release_writer.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not t1.is_alive() and not t2.is_alive(), "a race participant never finished -- possible deadlock"
    assert errors == [], f"unexpected exception(s): {errors}"


def test_write_stream_and_delete_stay_consistent_under_a_second_interleaving_shape(tmp_path):
    """Test E (founder's Pass 32 point-2 lettering), corrected. A second interleaving shape
    alongside the C/D/E harness above: the deleter always targets the PREVIOUS iteration's
    seed while a writer republishes the same content, and the seed is re-published between
    iterations regardless of who won.

    This test used to assert that "a return from write_stream() is a genuine,
    safe-at-that-instant guarantee the blob was present", reasoning that the check ran
    "before the deleter thread could possibly have been scheduled to interfere". That reasoning
    assumed a scheduling order the test does nothing to enforce, so it failed intermittently in
    CI and was repeatedly written off as a flake. It is not provable from outside the class at
    all: `write_stream()` releases `_key_lock()` before returning (deliberately -- see
    `_key_lock()`'s docstring on the lock-ordering cycle it must avoid), so from the instant it
    returns, a concurrent `delete()` holding that same lock is entitled to remove the blob.
    "Publish completion is safe" is only defined INSIDE the locked window; a caller who needs
    the blob to outlive that window uses
    `app/storage/references.py::store_content_with_reference_lock()`, which verifies and
    republishes under the DB advisory lock, and is tested there.

    Asserts the provable invariant instead, the same one the C/D/E harness above now uses: a
    concurrent delete may make the blob ABSENT, but nothing may ever make it
    present-with-wrong-bytes -- plus deadlock-freedom and no unexpected exceptions under this
    second interleaving."""
    storage = LocalFilesystemStorage(str(tmp_path))
    payload = b"pass 32 test E: safe publish completion" * 12
    seed = storage.write_stream(_chunks(payload), max_bytes=10_000_000)

    failures: list[str] = []

    def _writer():
        try:
            blob = storage.write_stream(_chunks(payload), max_bytes=10_000_000)
            if storage.exists(blob.storage_key) and not storage.verify(
                blob.storage_key, expected_sha256=blob.sha256, expected_size=blob.size_bytes
            ):
                failures.append("a published blob is present but its bytes do not match its own key")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"writer raised unexpectedly: {exc!r}")

    def _deleter():
        try:
            storage.delete(seed.storage_key)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"deleter raised unexpectedly: {exc!r}")

    for _ in range(50):
        t1 = threading.Thread(target=_writer)
        t2 = threading.Thread(target=_deleter)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert not t1.is_alive() and not t2.is_alive(), "a race participant never finished -- possible deadlock"
        # Re-seed for the next iteration regardless of which side "won" this one.
        seed = storage.write_stream(_chunks(payload), max_bytes=10_000_000)

    assert failures == [], f"content-addressing invariant violated under the race: {failures}"


def test_lock_file_count_stays_bounded_regardless_of_how_many_distinct_blobs_are_written(tmp_path):
    """Test H (founder's Pass 32 point-2 lettering): the fcntl lock files under root/locks/
    must never grow unbounded the way a per-exact-sha256 lock file would -- see `_key_lock()`'s
    own docstring for why it shards by the same two-hex-character prefix blobs already use.
    Writes many DISTINCT blobs (different content -> different sha256, spread across shards)
    and asserts the lock directory never exceeds the 256 possible two-hex-char shards, and that
    the tmp/ scratch directory is empty once every write has completed -- no leaked lock or
    tempfile artifacts from ordinary use."""
    storage = LocalFilesystemStorage(str(tmp_path))
    for i in range(500):
        payload = f"pass 32 test H: distinct blob #{i}".encode()
        storage.write_stream(_chunks(payload), max_bytes=10_000_000)

    lock_files = list((tmp_path / "locks").iterdir())
    assert len(lock_files) <= 256, f"expected at most 256 shard lock files, found {len(lock_files)}"
    assert list((tmp_path / "tmp").iterdir()) == [], "no leftover tempfiles after many distinct writes"


# --- Pass 32 blocker 2 (an eighth founder review round): verify hash, not just size --------
#
# _publish()'s SAME-SIZE dedup branch used to accept an existing file purely on a size match --
# a file with the right content-addressed path and the right size but the WRONG bytes (disk
# corruption, or a manual out-of-band edit) would be silently treated as "the" blob for its own
# sha256. Now hashes the existing file and, on a mismatch, repairs it from this call's own
# known-correct tmp_path bytes (still under the shard lock), then re-verifies. Tests below are
# the founder's own fresh lettering for this blocker; C (concurrent writers never accept a
# corrupt existing blob) is covered by the existing two-identical-concurrent-writers test plus
# the write/delete race test's repeated iterations (both exercise this same _publish() path);
# F (delete/write race stays deadlock-free) is covered by the SAME pre-existing race tests --
# no new locking was introduced by this blocker's fix, only a hash check inside an
# already-locked critical section.


def test_publish_repairs_a_same_size_but_corrupt_existing_blob(tmp_path):
    """Test A (founder's Pass 32 blocker-2 lettering): a file at a content-addressed path with
    the RIGHT size but the WRONG bytes must never be silently accepted just because its size
    matches -- write_stream() must detect the hash mismatch and repair the file from its own
    freshly-hashed, known-correct bytes."""
    storage = LocalFilesystemStorage(str(tmp_path))
    payload = b"pass 32 blocker 2 test A: same size wrong bytes" * 5
    blob = storage.write_stream(_chunks(payload), max_bytes=10_000_000)

    disk_path = tmp_path / blob.storage_key
    corrupted = bytes(b ^ 0xFF for b in payload)
    assert len(corrupted) == len(payload)
    disk_path.write_bytes(corrupted)
    assert storage.verify(blob.storage_key, expected_sha256=blob.sha256, expected_size=blob.size_bytes) is False

    second_blob = storage.write_stream(_chunks(payload), max_bytes=10_000_000)

    assert second_blob.storage_key == blob.storage_key
    assert disk_path.read_bytes() == payload  # repaired back to the correct content
    assert storage.verify(second_blob.storage_key, expected_sha256=second_blob.sha256, expected_size=second_blob.size_bytes) is True


def test_publish_raises_if_repairing_a_corrupt_same_size_blob_still_fails(tmp_path, monkeypatch):
    """Test D (founder's Pass 32 blocker-2 lettering, applied at the storage layer): if even
    the repair attempt doesn't produce matching bytes (a pathological repeat failure -- e.g. a
    faulty disk), _publish() must raise StorageIntegrityError rather than silently accepting
    corrupt content as if it were the real blob."""
    storage = LocalFilesystemStorage(str(tmp_path))
    payload = b"pass 32 blocker 2 test D: repair itself fails" * 5
    blob = storage.write_stream(_chunks(payload), max_bytes=10_000_000)

    disk_path = tmp_path / blob.storage_key
    disk_path.write_bytes(bytes(b ^ 0xFF for b in payload))

    real_replace = os.replace

    def _replace_then_corrupt_again(src, dst, *a, **kw):
        real_replace(src, dst, *a, **kw)
        with open(dst, "r+b") as f:
            f.seek(0)
            f.write(b"\x00" * min(4, len(payload)))

    monkeypatch.setattr(os, "replace", _replace_then_corrupt_again)

    with pytest.raises(StorageIntegrityError):
        storage.write_stream(_chunks(payload), max_bytes=10_000_000)
