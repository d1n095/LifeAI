"""Production resumable HTTP on the #313 spool. Does not certify 5 GB."""

from __future__ import annotations

import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.db import get_db
from app.deps import get_current_user
from app.founder import FOUNDER_USER_ID
from app.large_file_lane.http_store import (
    HTTP_PART_MAX_BYTES,
    OWNER_QUOTA_BYTES,
    ResumableHttpError,
    ResumableHttpStore,
)
from app.large_file_lane.session import MANIFEST, SPOOL, ResumableUploadSession
from app.large_file_lane.stream import TARGET_FILE_BYTES
from app.limiter import limiter
from app.models.user import UserRole
from app.routers import library
from app.routers.library_resumable import get_resumable_store, router


REPO_ROOT = Path(__file__).resolve().parents[3]


class _Founder:
    id = FOUNDER_USER_ID
    role = UserRole.founder


class _Other:
    id = uuid.UUID(int=2)
    role = UserRole.member


class _Query:
    def __init__(self, user: object) -> None:
        self._user = user

    def filter_by(self, **kwargs: object) -> "_Query":
        return self

    def first(self) -> object:
        return self._user


class _Db:
    def __init__(self, user: object) -> None:
        self._user = user

    def query(self, model: object) -> _Query:
        return _Query(self._user)


def _app(store: ResumableHttpStore, user: object = _Founder()) -> FastAPI:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_resumable_store] = lambda: store
    app.dependency_overrides[get_db] = lambda: _Db(user)
    return app


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ResumableHttpStore:
    monkeypatch.setattr("app.routers.library_resumable.acquire_owner_erasure_lock", lambda *a, **k: None)
    return ResumableHttpStore(tmp_path / "resumable")


def test_store_resumes_after_new_process_and_finalizes_hash(store: ResumableHttpStore):
    payload = b"abcdef" * 2000
    created = store.create(FOUNDER_USER_ID, declared_total_bytes=len(payload), declared_filename="notes.bin")
    session_id = uuid.UUID(created["session_id"])
    first = payload[: HTTP_PART_MAX_BYTES]
    store.write_part(FOUNDER_USER_ID, session_id, first, expected_offset=0)
    restarted = ResumableHttpStore(store.root)
    status = restarted.get(FOUNDER_USER_ID, session_id)
    assert status["offset"] == len(first)
    remaining = payload[len(first) :]
    while remaining:
        chunk = remaining[: HTTP_PART_MAX_BYTES]
        restarted.write_part(FOUNDER_USER_ID, session_id, chunk, expected_offset=len(payload) - len(remaining))
        remaining = remaining[len(chunk) :]
    done = restarted.finalize(FOUNDER_USER_ID, session_id)
    assert done["outcome"] == "stored"
    assert done["sha256"] == hashlib.sha256(payload).hexdigest()
    assert done["indexed"] is False
    assert done["five_gb_supported"] is False


def test_crash_after_spool_write_rolls_back_uncommitted_tail(
    store: ResumableHttpStore,
    monkeypatch: pytest.MonkeyPatch,
):
    payload = b"committed-after-retry"
    created = store.create(FOUNDER_USER_ID, declared_total_bytes=len(payload))
    session_id = uuid.UUID(created["session_id"])
    original_persist = ResumableUploadSession._persist

    def crash_before_manifest(_session: ResumableUploadSession) -> None:
        raise RuntimeError("simulated process crash before manifest commit")

    monkeypatch.setattr(ResumableUploadSession, "_persist", crash_before_manifest)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        store.write_part(FOUNDER_USER_ID, session_id, payload, expected_offset=0)
    monkeypatch.setattr(ResumableUploadSession, "_persist", original_persist)

    restarted = ResumableHttpStore(store.root)
    assert restarted.get(FOUNDER_USER_ID, session_id)["offset"] == 0
    root = restarted.session_root(FOUNDER_USER_ID, session_id)
    assert (root / SPOOL).stat().st_size == 0
    restarted.write_part(FOUNDER_USER_ID, session_id, payload, expected_offset=0)
    done = restarted.finalize(FOUNDER_USER_ID, session_id)
    assert done["sha256"] == hashlib.sha256(payload).hexdigest()


