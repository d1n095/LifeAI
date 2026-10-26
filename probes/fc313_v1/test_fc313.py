"""#313 large-file corrections — independent closure verification (examiner-owned).

Subject: #313 5cb9b46 (based on #311 c3373f9). Verifies the CHECKPOINT-HASH fix, resumable sessions,
crash recovery, integrity, backpressure and bounded memory on the REAL app.large_file_lane code.

SMALL fixtures only (<= ~1 MB). This does NOT verify 5 GB support — small-fixture success is not
5 GB success (see LARGE_FILE_GATE2_SPEC.md). No DB, no network, no paid calls. first_cycle_go=false.
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


def _blob(n: int, seed: bytes = b"\x02") -> bytes:
    base = (seed * 997 + b"LIFEAI-313")[:1024]
    return (base * ((n // len(base)) + 1))[:n]


# ---------------------------------------------------------------------------
# CHECKPOINT-HASH — resume hash now equals the single-pass full-file hash
# ---------------------------------------------------------------------------

def test_FC313_resume_hash_equals_single_pass():
    data = _blob(1024 * 1024)
    full = _lane(chunk_size=CHUNK).process(io.BytesIO(data)).sha256
    assert full == hashlib.sha256(data).hexdigest()
    captured = {}

    def on_chunk(chunk, state):
        if state.offset >= 4 * CHUNK and "cp" not in captured:
            captured["cp"] = state
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        _lane(chunk_size=CHUNK).process(io.BytesIO(data), on_chunk=on_chunk)
    resumed = _lane(chunk_size=CHUNK).process(io.BytesIO(data), checkpoint=captured["cp"]).sha256
    assert resumed == full, "resumed hash != single-pass hash (CHECKPOINT-HASH not closed)"


def test_FC313_resume_stays_bounded_memory():
    data = _blob(1024 * 1024)
    lane = _lane(chunk_size=CHUNK, max_buffer_chunks=2)
    # simulate a checkpoint at 8 chunks then resume; prefix re-hash must stay within one chunk
    from app.large_file_lane.stream import ImportCheckpoint
    cp = ImportCheckpoint(path="stream", offset=8 * CHUNK, sha256="x", bytes_seen=8 * CHUNK)
    out = lane.process(io.BytesIO(data), checkpoint=cp)
    view = lane.stored_vs_understood(out)
    assert view["bounded"] is True and view["peak_buffer_bytes"] <= CHUNK


# ---------------------------------------------------------------------------
# Resumable sessions + crash recovery (filesystem-backed)
# ---------------------------------------------------------------------------

def test_FC313_session_crash_recovery_finalizes_correct_hash(tmp_path):
    from app.large_file_lane.session import ResumableUploadSession
    data = _blob(200 * 1024)
    root = tmp_path / "sess"
    s = ResumableUploadSession.create(root, session_id="s1", declared_total_bytes=len(data), chunk_size=CHUNK)
    # write first half, then "crash" and reload from disk
    half = len(data) // 2
    for i in range(0, half, CHUNK):
        assert s.write_part(data[i:i + CHUNK])["outcome"] == "accepted"
    reloaded = ResumableUploadSession.load(root)              # fresh process
    assert reloaded.manifest.offset == s.manifest.offset       # durable resume point
    for i in range(reloaded.manifest.offset, len(data), CHUNK):
        assert reloaded.write_part(data[i:i + CHUNK])["outcome"] == "accepted"
    fin = reloaded.finalize()
    assert fin["outcome"] == "stored"
    assert fin["sha256"] == hashlib.sha256(data).hexdigest(), "finalized hash != content hash"
    assert fin["indexed"] is False and fin["understood"] is False


def test_FC313_session_backpressure_and_integrity_guards(tmp_path):
    from app.large_file_lane.session import ResumableUploadSession
    root = tmp_path / "g"
    s = ResumableUploadSession.create(root, session_id="g1", declared_total_bytes=100, chunk_size=64)
    assert s.write_part(b"")["outcome"] == "rejected"                       # empty
    assert s.write_part(b"x" * 65)["outcome"] == "rejected"                 # exceeds chunk_size
    assert s.write_part(b"a" * 64)["outcome"] == "accepted"                 # ok (offset 64)
    assert s.write_part(b"b" * 64)["outcome"] == "rejected"                 # would exceed declared_total (100)
    assert s.write_part(b"b" * 36)["outcome"] == "accepted"                 # exactly fills to 100
    fin = s.finalize()
    assert fin["outcome"] == "stored"
    # no more writes after finalize
    assert s.write_part(b"z" * 10)["outcome"] == "rejected"


def test_FC313_finalize_incomplete_refused(tmp_path):
    from app.large_file_lane.session import ResumableUploadSession
    root = tmp_path / "inc"
    s = ResumableUploadSession.create(root, session_id="i1", declared_total_bytes=100, chunk_size=64)
    s.write_part(b"a" * 50)
    assert s.finalize()["outcome"] == "incomplete"            # declared total not yet received


def test_FC313_create_validates_positive_bounds(tmp_path):
    from app.large_file_lane.session import ResumableUploadSession
    with pytest.raises(ValueError):
        ResumableUploadSession.create(tmp_path / "x", session_id="x", declared_total_bytes=0)
    with pytest.raises(ValueError):
        ResumableUploadSession.create(tmp_path / "y", session_id="y", declared_total_bytes=10, chunk_size=0)


# ---------------------------------------------------------------------------
# Still-honest 5 GB reporting (must NOT have flipped to "supported")
# ---------------------------------------------------------------------------

def test_FC313_five_gb_still_unsupported():
    from app.large_file_lane.stream import five_gb_not_supported
    v = five_gb_not_supported()
    assert v["five_gb_supported"] is False and v["five_by_five_gb_measured"] is False
    assert v["first_cycle_go"] is False
