"""#315 ACTUAL HTTP integration acceptance — independent examiner suite.

Subject: #315 head SHA 0741a3d5 (docs-only child of product SHA 292a091; the resumable
router/store tree is byte-identical to 292a091, which fc315_v1 already storage-tested).

fc315_v1 (S1..S12) drove the STORE object directly + a source-inspection auth check. THIS
suite drives the REAL FastAPI routes (app.routers.library_resumable) through an authenticated
TestClient: genuine login cookies + CSRF, real request/response status codes, real streamed
part bodies, real cross-instance restart, HTTP concurrency, and the route-only boundaries
S1..S12 never reached (require_founder/CSRF at the wire, the _read_bounded_part 413 paths,
ResumableHttpError->HTTPException status mapping, cross-owner-by-path, quota/disk surfaced as
413/507, abort/finalize race, corrupted/truncated spool truthfulness, and the inherited
per-minute limiter). No 5 GB run, no production mutation, no external call. first_cycle_go=false.

Fixtures from the target's tests.conftest (-p tests.conftest): client, db_session,
superuser_db, make_verified_user.
"""
from __future__ import annotations

import hashlib
import threading
import uuid

import pytest

from _guards import _no_egress  # noqa: F401  (autouse network guard)

PART = 64 * 1024  # HTTP_PART_MAX_BYTES
URL = "/api/library/resumable/sessions"