def test_response_loss_retry_cannot_duplicate_committed_part(store: ResumableHttpStore):
    payload = b"one-effect"
    created = store.create(FOUNDER_USER_ID, declared_total_bytes=len(payload))
    session_id = uuid.UUID(created["session_id"])
    store.write_part(FOUNDER_USER_ID, session_id, payload, expected_offset=0)

    with pytest.raises(ResumableHttpError) as replay:
        store.write_part(FOUNDER_USER_ID, session_id, payload, expected_offset=0)
    assert replay.value.code == "offset_mismatch"
    assert store.session_root(FOUNDER_USER_ID, session_id).joinpath(SPOOL).read_bytes() == payload
    assert store.finalize(FOUNDER_USER_ID, session_id)["sha256"] == hashlib.sha256(payload).hexdigest()


def test_manifest_ahead_of_spool_fails_closed(store: ResumableHttpStore):
    payload = b"durable"
    created = store.create(FOUNDER_USER_ID, declared_total_bytes=len(payload))
    session_id = uuid.UUID(created["session_id"])
    store.write_part(FOUNDER_USER_ID, session_id, payload, expected_offset=0)
    root = store.session_root(FOUNDER_USER_ID, session_id)
    (root / SPOOL).write_bytes(payload[:-1])

    restarted = ResumableHttpStore(store.root)
    with pytest.raises(ResumableHttpError) as corrupt:
        restarted.get(FOUNDER_USER_ID, session_id)
    assert corrupt.value.code == "session_integrity_error"
    with pytest.raises(ResumableHttpError):
        restarted.finalize(FOUNDER_USER_ID, session_id)


def test_uncommitted_tail_is_removed_before_next_part(store: ResumableHttpStore):
    first = b"abc"
    second = b"def"
    created = store.create(FOUNDER_USER_ID, declared_total_bytes=len(first + second))
    session_id = uuid.UUID(created["session_id"])
    store.write_part(FOUNDER_USER_ID, session_id, first, expected_offset=0)
    root = store.session_root(FOUNDER_USER_ID, session_id)
    with (root / SPOOL).open("ab") as handle:
        handle.write(b"uncommitted-crash-tail")

    restarted = ResumableHttpStore(store.root)
    assert restarted.get(FOUNDER_USER_ID, session_id)["offset"] == len(first)
    assert (root / SPOOL).read_bytes() == first
    restarted.write_part(FOUNDER_USER_ID, session_id, second, expected_offset=len(first))
    assert restarted.finalize(FOUNDER_USER_ID, session_id)["sha256"] == hashlib.sha256(first + second).hexdigest()


def test_concurrent_same_offset_has_one_winner(store: ResumableHttpStore):
    payload = b"only-once"
    created = store.create(FOUNDER_USER_ID, declared_total_bytes=len(payload))
    session_id = uuid.UUID(created["session_id"])

    def attempt() -> str:
        try:
            store.write_part(FOUNDER_USER_ID, session_id, payload, expected_offset=0)
            return "accepted"
        except ResumableHttpError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: attempt(), range(2)))
    assert sorted(outcomes) == ["accepted", "offset_mismatch"]
    assert store.session_root(FOUNDER_USER_ID, session_id).joinpath(SPOOL).read_bytes() == payload


def test_corrupt_manifest_blocks_quota_and_listing(store: ResumableHttpStore):
    created = store.create(FOUNDER_USER_ID, declared_total_bytes=16)
    session_id = uuid.UUID(created["session_id"])
    root = store.session_root(FOUNDER_USER_ID, session_id)
    (root / MANIFEST).write_text("not-json")

    with pytest.raises(ResumableHttpError) as quota:
        store.create(FOUNDER_USER_ID, declared_total_bytes=1)
    assert quota.value.code == "session_integrity_error"
    with pytest.raises(ResumableHttpError) as listing:
        store.list_sessions(FOUNDER_USER_ID)
    assert listing.value.code == "session_integrity_error"
    response = TestClient(_app(store)).get("/api/library/resumable/sessions")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "session_integrity_error"


