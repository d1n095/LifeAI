"""P2 capacity measurement (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §10.7) — deliberately
NOT part of the normal unit-test run. Builds a synthetic multi-level-nested ZIP package with
several thousand files and runs it through the REAL import pipeline (run_import_job, real
Postgres, real storage backend, a mocked-but-realistic embedding provider — not
zip_import.py in isolation), measuring actual wall-clock time and peak memory so the
MAX_FILES=500 default can be confirmed or revised from real numbers instead of a guess.

Skipped by default (it takes tens of seconds to minutes and is not something every CI run or
`pytest tests/` invocation should pay for). Run explicitly with:

    RUN_CAPACITY_TEST=1 python -m pytest tests/backend/rag/test_zip_import_capacity.py -v -s
"""

import io
import os
import resource
import time
import tracemalloc
import zipfile

import pytest

from app.config import get_settings
from app.models.import_job import ImportJobStatus
from app.rag import library_import as li
from app.rag.zip_import import validate_and_extract_zip

EMBEDDING_DIM = get_settings().embedding_dim

# 2 regions x 5 shards x 250 files = 2500 leaf files, nested 2 levels deep (region.zip inside
# the outer package, shard.zip inside each region) — "flera nivåer" per §10.7, well above the
# 2 000-file floor the plan requires.
REGIONS = 2
SHARDS_PER_REGION = 5
FILES_PER_SHARD = 250
TOTAL_FILES = REGIONS * SHARDS_PER_REGION * FILES_PER_SHARD


def _build_synthetic_package() -> bytes:
    def _shard_zip(region: int, shard: int) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for i in range(FILES_PER_SHARD):
                zf.writestr(f"note_{i}.txt", f"Region {region} skiftlag {shard} anteckning {i}: rutinkontroll utford.")
        return buf.getvalue()

    def _region_zip(region: int) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for shard in range(SHARDS_PER_REGION):
                zf.writestr(f"shard_{shard}.zip", _shard_zip(region, shard))
        return buf.getvalue()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for region in range(REGIONS):
            zf.writestr(f"region_{region}.zip", _region_zip(region))
    return buf.getvalue()


@pytest.mark.skipif(
    not os.environ.get("RUN_CAPACITY_TEST"),
    reason="P2 capacity test: slow by design, run explicitly with RUN_CAPACITY_TEST=1 (see module docstring).",
)
@pytest.mark.asyncio
async def test_capacity_2500_files_through_the_real_import_pipeline(db_session, make_verified_user, monkeypatch):
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    async def _fake_embed(self, texts, model, **kwargs):
        return [[0.01 * (i + 1)] * EMBEDDING_DIM for i, _ in enumerate(texts)]

    async def _fake_chat(self, messages, model, **kwargs):
        return ChatResult(content="[]", provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 2})

    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)

    # MAX_FILES=500's default is baked into validate_and_extract_zip's signature at def time
    # (a plain Python default, not re-read at call time) — overridden here, at the call site
    # run_import_job._run_once uses, purely so THIS test can measure a package larger than
    # today's production ceiling. Nothing about zip_import.py itself is changed.
    real_validate = validate_and_extract_zip

    def _validate_with_higher_ceiling(raw, *, outer_filename="archive.zip", **kwargs):
        return real_validate(raw, outer_filename=outer_filename, max_files=TOTAL_FILES + 100, **kwargs)

    monkeypatch.setattr(li, "validate_and_extract_zip", _validate_with_higher_ceiling)

    raw = _build_synthetic_package()
    print(f"\n[P2 capacity] synthetic package: {TOTAL_FILES} files across 2 nesting levels, {len(raw)} bytes on disk")

    user, _ = make_verified_user()
    from sqlalchemy import text as sa_text

    from app.models.import_job import ImportJob
    from app.request_context import current_user_id as current_user_id_var

    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    from app.storage import get_storage

    def _read_chunk_for(data: bytes, size: int = 1 << 16):
        pos = 0

        def _read():
            nonlocal pos
            chunk = data[pos : pos + size]
            pos += len(chunk)
            return chunk

        return _read

    stored = get_storage().write_stream(_read_chunk_for(raw), max_bytes=len(raw))
    job = ImportJob(
        owner_id=user.id,
        status=ImportJobStatus.pending,
        source_filename="capacity-test.zip",
        source_checksum=stored.sha256,
        source_storage_key=stored.storage_key,
        source_size_bytes=stored.size_bytes,
    )
    db_session.add(job)
    db_session.commit()

    tracemalloc.start()
    rusage_before_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started = time.monotonic()

    await li.run_import_job(db_session, job.id, user.id)

    elapsed = time.monotonic() - started
    _, peak_python_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rusage_after_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    db_session.refresh(job)
    assert job.status == ImportJobStatus.completed
    assert job.succeeded_count == TOTAL_FILES
    assert job.failed_count == 0

    per_file_ms = (elapsed / TOTAL_FILES) * 1000
    print(
        f"[P2 capacity] {TOTAL_FILES} files: {elapsed:.1f}s wall clock "
        f"({per_file_ms:.1f} ms/file), peak Python heap {peak_python_bytes / (1024 * 1024):.1f} MB, "
        f"process RSS high-water mark before/after: {rusage_before_kb / 1024:.1f} MB / {rusage_after_kb / 1024:.1f} MB"
    )
