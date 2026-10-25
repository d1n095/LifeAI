"""Large-file lane: streaming, resume, isolation. Does not claim 5 GB."""

from __future__ import annotations

from pathlib import Path

from app.large_file_lane import StreamingImportLane, five_gb_not_supported
from app.large_file_lane.isolation import BackgroundIsolation


def test_streaming_never_buffers_the_whole_file(tmp_path: Path):
    payload = (b"abcdefgh" * 8192) * 8  # 512 KiB
    path = tmp_path / "sample.bin"
    path.write_bytes(payload)
    lane = StreamingImportLane(chunk_size=4096, max_buffer_chunks=2)
    seen = []

    def on_chunk(chunk: bytes, checkpoint) -> None:
        seen.append(len(chunk))
        assert len(chunk) <= 4096

    result = lane.process(path, on_chunk=on_chunk)
    assert result.bytes_seen == len(payload)
    assert result.indexed is False
    assert result.understood is False
    assert lane.peak_buffer <= 8192
    assert max(seen) <= 4096
    status = lane.stored_vs_understood(result)
    assert status["stored"] is True
    assert status["indexed"] is False
    assert status["five_gb_supported"] is False


def test_resume_from_checkpoint(tmp_path: Path):
    payload = b"0123456789" * 1000
    path = tmp_path / "resume.bin"
    path.write_bytes(payload)
    lane = StreamingImportLane(chunk_size=250)
    mid = None

    def stop_halfway(chunk: bytes, checkpoint) -> None:
        nonlocal mid
        if checkpoint.bytes_seen >= 2500 and mid is None:
            mid = checkpoint
            raise RuntimeError("simulated crash")

    try:
        lane.process(path, on_chunk=stop_halfway)
    except RuntimeError:
        pass
    assert mid is not None
    resumed = StreamingImportLane(chunk_size=250)
    finished = resumed.process(path, checkpoint=mid)
    assert finished.bytes_seen == len(payload)
    assert finished.offset == len(payload)


def test_background_import_does_not_block_interactive():
    iso = BackgroundIsolation(max_in_flight=1, max_queued=2)
    assert iso.admit("interactive", name="founder_chat")["admitted"] is True
    first = iso.admit("background", name="import-a")
    second = iso.admit("background", name="import-b")
    third = iso.admit("background", name="import-c")
    later = iso.admit("interactive", name="founder_ops")
    assert first["admitted"] is True
    assert second["admitted"] is False
    assert third["admitted"] is False or third.get("queued") or third.get("backpressure")
    assert later["admitted"] is True
    assert five_gb_not_supported()["five_by_five_gb_measured"] is False
