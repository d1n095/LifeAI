"""#311 large-file lane acceptance (examiner-owned).

Subject: #311 c3373f9 (branched from #306 2444bcc). Exercises the REAL app.large_file_lane
(stream.StreamingImportLane / ImportCheckpoint / five_gb_not_supported, isolation.BackgroundIsolation).

Uses SMALL fixtures (<= ~2 MB) to measure the SHAPE of the lane — bounded memory independent of file
size, checkpoint resume, isolation, corruption detection, honest limits. It does NOT run 5 GB or
5x5 GB (that needs authorized large-scale resources; see LARGE_FILE_GATE2_SPEC.md).

PASS = enforced today. xfail(strict) = required integrity property not met. No DB, no network.
first_cycle_go=false.
"""
from __future__ import annotations

import hashlib
import io

import pytest

from _guards import egress  # noqa: F401

CHUNK = 64 * 1024


def _lane(**kw):
    from app.large_file_lane.stream import StreamingImportLane
    return StreamingImportLane(**kw)


def _blob(n_bytes: int, seed: bytes = b"\x00") -> bytes:
    # deterministic, incompressible-ish content
    base = (seed * 997 + b"LIFEAI-LARGEFILE")[:1024]
    return (base * ((n_bytes // len(base)) + 1))[:n_bytes]


# ---------------------------------------------------------------------------
# A. Bounded-memory streaming
# ---------------------------------------------------------------------------

def test_LF1_memory_is_bounded_independent_of_file_size():
    lane = _lane(chunk_size=CHUNK, max_buffer_chunks=2)
    data = _blob(2 * 1024 * 1024)  # 2 MB over 64 KB chunks = 32 chunks
    cp = lane.process(io.BytesIO(data))
    view = lane.stored_vs_understood(cp)
    assert view["bounded"] is True
    assert view["peak_buffer_bytes"] <= CHUNK, "peak buffer exceeded one chunk"
    assert cp.bytes_seen == len(data)


# ---------------------------------------------------------------------------
# B. File integrity (single pass) + corruption detection
# ---------------------------------------------------------------------------

def test_LF2_single_pass_hash_matches_content():
    lane = _lane()
    data = _blob(512 * 1024)
    cp = lane.process(io.BytesIO(data))
    assert cp.sha256 == hashlib.sha256(data).hexdigest()


def test_LF3_corruption_is_detectable_via_hash():
    lane = _lane()
    good = _blob(256 * 1024, seed=b"\x01")
    bad = bytearray(good)
    bad[1000] ^= 0xFF  # flip one bit region
    h_good = lane.process(io.BytesIO(good)).sha256
    h_bad = _lane().process(io.BytesIO(bytes(bad))).sha256
    assert h_good != h_bad, "a corrupted byte did not change the content hash"


# ---------------------------------------------------------------------------
# C. Separation of stored / indexed / understood + honest limits
# ---------------------------------------------------------------------------

def test_LF4_stored_is_not_indexed_or_understood():
    lane = _lane()
    cp = lane.process(io.BytesIO(_blob(128 * 1024)))
    assert cp.indexed is False and cp.understood is False
    view = lane.stored_vs_understood(cp)
    assert view["stored"] is True and view["indexed"] is False and view["understood"] is False


def test_LF5_five_gb_is_honestly_unsupported():
    from app.large_file_lane.stream import five_gb_not_supported
    v = five_gb_not_supported()
    assert v["five_gb_supported"] is False and v["five_by_five_gb_measured"] is False
    assert v["stored_is_not_indexed"] is True and v["http_upload_limit_unchanged"] is True
    assert v["first_cycle_go"] is False


# ---------------------------------------------------------------------------
# D. Cancellation + crash/restart recovery (offset/bytes)
# ---------------------------------------------------------------------------

def test_LF6_cancellation_via_callback_stops_processing():
    lane = _lane(chunk_size=CHUNK, max_buffer_chunks=2)
    data = _blob(1024 * 1024)
    seen = {"n": 0}

    def on_chunk(chunk, state):
        seen["n"] += 1
        if seen["n"] == 3:
            raise KeyboardInterrupt("founder cancelled")

    with pytest.raises(KeyboardInterrupt):
        lane.process(io.BytesIO(data), on_chunk=on_chunk)
    assert seen["n"] == 3, "processing did not stop at cancellation"


def test_LF7_restart_recovers_offset_and_byte_count():
    lane = _lane(chunk_size=CHUNK)
    data = _blob(1024 * 1024)  # 16 chunks
    # crash after 4 chunks: capture the checkpoint handed to on_chunk
    captured = {}

    def on_chunk(chunk, state):
        if state.offset >= 4 * CHUNK and "cp" not in captured:
            captured["cp"] = state
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        lane.process(io.BytesIO(data), on_chunk=on_chunk)
    cp = captured["cp"]
    # resume from the checkpoint over the SAME content
    final = _lane(chunk_size=CHUNK).process(io.BytesIO(data), checkpoint=cp)
    assert final.bytes_seen == len(data), "resumed byte count did not reach full size"
    assert final.offset == len(data)


# ---------------------------------------------------------------------------
# E. Concurrent workload isolation + backpressure
# ---------------------------------------------------------------------------

def test_LF8_interactive_isolated_from_largefile_and_backpressure():
    from app.large_file_lane.isolation import BackgroundIsolation
    iso = BackgroundIsolation(max_in_flight=1, max_queued=1)
    assert iso.admit("background", name="bigfile1")["admitted"] is True   # in flight
    q = iso.admit("background", name="bigfile2")
    assert q["admitted"] is False and q.get("queued") is True             # queued
    bp = iso.admit("background", name="bigfile3")
    assert bp["admitted"] is False and bp.get("backpressure") is True     # backpressure
    # interactive is never starved by large-file work
    assert iso.admit("interactive", name="founder")["admitted"] is True


# ---------------------------------------------------------------------------
# F. Offset-checkpoint INTEGRITY — resume hash must equal single-pass hash (xfail strict)
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="CHECKPOINT-HASH: resuming from a checkpoint resets the sha256 "
                   "and seeks to the offset, so the resumed final hash covers only post-offset bytes "
                   "and silently differs from the single-pass full-file hash. A resumed large-file "
                   "import cannot produce a verifiable whole-file checksum.")
def test_LF9_resume_hash_equals_single_pass_hash():
    lane = _lane(chunk_size=CHUNK)
    data = _blob(1024 * 1024)
    full = lane.process(io.BytesIO(data)).sha256
    # crash after 4 chunks, then resume
    captured = {}

    def on_chunk(chunk, state):
        if state.offset >= 4 * CHUNK and "cp" not in captured:
            captured["cp"] = state
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        _lane(chunk_size=CHUNK).process(io.BytesIO(data), on_chunk=on_chunk)
    resumed = _lane(chunk_size=CHUNK).process(io.BytesIO(data), checkpoint=captured["cp"]).sha256
    assert resumed == full, "resumed hash does not equal the single-pass full-file hash"
