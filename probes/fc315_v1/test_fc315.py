"""#315 production resumable HTTP — independent examination (examiner-owned).

Subject: #315 product SHA 292a091 (based on #313). Drives the REAL owner-scoped store
app.large_file_lane.http_store.ResumableHttpStore + the router's auth contract
(app.routers.library_resumable). SMALL fixtures only; injectable disk_free_fn simulates exhaustion.

Does NOT claim 5 GB support: the declared ceiling (TARGET_FILE_BYTES) is a target, five_gb_not_supported
stays False-flagged, and no 5 GB upload or content processing is performed. No DB, no network, no paid
calls. first_cycle_go=false.
"""
from __future__ import annotations

import hashlib
import threading
import uuid

import pytest

from _guards import egress  # noqa: F401

OA = uuid.uuid4()
OB = uuid.uuid4()


def _store(tmp_path, **kw):
    from app.large_file_lane.http_store import ResumableHttpStore
    return ResumableHttpStore(root=tmp_path / "store", **kw)


def _err(fn):
    from app.large_file_lane.http_store import ResumableHttpError
    try:
        fn()
    except ResumableHttpError as e:
        return e
    raise AssertionError("expected ResumableHttpError")


# ---------------------------------------------------------------------------
# Owner scoping + input validation
# ---------------------------------------------------------------------------

def test_S1_owner_scoped_isolation(tmp_path):
    st = _store(tmp_path)
    sid = uuid.UUID(st.create(OA, declared_total_bytes=1000, declared_filename="a.bin")["session_id"])
    # another owner cannot see or load owner A's session
    e = _err(lambda: st.get(OB, sid))
    assert e.http_status == 404 and e.code == "not_found"
    assert st.list_sessions(OB) == []
    assert len(st.list_sessions(OA)) == 1


def test_S2_filename_and_session_id_validation(tmp_path):
    from app.large_file_lane.http_store import sanitize_filename, parse_session_id, ResumableHttpError
    for bad in ("../etc/passwd", "a/b.txt", "", "x" * 1024):
        with pytest.raises(ResumableHttpError):
            sanitize_filename(bad)
    assert sanitize_filename("report.pdf") == "report.pdf"
    with pytest.raises(ResumableHttpError):
        parse_session_id("not-a-uuid")


def test_S3_session_path_cannot_escape_owner_root(tmp_path):
    st = _store(tmp_path)
    # a session id is parsed as UUID so traversal can't be injected; owner_root/session_root resolve+check
    root = st.session_root(OA, uuid.uuid4())
    assert str(root).startswith(str(st.owner_root(OA)))


# ---------------------------------------------------------------------------
# Quotas + disk exhaustion
# ---------------------------------------------------------------------------

def test_S4_owner_quota_enforced(tmp_path):
    st = _store(tmp_path, owner_quota_bytes=2000, declared_max_bytes=2000)
    st.create(OA, declared_total_bytes=1500, declared_filename="a")
    e = _err(lambda: st.create(OA, declared_total_bytes=1000, declared_filename="b"))
    assert e.http_status == 413 and e.code == "owner_quota_exceeded"


def test_S5_disk_headroom_enforced(tmp_path):
    st = _store(tmp_path, disk_free_fn=lambda p: 1024, disk_headroom_bytes=512 * 1024 * 1024)
    e = _err(lambda: st.create(OA, declared_total_bytes=5000, declared_filename="a"))
    assert e.http_status == 507 and e.code == "disk_headroom_insufficient"  # 507 Insufficient Storage


# ---------------------------------------------------------------------------
# Part bounds + rejection
# ---------------------------------------------------------------------------

def test_S6_part_bounds(tmp_path):
    st = _store(tmp_path, part_max_bytes=64)
    sid = uuid.UUID(st.create(OA, declared_total_bytes=100, declared_filename="a")["session_id"])
    big = _err(lambda: st.write_part(OA, sid, b"x" * 65))
    assert big.http_status == 413 and big.code == "part_too_large"
    st.write_part(OA, sid, b"a" * 64)
    over = _err(lambda: st.write_part(OA, sid, b"b" * 64))   # would exceed declared_total=100
    assert over.http_status == 409 and over.code == "part_rejected"