# --------------------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def _isolate_storage(monkeypatch, tmp_path):
    """Point the production store at a per-test tmp dir so sessions never leak across tests
    (the route reads get_settings().storage_root fresh on every request)."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "storage_root", str(tmp_path / "store"))
    return tmp_path / "store"


@pytest.fixture(autouse=True)
def _limiter_off(monkeypatch):
    """Isolate route-to-store behaviour from the orthogonal per-minute limiter (inherited from
    #313, 10/min on the `library` scope). HT13 re-enables it to prove the route carries it."""
    from app.limiter import limiter

    monkeypatch.setattr(limiter, "enabled", False)
    return limiter


def _founder_creds():
    from app.config import get_settings

    s = get_settings()
    return s.founder_email, s.founder_password


def _login(client, email, password):
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, (res.status_code, res.text)
    return res.json()["csrf_token"]


def _login_founder(client):
    email, pw = _founder_creds()
    return _login(client, email, pw)


def _founder_id():
    from app.founder import FOUNDER_USER_ID

    return FOUNDER_USER_ID


def _hdr(csrf):
    return {"X-CSRF-Token": csrf}


# --------------------------------------------------------------------------- HT1 happy path
def test_HT1_founder_full_lifecycle_over_http(client):
    csrf = _login_founder(client)
    payload = b"LIFEAI-315-HTTP-" * 300  # 4800 bytes
    r = client.post(URL, json={"declared_total_bytes": len(payload), "declared_filename": "a.bin"}, headers=_hdr(csrf))
    assert r.status_code == 200, r.text
    body = r.json()
    sid = body["session_id"]
    assert body["five_gb_supported"] is False and body["declared_filename"] == "a.bin"

    off = 0
    for i in range(0, len(payload), PART):
        chunk = payload[i:i + PART]
        pr = client.put(f"{URL}/{sid}/parts", content=chunk, headers=_hdr(csrf))
        assert pr.status_code == 200, pr.text
        off += len(chunk)
        assert pr.json()["offset"] == off

    g = client.get(f"{URL}/{sid}")
    assert g.status_code == 200 and g.json()["offset"] == len(payload)

    f = client.post(f"{URL}/{sid}/finalize", headers=_hdr(csrf))
    assert f.status_code == 200, f.text
    fb = f.json()
    assert fb["outcome"] == "stored" and fb["stored"] is True
    assert fb["sha256"] == hashlib.sha256(payload).hexdigest()
    assert fb["indexed"] is False and fb["understood"] is False

    lst = client.get(URL)
    assert lst.status_code == 200 and any(s["session_id"] == sid for s in lst.json()["sessions"])

    d = client.delete(f"{URL}/{sid}", headers=_hdr(csrf))
    assert d.status_code == 200 and d.json()["outcome"] == "aborted"
    assert client.get(f"{URL}/{sid}").status_code == 404


# --------------------------------------------------------------------------- HT2 require_founder at the wire
def test_HT2_unauthenticated_and_nonfounder_rejected(client, make_verified_user):
    # unauthenticated: every route (no cookie) -> 401
    assert client.post(URL, json={"declared_total_bytes": 10}).status_code == 401
    assert client.get(URL).status_code == 401
    assert client.get(f"{URL}/{uuid.uuid4()}").status_code == 401
    assert client.put(f"{URL}/{uuid.uuid4()}/parts", content=b"x").status_code == 401
    assert client.post(f"{URL}/{uuid.uuid4()}/finalize").status_code == 401
    assert client.delete(f"{URL}/{uuid.uuid4()}").status_code == 401

    # authenticated NON-founder -> 403 (role+id pinned to FOUNDER_USER_ID)
    user, pw = make_verified_user(role="member")
    csrf = _login(client, user.email, pw)
    assert client.get(URL).status_code == 403
    assert client.post(URL, json={"declared_total_bytes": 10}, headers=_hdr(csrf)).status_code == 403


# --------------------------------------------------------------------------- HT3 CSRF on mutating routes
def test_HT3_mutating_routes_require_csrf(client):
    csrf = _login_founder(client)
    # POST without the CSRF header is rejected even though the session cookie is valid
    assert client.post(URL, json={"declared_total_bytes": 100}).status_code == 403
    # ...and accepted with it
    assert client.post(URL, json={"declared_total_bytes": 100}, headers=_hdr(csrf)).status_code == 200


# --------------------------------------------------------------------------- HT4 malformed input over HTTP
def test_HT4_malformed_parts_and_ids(client):
    csrf = _login_founder(client)
    # non-UUID session id -> 400 invalid_session_id (GET is safe, no CSRF needed)
    assert client.get(f"{URL}/not-a-uuid").status_code == 400
    assert client.put(f"{URL}/not-a-uuid/parts", content=b"x", headers=_hdr(csrf)).json()["detail"]["code"] == "invalid_session_id"

    sid = client.post(URL, json={"declared_total_bytes": 200}, headers=_hdr(csrf)).json()["session_id"]

    # oversize via Content-Length (httpx sets it from the bytes body) -> 413 before streaming
    big = client.put(f"{URL}/{sid}/parts", content=b"x" * (PART + 1), headers=_hdr(csrf))
    assert big.status_code == 413 and big.json()["detail"]["code"] == "part_too_large"

    # oversize via CHUNKED stream (no Content-Length) -> 413 inside the stream guard
    gen = (b"y" * 4096 for _ in range((PART // 4096) + 2))
    chunked = client.put(f"{URL}/{sid}/parts", content=gen, headers=_hdr(csrf))
    assert chunked.status_code == 413

    # empty part -> store rejects -> 409 part_rejected
    empty = client.put(f"{URL}/{sid}/parts", content=b"", headers=_hdr(csrf))
    assert empty.status_code == 409 and empty.json()["detail"]["code"] == "part_rejected"

    # overflow declared_total -> 409 part_rejected
    client.put(f"{URL}/{sid}/parts", content=b"a" * 200, headers=_hdr(csrf))
    over = client.put(f"{URL}/{sid}/parts", content=b"b" * 1, headers=_hdr(csrf))
    assert over.status_code == 409

    # finalize incomplete -> 409 incomplete
    sid2 = client.post(URL, json={"declared_total_bytes": 50}, headers=_hdr(csrf)).json()["session_id"]
    client.put(f"{URL}/{sid2}/parts", content=b"z" * 10, headers=_hdr(csrf))
    inc = client.post(f"{URL}/{sid2}/finalize", headers=_hdr(csrf))
    assert inc.status_code == 409 and inc.json()["detail"]["code"] == "incomplete"


# --------------------------------------------------------------------------- HT5 cross-owner by path
def test_HT5_cross_owner_session_id_is_not_found(client, _isolate_storage):
    """A session that exists ON DISK under a different owner is invisible to the founder route:
    the owner is derived from the authenticated token, never from the URL path."""
    from pathlib import Path

    from app.large_file_lane.http_store import ResumableHttpStore, SESSION_SUBDIR

    other_owner = uuid.uuid4()
    store = ResumableHttpStore(Path(str(_isolate_storage)) / SESSION_SUBDIR)
    victim = store.create(other_owner, declared_total_bytes=100, declared_filename="secret.bin")
    sid = victim["session_id"]

    csrf = _login_founder(client)
    assert client.get(f"{URL}/{sid}").status_code == 404                       # cannot read
    assert client.put(f"{URL}/{sid}/parts", content=b"x", headers=_hdr(csrf)).status_code == 404  # cannot write
    assert client.post(f"{URL}/{sid}/finalize", headers=_hdr(csrf)).status_code == 404            # cannot finalize
    assert client.delete(f"{URL}/{sid}", headers=_hdr(csrf)).status_code == 404                   # cannot abort
    # and it never appears in the founder's own listing
    assert all(s["session_id"] != sid for s in client.get(URL).json()["sessions"])


# --------------------------------------------------------------------------- HT6 concurrent HTTP writes
def test_HT6_concurrent_http_puts_serialize_exactly(client):
    from app.main import app
    from fastapi.testclient import TestClient

    csrf = _login_founder(client)
    nthreads, per = 4, 8
    total = nthreads * per * PART
    sid = client.post(URL, json={"declared_total_bytes": total}, headers=_hdr(csrf)).json()["session_id"]
    tok = client.cookies.get("access_token")

    errors: list = []
    statuses: list[int] = []
    lock = threading.Lock()

    def worker(tag: int):
        c = TestClient(app, base_url="https://testserver")
        c.cookies.set("access_token", tok)
        try:
            for _ in range(per):
                r = c.put(f"{URL}/{sid}/parts", content=bytes([tag % 256]) * PART, headers=_hdr(csrf))
                with lock:
                    statuses.append(r.status_code)
        except Exception as e:  # noqa: BLE001
            with lock:
                errors.append(e)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(nthreads)]
    [t.start() for t in ts]
    [t.join() for t in ts]

    assert not errors, errors[:3]
    assert all(s == 200 for s in statuses), [s for s in statuses if s != 200]
    # flock serialization through the route: declared total reached EXACTLY, never exceeded
    assert client.get(f"{URL}/{sid}").json()["offset"] == total
    assert client.post(f"{URL}/{sid}/finalize", headers=_hdr(csrf)).json()["outcome"] == "stored"


# --------------------------------------------------------------------------- HT7 cross-instance restart/resume
def test_HT7_restart_resume_across_client_instances(client):
    from app.main import app
    from fastapi.testclient import TestClient

    csrf = _login_founder(client)
    data = b"RESUME-315-" * 1000  # 11000 bytes
    sid = client.post(URL, json={"declared_total_bytes": len(data)}, headers=_hdr(csrf)).json()["session_id"]

    half = (len(data) // 2 // PART + 1) * PART
    written = 0
    for i in range(0, half, PART):
        client.put(f"{URL}/{sid}/parts", content=data[i:i + PART], headers=_hdr(csrf))
        written = min(half, i + PART)
    written = min(written, len(data))

    # "process restart": a brand-new client/app handle over the SAME storage_root
    tok = client.cookies.get("access_token")
    c2 = TestClient(app, base_url="https://testserver")
    c2.cookies.set("access_token", tok)
    resumed = c2.get(f"{URL}/{sid}")
    assert resumed.status_code == 200
    off = resumed.json()["offset"]
    assert off == written and off > 0          # progress survived purely via manifest + spool on disk

    for i in range(off, len(data), PART):
        assert c2.put(f"{URL}/{sid}/parts", content=data[i:i + PART], headers=_hdr(csrf)).status_code == 200
    f = c2.post(f"{URL}/{sid}/finalize", headers=_hdr(csrf))
    assert f.status_code == 200 and f.json()["sha256"] == hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- HT8 quota exhaustion -> 413
def test_HT8_owner_quota_exhaustion_surfaces_413(client, _isolate_storage):
    from pathlib import Path

    from app.main import app
    from app.large_file_lane.http_store import ResumableHttpStore, SESSION_SUBDIR
    from app.routers.library_resumable import get_resumable_store

    root = Path(str(_isolate_storage)) / SESSION_SUBDIR
    app.dependency_overrides[get_resumable_store] = lambda: ResumableHttpStore(
        root, owner_quota_bytes=2000, declared_max_bytes=2000)
    try:
        csrf = _login_founder(client)
        assert client.post(URL, json={"declared_total_bytes": 1500}, headers=_hdr(csrf)).status_code == 200
        r = client.post(URL, json={"declared_total_bytes": 1000}, headers=_hdr(csrf))
        assert r.status_code == 413 and r.json()["detail"]["code"] == "owner_quota_exceeded"
    finally:
        app.dependency_overrides.pop(get_resumable_store, None)


# --------------------------------------------------------------------------- HT9 disk headroom -> 507
def test_HT9_disk_headroom_surfaces_507(client, _isolate_storage):
    from pathlib import Path

    from app.main import app
    from app.large_file_lane.http_store import ResumableHttpStore, SESSION_SUBDIR
    from app.routers.library_resumable import get_resumable_store

    root = Path(str(_isolate_storage)) / SESSION_SUBDIR
    app.dependency_overrides[get_resumable_store] = lambda: ResumableHttpStore(
        root, disk_free_fn=lambda p: 1024, disk_headroom_bytes=512 * 1024 * 1024)
    try:
        csrf = _login_founder(client)
        r = client.post(URL, json={"declared_total_bytes": 5000}, headers=_hdr(csrf))
        assert r.status_code == 507 and r.json()["detail"]["code"] == "disk_headroom_insufficient"
    finally:
        app.dependency_overrides.pop(get_resumable_store, None)


# --------------------------------------------------------------------------- HT10 abort/finalize race
def test_HT10_abort_finalize_race_is_coherent(client):
    from app.main import app
    from fastapi.testclient import TestClient

    csrf = _login_founder(client)
    data = b"q" * (4 * PART)
    sid = client.post(URL, json={"declared_total_bytes": len(data)}, headers=_hdr(csrf)).json()["session_id"]
    for i in range(0, len(data), PART):
        client.put(f"{URL}/{sid}/parts", content=data[i:i + PART], headers=_hdr(csrf))
    tok = client.cookies.get("access_token")

    results: dict[str, int] = {}
    lock = threading.Lock()

    def do(kind: str):
        c = TestClient(app, base_url="https://testserver")
        c.cookies.set("access_token", tok)
        if kind == "finalize":
            r = c.post(f"{URL}/{sid}/finalize", headers=_hdr(csrf))
        else:
            r = c.delete(f"{URL}/{sid}", headers=_hdr(csrf))
        with lock:
            results[kind] = r.status_code

    ts = [threading.Thread(target=do, args=(k,)) for k in ("finalize", "abort")]
    [t.start() for t in ts]
    [t.join() for t in ts]

    # Both operations take the SAME per-session flock, so neither can corrupt the other. Each
    # call returns a coherent HTTP status (no 5xx), and the terminal on-disk state agrees with
    # what the responses claim: if abort won/ran the session is gone (GET 404); if only
    # finalize ran it is stored.
    assert all(code < 500 for code in results.values()), results
    assert results["finalize"] in (200, 404) and results["abort"] in (200, 404), results
    final = client.get(f"{URL}/{sid}")
    if results["abort"] == 200:
        assert final.status_code == 404        # aborted => removed
    else:
        assert final.status_code == 200 and final.json().get("finalized") is True


# --------------------------------------------------------------------------- HT11 corrupted / truncated spool truthfulness
def _spool_path(storage_root, sid):
    from pathlib import Path

    from app.large_file_lane.http_store import SESSION_SUBDIR
    from app.large_file_lane.session import SPOOL

    return Path(str(storage_root)) / SESSION_SUBDIR / str(_founder_id()) / sid / SPOOL


def test_HT11a_corrupted_same_length_spool_reports_hash_of_stored_bytes(client, _isolate_storage):
    """No client-declared content hash is accepted at create, so finalize can only report the
    sha256 of whatever bytes are actually stored. A same-length corruption changes the reported
    hash — the store never claims the original content; it truthfully hashes what is on disk."""
    csrf = _login_founder(client)
    data = b"c" * (2 * PART)
    sid = client.post(URL, json={"declared_total_bytes": len(data)}, headers=_hdr(csrf)).json()["session_id"]
    for i in range(0, len(data), PART):
        client.put(f"{URL}/{sid}/parts", content=data[i:i + PART], headers=_hdr(csrf))

    sp = _spool_path(_isolate_storage, sid)
    raw = bytearray(sp.read_bytes())
    raw[0] ^= 0xFF                     # flip one byte, same length
    sp.write_bytes(bytes(raw))

    f = client.post(f"{URL}/{sid}/finalize", headers=_hdr(csrf))
    assert f.status_code == 200
    assert f.json()["sha256"] == hashlib.sha256(bytes(raw)).hexdigest()
    assert f.json()["sha256"] != hashlib.sha256(data).hexdigest()


def test_HT11b_truncated_spool_finalize_behaviour(client, _isolate_storage):
    """A spool truncated OUT-OF-BAND to fewer bytes than manifest.offset: finalize's
    completeness gate reads manifest.offset, not the live spool length, so it still reports
    `stored`/`complete` with a sha over fewer bytes than declared_total. Documented as a
    route-to-store integrity-reporting observation (see VERDICT); asserted against the real
    observed behaviour so a future re-verification against the live spool length flips it."""
    csrf = _login_founder(client)
    data = b"t" * (2 * PART)
    sid = client.post(URL, json={"declared_total_bytes": len(data)}, headers=_hdr(csrf)).json()["session_id"]
    for i in range(0, len(data), PART):
        client.put(f"{URL}/{sid}/parts", content=data[i:i + PART], headers=_hdr(csrf))

    sp = _spool_path(_isolate_storage, sid)
    full = sp.read_bytes()
    sp.write_bytes(full[: len(full) - PART])   # drop the last part on disk, leave manifest.offset intact

    f = client.post(f"{URL}/{sid}/finalize", headers=_hdr(csrf))
    body = f.json()
    # Observed behaviour today: declared completeness is manifest-driven, so finalize succeeds
    # and reports stored with a hash over the (now shorter) on-disk bytes. Record it exactly.
    assert f.status_code == 200
    assert body["outcome"] == "stored"
    assert body["offset"] == len(data)                                   # manifest still says complete
    assert body["sha256"] == hashlib.sha256(full[: len(full) - PART]).hexdigest()  # hash is of fewer bytes


# --------------------------------------------------------------------------- HT12 stored-vs-indexed honesty at the wire
def test_HT12_http_responses_never_overclaim(client):
    csrf = _login_founder(client)
    data = b"h" * PART
    create = client.post(URL, json={"declared_total_bytes": len(data)}, headers=_hdr(csrf)).json()
    sid = create["session_id"]
    client.put(f"{URL}/{sid}/parts", content=data, headers=_hdr(csrf))
    get = client.get(f"{URL}/{sid}").json()
    lst = client.get(URL).json()
    fin = client.post(f"{URL}/{sid}/finalize", headers=_hdr(csrf)).json()

    for body in (create, get, fin, *lst["sessions"]):
        assert body["five_gb_supported"] is False
        assert body["five_by_five_gb_measured"] is False
        assert body["stored_is_not_indexed"] is True
        assert body["caddy_request_body_unchanged"] is True
        assert body["first_cycle_go"] is False
    assert lst["five_gb_supported"] is False
    # finalize must not claim indexing or understanding of the stored bytes
    assert fin["stored"] is True and fin["indexed"] is False and fin["understood"] is False


# --------------------------------------------------------------------------- HT13 inherited per-minute limiter is wired
def test_HT13_route_carries_library_rate_limit(client, monkeypatch):
    """Prove the production route actually carries the #313-inherited per-minute limiter on the
    `library` scope — re-enable it (the suite otherwise disables it) and confirm the N+1th
    create inside the window is a 429, not an unbounded accept."""
    from app.config import get_settings
    from app.limiter import limiter

    monkeypatch.setattr(limiter, "enabled", True)
    per_min = get_settings().rate_limit_library_import_per_minute
    csrf = _login_founder(client)

    codes = []
    for _ in range(per_min + 3):
        codes.append(client.post(URL, json={"declared_total_bytes": 100}, headers=_hdr(csrf)).status_code)
    assert 429 in codes, codes
    assert codes.count(200) <= per_min, codes
