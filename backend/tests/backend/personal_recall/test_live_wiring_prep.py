import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.personal_recall.index import RecallIndexError, SnapshotStoragePolicy
from app.personal_recall.snapshot_protection import DeterministicTestSnapshotProtector, RecallSnapshotProtectionError
from app.personal_recall.workers import ChangeKind, RecallIndexWorker, SourceChange
from app.personal_recall.routes_prep import build_recall_router
from test_integration_bridge import _context, _item, _service, MutableResolver, MutableRegistry


def worker(tmp_path, loader, **kwargs):
    tmp_path.chmod(0o700)
    return RecallIndexWorker(tmp_path / "worker.sqlite", policy=SnapshotStoragePolicy(tmp_path), owner_id="alice",
                             protector=DeterministicTestSnapshotProtector(), authorize=kwargs.pop("authorize", lambda owner: True),
                             load_source=loader, test_only=True, **kwargs)


def event(n="1", kind=ChangeKind.NEW_MESSAGE, source="item-1", owner="alice"):
    return SourceChange(n, owner, source, kind)


@pytest.mark.parametrize("kind", list(ChangeKind))
def test_event_kinds_refresh_only_canonical_source_and_restart(tmp_path, kind):
    calls = []
    current = [_item()]
    def loader(**keys):
        calls.append(keys)
        return current
    w = worker(tmp_path, loader)
    w.enqueue(event(kind=kind))
    assert w.run_once()
    current[:] = []
    w.enqueue(event("2", kind))
    restarted = worker(tmp_path, loader)
    assert restarted.run_once()
    assert restarted.read_items() == []
    assert not restarted.run_once()
    assert calls == [dict(owner_id="alice", family=kind.family, source_id="item-1")] * 2


def test_edit_is_incremental_and_duplicate_event_is_idempotent(tmp_path):
    contents = {"item-1": _item(), "item-2": _item(item_id="item-2")}
    calls = []
    def loader(**keys):
        calls.append(keys["source_id"])
        return [contents[keys["source_id"]]]
    w = worker(tmp_path, loader)
    for n in ("item-1", "item-2"):
        w.enqueue(event(n, source=n))
        assert w.run_once()
    contents["item-1"].text = "edited"
    change = event("edit", ChangeKind.EDITED_MESSAGE)
    w.enqueue(change)
    w.enqueue(change)
    assert w.run_once()
    assert not w.run_once()
    assert calls == ["item-1", "item-2", "item-1"]
    assert {i.item_id: i.text for i in w.read_items()}["item-1"] == "edited"
    with pytest.raises(RecallIndexError):
        w.enqueue(event("edit", source="item-2"))


def test_failed_or_revoked_work_is_not_acknowledged(tmp_path):
    allowed = [True]
    def loader(**keys):
        allowed[0] = False
        return [_item()]
    w = worker(tmp_path, loader, authorize=lambda owner: allowed[0])
    w.enqueue(event())
    with pytest.raises(PermissionError):
        w.run_once()
    allowed[0] = True
    resumed = worker(tmp_path, lambda **keys: [_item()])
    assert resumed.read_items() == []
    assert resumed.run_once()
    assert len(resumed.read_items()) == 1


@pytest.mark.parametrize("bad", [_item(owner="bob"), _item(item_id="other")])
def test_worker_rejects_cross_owner_and_cross_source_loader(tmp_path, bad):
    w = worker(tmp_path, lambda **keys: [bad])
    with pytest.raises(RecallIndexError):
        w.enqueue(event(owner="bob"))
    w.enqueue(event())
    with pytest.raises(RecallIndexError):
        w.run_once()
    assert w.read_items() == []


def test_two_workers_ack_once_and_recover_same_state(tmp_path):
    calls = []
    def loader(**keys):
        calls.append(keys)
        return [_item()]
    w = worker(tmp_path, loader)
    other = worker(tmp_path, loader)
    w.enqueue(event())
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(lambda instance: instance.run_once(), [w, other]))
    assert sorted(outcomes) == [False, True]
    assert len(calls) == 1
    assert w.read_items() == other.read_items()


def test_tamper_and_generation_swap_fail_closed(tmp_path):
    w = worker(tmp_path, lambda **keys: [_item()])
    w.enqueue(event())
    w.run_once()
    with sqlite3.connect(w.path) as db:
        db.execute("UPDATE projections SET seq=seq+1")
    with pytest.raises(RecallSnapshotProtectionError):
        w.read_items()


def test_production_worker_requires_real_key_hierarchy(tmp_path):
    with pytest.raises(RecallIndexError, match="key hierarchy"):
        RecallIndexWorker(tmp_path / "prod", policy=SnapshotStoragePolicy(tmp_path), owner_id="alice",
                          protector=DeterministicTestSnapshotProtector(), authorize=lambda o: True, load_source=lambda **k: [])