# ---------------------------------------------------------------------------
# Restart recovery + checksum integrity + stored/indexed/understood
# ---------------------------------------------------------------------------

def test_S7_restart_recovery_and_checksum(tmp_path):
    data = b"LIFEAI-315-" * 500  # 5500 bytes
    st1 = _store(tmp_path, part_max_bytes=1024)
    sid = uuid.UUID(st1.create(OA, declared_total_bytes=len(data), declared_filename="a")["session_id"])
    for i in range(0, 3000, 1024):
        st1.write_part(OA, sid, data[i:i + 1024])
    # fresh store instance over the same root = process restart
    st2 = _store(tmp_path, part_max_bytes=1024)
    resumed = st2.get(OA, sid)
    off = resumed["offset"]
    assert off == 3072 or off == len(data[:3072]) or off > 0
    for i in range(off, len(data), 1024):
        st2.write_part(OA, sid, data[i:i + 1024])
    fin = st2.finalize(OA, sid)
    assert fin["outcome"] == "stored"
    assert fin["sha256"] == hashlib.sha256(data).hexdigest()
    assert fin["indexed"] is False and fin["understood"] is False
    assert fin["five_gb_supported"] is False


# ---------------------------------------------------------------------------
# Finalize incompleteness + idempotency; abort + cleanup
# ---------------------------------------------------------------------------

def test_S8_finalize_incomplete_then_idempotent(tmp_path):
    st = _store(tmp_path, part_max_bytes=64)
    sid = uuid.UUID(st.create(OA, declared_total_bytes=100, declared_filename="a")["session_id"])
    st.write_part(OA, sid, b"a" * 50)
    inc = _err(lambda: st.finalize(OA, sid))
    assert inc.http_status == 409 and inc.code == "incomplete"
    st.write_part(OA, sid, b"b" * 50)
    assert st.finalize(OA, sid)["outcome"] == "stored"
    again = st.finalize(OA, sid)
    assert again["outcome"] == "stored" and again.get("already_finalized") is True


def test_S9_abort_and_cleanup(tmp_path):
    st = _store(tmp_path)
    sid = uuid.UUID(st.create(OA, declared_total_bytes=100, declared_filename="a")["session_id"])
    assert st.abort(OA, sid)["outcome"] == "aborted"
    assert _err(lambda: st.get(OA, sid)).http_status == 404      # dir removed
    assert _err(lambda: st.abort(OA, sid)).http_status == 404    # second abort 404


# ---------------------------------------------------------------------------
# Concurrent writes serialized by the session lock (flock)
# ---------------------------------------------------------------------------

def test_S10_concurrent_writes_serialized(tmp_path):
    st = _store(tmp_path, part_max_bytes=1024)
    total = 20 * 1024
    sid = uuid.UUID(st.create(OA, declared_total_bytes=total, declared_filename="a")["session_id"])
    errors = []

    def writer():
        for _ in range(10):
            try:
                st.write_part(OA, sid, b"z" * 1024)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    ts = [threading.Thread(target=writer) for _ in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    # flock serializes appends; the declared total is reached exactly, never exceeded (no corruption)
    final = st.get(OA, sid)
    assert final["offset"] == total, (final["offset"], errors[:2])


# ---------------------------------------------------------------------------
# Router auth contract: owner derived from authenticated founder, not client input
# ---------------------------------------------------------------------------

def test_S11_router_derives_owner_from_authenticated_founder():
    import inspect
    import app.routers.library_resumable as lr
    src = inspect.getsource(lr)
    # every handler authenticates as founder and scopes by user.id (never a client-supplied owner)
    assert "require_founder" in src
    assert "store.create(" in src and "user.id" in src
    # no endpoint accepts an owner_id from the request body/query
    assert "owner_id=" not in src.replace("user.id", ""), "router may accept a client-supplied owner_id"


# ---------------------------------------------------------------------------
# 5 GB honesty (no claim without measured tests + content processing)
# ---------------------------------------------------------------------------

def test_S12_five_gb_not_claimed(tmp_path):
    st = _store(tmp_path)
    row = st.create(OA, declared_total_bytes=1000, declared_filename="a")
    assert row["five_gb_supported"] is False and row["five_by_five_gb_measured"] is False
    assert row["caddy_request_body_unchanged"] is True