def test_manifest_identity_cannot_be_substituted(store: ResumableHttpStore):
    created = store.create(FOUNDER_USER_ID, declared_total_bytes=4)
    session_id = uuid.UUID(created["session_id"])
    root = store.session_root(FOUNDER_USER_ID, session_id)
    manifest_path = root / MANIFEST
    manifest = json.loads(manifest_path.read_text())
    manifest["session_id"] = str(uuid.uuid4())
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ResumableHttpError) as substituted:
        store.get(FOUNDER_USER_ID, session_id)
    assert substituted.value.code == "session_integrity_error"


def test_owner_isolation_and_path_traversal(store: ResumableHttpStore):
    created = store.create(FOUNDER_USER_ID, declared_total_bytes=32, declared_filename="own.bin")
    session_id = uuid.UUID(created["session_id"])
    store.write_part(FOUNDER_USER_ID, session_id, b"x" * 32, expected_offset=0)
    other = uuid.UUID(int=99)
    with pytest.raises(ResumableHttpError) as exc:
        store.get(other, session_id)
    assert exc.value.http_status == 404
    from app.large_file_lane.http_store import parse_session_id

    with pytest.raises(ResumableHttpError) as traversal:
        parse_session_id("../etc/passwd")
    assert traversal.value.code == "invalid_session_id"


def test_quota_disk_and_part_bounds(store: ResumableHttpStore):
    tight = ResumableHttpStore(store.root, owner_quota_bytes=64, disk_headroom_bytes=0)
    tight.create(FOUNDER_USER_ID, declared_total_bytes=64)
    with pytest.raises(ResumableHttpError) as quota:
        tight.create(FOUNDER_USER_ID, declared_total_bytes=1)
    assert quota.value.code == "owner_quota_exceeded"
    dry = ResumableHttpStore(store.root / "dry", disk_headroom_bytes=100, disk_free_fn=lambda _p: 10)
    with pytest.raises(ResumableHttpError) as disk:
        dry.create(FOUNDER_USER_ID, declared_total_bytes=32)
    assert disk.value.code == "disk_headroom_insufficient"
    created = store.create(FOUNDER_USER_ID, declared_total_bytes=8, declared_filename="tiny.bin")
    session_id = uuid.UUID(created["session_id"])
    with pytest.raises(ResumableHttpError) as part:
        store.write_part(
            FOUNDER_USER_ID,
            session_id,
            b"x" * (HTTP_PART_MAX_BYTES + 1),
            expected_offset=0,
        )
    assert part.value.http_status == 413
    with pytest.raises(ResumableHttpError) as name:
        store.create(FOUNDER_USER_ID, declared_total_bytes=8, declared_filename="../secret.bin")
    assert name.value.code == "invalid_filename"


def test_abort_releases_quota_and_declared_5gb_is_not_support(tmp_path: Path):
    store = ResumableHttpStore(tmp_path / "q", owner_quota_bytes=TARGET_FILE_BYTES, disk_headroom_bytes=0)
    created = store.create(FOUNDER_USER_ID, declared_total_bytes=TARGET_FILE_BYTES, declared_filename="big.bin")
    assert created["declared_meets_5gb_target"] is True
    assert created["five_gb_supported"] is False
    assert created["five_by_five_gb_measured"] is False
    assert store.reserved_bytes(FOUNDER_USER_ID) == TARGET_FILE_BYTES
    aborted = store.abort(FOUNDER_USER_ID, uuid.UUID(created["session_id"]))
    assert aborted["outcome"] == "aborted"
    assert store.reserved_bytes(FOUNDER_USER_ID) == 0
    with pytest.raises(ResumableHttpError) as oversized:
        store.create(FOUNDER_USER_ID, declared_total_bytes=TARGET_FILE_BYTES + 1)
    assert oversized.value.code == "declared_exceeds_http_cap"