@pytest.mark.parametrize("enabled,allowed", [(False, True), (True, False)])
def test_route_gate_precedes_authority_and_service(enabled, allowed):
    def forbidden():
        pytest.fail("disabled route invoked dependency")
    app = FastAPI()
    app.include_router(build_recall_router(service_dependency=forbidden, authorization_dependency=forbidden,
                                         authorization_gate=lambda: allowed, enabled=enabled))
    client = TestClient(app)
    assert client.get("/personal-recall/status").status_code == 404
    assert client.post("/personal-recall/query", json={"query": "test"}).status_code == 404


def test_metadata_http_handoff_omits_content_and_body_cannot_forge_grant():
    from app.personal_recall.authorization import DisclosureLevel
    context = _context(disclosure_level=DisclosureLevel.METADATA)
    service = _service(MutableResolver(context), MutableRegistry())
    app = FastAPI()
    app.include_router(build_recall_router(service_dependency=lambda: service, authorization_dependency=lambda: context,
                                         authorization_gate=lambda: True, enabled=True))
    client = TestClient(app)
    response = client.post("/personal-recall/query", json={"query": "tandkrämsrecept"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["contract_version"] == "personal-recall-evidence-v1"
    assert payload["handoff"]["evidence"][0]["snippet"] is None
    assert "nano HAp" not in response.text
    assert client.post("/personal-recall/query", json={"query": "test", "owner_id": "bob"}).status_code == 422


def test_disclosure_uses_fresh_wall_clock(monkeypatch):
    from datetime import timedelta
    import app.personal_recall.service as module
    from app.personal_recall.authorization import RecallAuthorizationError
    context = _context()
    class Clock:
        calls = 0
        @classmethod
        def now(cls, tz):
            cls.calls += 1
            return context.expires_at + timedelta(seconds=1) if cls.calls > 1 else context.session_issued_at
    monkeypatch.setattr(module, "datetime", Clock)
    # require_fresh_authority owns its own clock; patch that too.
    import app.personal_recall.authorization as auth
    monkeypatch.setattr(auth, "datetime", Clock)
    with pytest.raises(RecallAuthorizationError, match="expired"):
        _service(MutableResolver(context), MutableRegistry()).query(module.RecallQueryRequest("tandkrämsrecept", context))


def test_process_crash_between_projection_and_ack_rolls_back(tmp_path):
    import multiprocessing
    w = worker(tmp_path, lambda **keys: [_item()])
    w.enqueue(event())
    process = multiprocessing.get_context("spawn").Process(target=_crash_worker, args=(tmp_path,))
    process.start()
    process.join(10)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("crash worker hung")
    assert process.exitcode == 23
    restarted = worker(tmp_path, lambda **keys: [_item()])
    assert restarted.read_items() == []
    assert restarted.run_once()
    assert len(restarted.read_items()) == 1
    assert not restarted.run_once()


@pytest.mark.parametrize("operation", ["query", "open"])
def test_revoke_during_canonical_registry_validation_blocks_disclosure(operation):
    from app.personal_recall.authorization import RecallAuthorizationError
    from app.personal_recall.service import RecallQueryRequest, RecallOpenSourceRequest
    context = _context()
    resolver = MutableResolver(context)
    registry = MutableRegistry()
    service = _service(resolver, registry)
    response = service.query(RecallQueryRequest("tandkrämsrecept", context))
    original = registry.resolve
    def revoke(**kwargs):
        resolver.current = None
        return original(**kwargs)
    registry.resolve = revoke
    with pytest.raises(RecallAuthorizationError):
        if operation == "query":
            service.query(RecallQueryRequest("tandkrämsrecept", context))
        else:
            service.open_source(RecallOpenSourceRequest(response.recall.results[0].item, context, response.authorization_receipt))


def _crash_worker(tmp_path):
    import os
    from contextlib import contextmanager
    child = worker(tmp_path, lambda **keys: [_item()])
    original = child._connection
    @contextmanager
    def connection():
        with original() as db:
            db.create_function("crash_now", 0, lambda: os._exit(23))
            db.execute("CREATE TEMP TRIGGER die BEFORE UPDATE ON events BEGIN SELECT crash_now(); END")
            yield db
    child._connection = connection
    child.run_once()


def test_pending_invalidation_hides_old_projection_until_refresh(tmp_path):
    w = worker(tmp_path, lambda **keys: [_item()])
    w.enqueue(event())
    w.run_once()
    assert len(w.read_items()) == 1
    w.enqueue(event("edit", ChangeKind.EDITED_MESSAGE))
    assert w.read_items() == []
    assert w.run_once()
    assert len(w.read_items()) == 1
