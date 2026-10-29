"""Resumable 5 GB+ session contract and measured isolation. Does not certify 5 GB."""

from __future__ import annotations

import hashlib
import resource
from pathlib import Path

from app.large_file_lane import ResumableUploadSession, StreamingImportLane, five_gb_not_supported
from app.large_file_lane.isolation import MeasuredBackgroundIsolation
from app.large_file_lane.stream import TARGET_FILE_BYTES


def test_declared_5gb_session_resumes_without_claiming_support(tmp_path: Path):
    root = tmp_path / "session"
    created = ResumableUploadSession.create(
        root,
        session_id="five-gb-declared",
        declared_total_bytes=TARGET_FILE_BYTES,
        chunk_size=1024,
    )
    assert created.manifest.public()["declared_meets_5gb_target"] is True
    assert created.manifest.public()["five_gb_supported"] is False
    first = created.write_part(b"a" * 1024, expected_offset=0)
    assert first["outcome"] == "accepted"
    second = created.write_part(b"b" * 1024, expected_offset=1024)
    assert second["offset"] == 2048
    restored = ResumableUploadSession.load(root)
    assert restored.manifest.offset == 2048
    third = restored.write_part(b"c" * 512, expected_offset=2048)
    assert third["outcome"] == "accepted"
    assert third["offset"] == 2560
    unfinished = restored.finalize()
    assert unfinished["outcome"] == "incomplete"
    assert unfinished["five_gb_supported"] is False
    assert unfinished["five_by_five_gb_measured"] is False
    assert unfinished["indexed"] is False


def test_crash_resume_finalizes_from_spool_hash(tmp_path: Path):
    payload = (b"0123456789abcdef" * 1024) * 8  # 128 KiB
    root = tmp_path / "complete"
    session = ResumableUploadSession.create(
        root,
        session_id="complete-small",
        declared_total_bytes=len(payload),
        chunk_size=4096,
    )
    mid = len(payload) // 2
    assert session.write_part(payload[:4096], expected_offset=0)["outcome"] == "accepted"
    offset = 4096
    while offset < mid:
        chunk = payload[offset : offset + 4096]
        session.write_part(chunk, expected_offset=offset)
        offset += len(chunk)
    crashed_offset = session.manifest.offset
    restored = ResumableUploadSession.load(root)
    assert restored.manifest.offset == crashed_offset
    while restored.manifest.offset < len(payload):
        start = restored.manifest.offset
        restored.write_part(payload[start : start + 4096], expected_offset=start)
    done = restored.finalize()
    assert done["outcome"] == "stored"
    assert done["sha256"] == hashlib.sha256(payload).hexdigest()
    assert done["stored"] is True
    assert done["indexed"] is False
    assert done["understood"] is False
    assert done["five_gb_supported"] is False
    assert restored.peak_part_bytes <= 4096
    oversized = restored.write_part(b"x", expected_offset=len(payload))
    assert oversized["outcome"] == "rejected"


def test_bounded_memory_on_multi_megabyte_stream(tmp_path: Path):
    size = 8 * 1024 * 1024
    path = tmp_path / "eight-mb.bin"
    path.write_bytes(b"\xab" * size)
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    lane = StreamingImportLane(chunk_size=64 * 1024, max_buffer_chunks=2)
    result = lane.process(path)
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert result.bytes_seen == size
    assert lane.peak_buffer <= 64 * 1024
    # ru_maxrss is KiB on Linux. Growth must stay far below the 8 MiB payload.
    assert after - before < 4 * 1024
    assert five_gb_not_supported()["five_gb_supported"] is False


def test_measured_isolation_keeps_interactive_admitted(tmp_path: Path):
    size = 2 * 1024 * 1024
    path = tmp_path / "bg.bin"
    path.write_bytes(b"\xcd" * size)
    lane = StreamingImportLane(chunk_size=32 * 1024, max_buffer_chunks=2)
    interactive_hits = {"n": 0}

    def background() -> None:
        lane.process(path)

    def interactive() -> None:
        interactive_hits["n"] += 1

    measured = MeasuredBackgroundIsolation(max_in_flight=1, max_queued=1).measure_interactive_during(
        background,
        interactive,
        interactive_calls=24,
    )
    assert measured["outcome"] == "measured"
    assert measured["background_blocked_interactive"] is False
    assert measured["interactive_samples"] >= 8
    assert measured["interactive_max_ms"] < 50
    assert measured["five_gb_supported"] is False
    assert interactive_hits["n"] >= 8
    assert lane.peak_buffer <= 32 * 1024
