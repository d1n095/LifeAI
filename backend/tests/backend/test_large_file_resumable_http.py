"""Production resumable HTTP on the #313 spool. Does not certify 5 GB."""

from __future__ import annotations

import hashlib
import uuid
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
    store.write_part(FOUNDER_USER_ID, session_id, first)
    restarted = ResumableHttpStore(store.root)
    status = restarted.get(FOUNDER_USER_ID, session_id)
    assert status["offset"] == len(first)
    remaining = payload[len(first) :]
    while remaining:
        chunk = remaining[: HTTP_PART_MAX_BYTES]
        restarted.write_part(FOUNDER_USER_ID, session_id, chunk)
        remaining = remaining[len(chunk) :]
    done = restarted.finalize(FOUNDER_USER_ID, session_id)
    assert done["outcome"] == "stored"
    assert done["sha256"] == hashlib.sha256(payload).hexdigest()
    assert done["indexed"] is False
    assert done["five_gb_supported"] is False


def test_owner_isolation_and_path_traversal(store: ResumableHttpStore):
    created = store.create(FOUNDER_USER_ID, declared_total_bytes=32, declared_filename="own.bin")
    session_id = uuid.UUID(created["session_id"])
    store.write_part(FOUNDER_USER_ID, session_id, b"x" * 32)
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
        store.write_part(FOUNDER_USER_ID, session_id, b"x" * (HTTP_PART_MAX_BYTES + 1))
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
    first = client.put(f"/api/library/resumable/sessions/{session_id}/parts", content=b"A" * 64)
    assert first.status_code == 200
    restarted = TestClient(_app(ResumableHttpStore(store.root)))
    status = restarted.get(f"/api/library/resumable/sessions/{session_id}")
    assert status.status_code == 200
    assert status.json()["offset"] == 64
    second = restarted.put(f"/api/library/resumable/sessions/{session_id}/parts", content=b"B" * 36)
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
