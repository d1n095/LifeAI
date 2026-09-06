import pytest
from sqlalchemy import select

from app.models.conversation import Conversation, Message, MessageRole, MessageStatus
from app.models.personal_recall_outbox import PersonalRecallOutbox
from app.personal_recall.index import SnapshotStoragePolicy
from app.personal_recall.snapshot_protection import DeterministicTestSnapshotProtector
from app.personal_recall.outbox import consume_outbox, outbox_status
from app.personal_recall.workers import ChangeKind, RecallIndexWorker, SourceChange
from test_integration_bridge import _item


def _worker(tmp_path, loader, owner="alice"):
    tmp_path.chmod(0o700)
    return RecallIndexWorker(tmp_path / "worker.sqlite", policy=SnapshotStoragePolicy(tmp_path), owner_id=owner,
                             protector=DeterministicTestSnapshotProtector(), authorize=lambda value: value == owner,
                             load_source=loader, test_only=True)


def test_late_lower_version_cannot_resurrect_newer_projection(tmp_path):
    item = _item()
    item.text = "v2"
    worker = _worker(tmp_path, lambda **keys: [item])
    worker.enqueue(SourceChange("v2", "alice", "item-1", ChangeKind.EDITED_MESSAGE, 2, "updated"))
    assert worker.run_once()
    item.text = "v1-stale"
    worker.enqueue(SourceChange("v1", "alice", "item-1", ChangeKind.EDITED_MESSAGE, 1, "updated"))
    assert worker.run_once()
    assert worker.read_items()[0].text == "v2"


def test_same_event_is_idempotent_after_restart_and_collision_rejected(tmp_path):
    worker = _worker(tmp_path, lambda **keys: [_item()])
    event = SourceChange("same", "alice", "item-1", ChangeKind.NEW_MESSAGE, 1, "created")
    worker.enqueue(event)
    worker.enqueue(event)
    assert worker.run_once()
    restarted = _worker(tmp_path, lambda **keys: [_item()])
    restarted.enqueue(event)
    assert not restarted.run_once()
    with pytest.raises(ValueError):
        restarted.enqueue(SourceChange("same", "alice", "item-2", ChangeKind.NEW_MESSAGE, 1, "created"))


def test_outbox_trigger_is_atomic_owner_scoped_and_versioned(superuser_db):
    from test_sqlalchemy_adapters import _seed
    alice, _bob, _memory, _revoked = _seed(superuser_db)
    conversation = superuser_db.execute(select(Conversation).where(Conversation.user_id == alice)).scalar_one()
    message = Message(conversation_id=conversation.id, role=MessageRole.user, content="outbox v1", status=MessageStatus.succeeded)
    superuser_db.add(message)
    superuser_db.flush()
    rows = superuser_db.execute(select(PersonalRecallOutbox).where(PersonalRecallOutbox.source_id == message.id).order_by(PersonalRecallOutbox.canonical_version)).scalars().all()
    assert len(rows) == 1
    assert rows[0].owner_id == alice
    assert rows[0].event_type == "created"
    assert rows[0].canonical_version == 1
    message.content = "outbox v2"
    superuser_db.flush()
    rows = superuser_db.execute(select(PersonalRecallOutbox).where(PersonalRecallOutbox.source_id == message.id).order_by(PersonalRecallOutbox.canonical_version)).scalars().all()
    assert [row.canonical_version for row in rows] == [1, 2]
    assert rows[-1].event_type == "updated"
    superuser_db.delete(message)
    superuser_db.flush()
    rows = superuser_db.execute(select(PersonalRecallOutbox).where(PersonalRecallOutbox.source_id == message.id).order_by(PersonalRecallOutbox.canonical_version)).scalars().all()
    assert rows[-1].event_type == "deleted"
    assert rows[-1].canonical_version == 2


def test_outbox_consumer_uses_only_routing_data_and_is_bounded(superuser_db, tmp_path):
    from test_sqlalchemy_adapters import _seed
    alice, _bob, _memory, _revoked = _seed(superuser_db)
    conversation = superuser_db.execute(select(Conversation).where(Conversation.user_id == alice)).scalar_one()
    message = Message(conversation_id=conversation.id, role=MessageRole.user, content="private body", status=MessageStatus.succeeded)
    superuser_db.add(message)
    superuser_db.commit()
    worker = _worker(tmp_path, lambda **keys: [_item(owner=str(alice))], owner=str(alice))
    before = outbox_status(superuser_db, owner_id=str(alice), limit=10)
    result = consume_outbox(superuser_db, owner_id=str(alice), worker=worker, limit=10)
    assert result.seen >= 1
    after = outbox_status(superuser_db, owner_id=str(alice), limit=10)
    assert after["pending_events"] < before["pending_events"]
    assert "private body" not in repr(result)


def test_outbox_rls_hides_foreign_owner_events(superuser_db):
    from app.db import SessionLocal
    from app.request_context import current_user_id
    from test_sqlalchemy_adapters import _seed

    alice, bob, _memory, _revoked = _seed(superuser_db)
    conversation = superuser_db.execute(select(Conversation).where(Conversation.user_id == bob)).scalar_one()
    superuser_db.add(Message(conversation_id=conversation.id, role=MessageRole.user, content="bob private", status=MessageStatus.succeeded))
    superuser_db.commit()
    token = current_user_id.set(str(alice))
    db = SessionLocal()
    try:
        rows = db.execute(select(PersonalRecallOutbox)).scalars().all()
        assert rows
        assert all(row.owner_id == alice for row in rows)
    finally:
        db.close()
        current_user_id.reset(token)