def test_http_founder_cycle_and_restart(store: ResumableHttpStore):
    client = TestClient(_app(store))
    created = client.post("/api/library/resumable/sessions", json={"declared_total_bytes": 100, "declared_filename": "cycle.bin"})
    assert created.status_code == 200, created.text
    body = created.json()
    session_id = body["session_id"]
    assert body["five_gb_supported"] is False
    assert body["http_part_max_bytes"] == HTTP_PART_MAX_BYTES
    first = client.put(
        f"/api/library/resumable/sessions/{session_id}/parts",
        content=b"A" * 64,
        headers={"Upload-Offset": "0"},
    )
    assert first.status_code == 200
    restarted = TestClient(_app(ResumableHttpStore(store.root)))
    status = restarted.get(f"/api/library/resumable/sessions/{session_id}")
    assert status.status_code == 200
    assert status.json()["offset"] == 64
    second = restarted.put(
        f"/api/library/resumable/sessions/{session_id}/parts",
        content=b"B" * 36,
        headers={"Upload-Offset": "64"},
    )
    assert second.status_code == 200
    done = restarted.post(f"/api/library/resumable/sessions/{session_id}/finalize")
    assert done.status_code == 200
    payload = done.json()
    assert payload["outcome"] == "stored"
    assert payload["sha256"] == hashlib.sha256(b"A" * 64 + b"B" * 36).hexdigest()
    assert payload["indexed"] is False
    listed = restarted.get("/api/library/resumable/sessions")
    assert listed.status_code == 200
    assert listed.json()["sessions"][0]["session_id"] == session_id


def test_http_requires_and_validates_upload_offset(store: ResumableHttpStore):
    client = TestClient(_app(store))
    created = client.post("/api/library/resumable/sessions", json={"declared_total_bytes": 4})
    session_id = created.json()["session_id"]
    missing = client.put(f"/api/library/resumable/sessions/{session_id}/parts", content=b"data")
    assert missing.status_code == 400
    assert missing.json()["detail"]["code"] == "missing_upload_offset"
    invalid = client.put(
        f"/api/library/resumable/sessions/{session_id}/parts",
        content=b"data",
        headers={"Upload-Offset": "-1"},
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "invalid_upload_offset"


def test_http_rejects_non_founder_oversize_and_traversal(store: ResumableHttpStore):
    founder = TestClient(_app(store))
    created = founder.post("/api/library/resumable/sessions", json={"declared_total_bytes": 32})
    session_id = created.json()["session_id"]
    outsider = TestClient(_app(store, user=_Other()))
    assert outsider.get("/api/library/resumable/sessions").status_code == 403
    assert outsider.get(f"/api/library/resumable/sessions/{session_id}").status_code == 403
    too_big = founder.put(
        f"/api/library/resumable/sessions/{session_id}/parts",
        content=b"x" * (HTTP_PART_MAX_BYTES + 1),
        headers={"Upload-Offset": "0"},
    )
    assert too_big.status_code == 413
    assert founder.get("/api/library/resumable/sessions/not-a-uuid").status_code == 400
    incomplete = founder.post(f"/api/library/resumable/sessions/{session_id}/finalize")
    assert incomplete.status_code == 409


def test_http_limits_and_quota_ceiling_unchanged():
    assert library.MAX_UPLOAD_BYTES == 60 * 1024 * 1024
    caddy = (REPO_ROOT / "Caddyfile").read_text()
    assert "max_size 30MB" in caddy
    assert OWNER_QUOTA_BYTES == 5 * TARGET_FILE_BYTES
    assert HTTP_PART_MAX_BYTES == 64 * 1024
