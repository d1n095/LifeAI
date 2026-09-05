from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.config import get_settings
from app.models.conversation import Conversation, Message, MessageRole
from app.models.document import ActiveTruthStatus, DeletionStatus, Document, DocumentSource, IndexStatus, KnowledgeClassification
from app.models.document_chunk import DocumentChunk
from app.models.founder_memory import FounderMemoryNote
from app.models.knowledge_version import KnowledgeVersion
from app.models.memory_source_unit import SnapshotStatus
from app.models.revoked_access_token import RevokedAccessToken
from app.models.source_relationship import RelationshipType, SourceRelationship
from app.models.user import User
from app.personal_recall.retrieval import PersonalRecallEngine
from app.personal_recall.locator import validate_open_locator
from app.personal_recall.serialization import RecallSerializationError, serialize_for_local_client
from app.personal_recall.index import LocalRecallIndex
from app.personal_recall.snapshot_sync import synchronize_authoritative_sources
from app.personal_recall.sqlalchemy_adapters import AdapterAuthorizationError, ConversationMessageAdapter, DocumentChunkAdapter, DurableMemoryAdapter, SQLAlchemySourceRegistry, canonical_personal_adapters
from app.personal_recall.types import SourceType
from app.personal_recall.authorization import DisclosureLevel, IdentifierScope, RecallAuthorizationContext, SQLAlchemyRecallAuthorityResolver
from app.rag.memory_source import DocumentSourceLocator, get_or_create_memory_source_unit
from app.request_context import current_user_id
from app.security import utcnow_seconds_baseline


def _user(email: str) -> User:
    now = utcnow_seconds_baseline()
    return User(email=email, password_hash="not-used", email_verified=True, email_verified_at=now, sessions_valid_after=now)


def _document(owner_id, title: str, *, deleted=False, truth=ActiveTruthStatus.active) -> Document:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return Document(uploaded_by=owner_id, title=title, source=DocumentSource.upload, content_preview=f"{title}: nano HAp och fluorid", status=IndexStatus.indexed, chunk_count=1, checksum=uuid.uuid4().hex * 2, media_type="application/pdf", original_filename=f"{title}.pdf", classification=KnowledgeClassification.decisions, active_truth_status=truth, version_number=1, imported_at=now, deleted_at=now if deleted else None, deletion_status=DeletionStatus.pending if deleted else DeletionStatus.none)


def _seed(superuser_db):
    alice, bob = _user("recall-alice@example.com"), _user("recall-bob@example.com")
    superuser_db.add_all([alice, bob])
    superuser_db.flush()
    alice_conversation = Conversation(user_id=alice.id, title="Tandkrämsrecept")
    bob_conversation = Conversation(user_id=bob.id, title="Tandkrämsrecept")
    superuser_db.add_all([alice_conversation, bob_conversation])
    superuser_db.flush()
    superuser_db.add_all([
        Message(conversation_id=alice_conversation.id, role=MessageRole.user, content="Mitt tandkrämsrecept använder nano HAp"),
        Message(conversation_id=alice_conversation.id, role=MessageRole.assistant, content="Ett tidigare, overifierat svar om tandkrämsreceptet"),
        Message(conversation_id=bob_conversation.id, role=MessageRole.user, content="BOBS HEMLIGA tandkrämsrecept"),
    ])
    live = _document(alice.id, "Tandkrämsrecept V2")
    old = _document(alice.id, "Tandkrämsrecept V1", truth=ActiveTruthStatus.superseded)
    deleted = _document(alice.id, "Raderat tandkrämsrecept", deleted=True)
    bob_doc = _document(bob.id, "Bobs hemliga tandkrämsrecept")
    superuser_db.add_all([live, old, deleted, bob_doc])
    superuser_db.flush()
    dim = get_settings().embedding_dim
    chunks = [
        DocumentChunk(document_id=live.id, owner_id=alice.id, chunk_index=0, text="Aktuellt tandkrämsrecept med nano HAp", embedding=[0.0] * dim),
        DocumentChunk(document_id=old.id, owner_id=alice.id, chunk_index=0, text="Gammalt tandkrämsrecept", embedding=[0.0] * dim),
        DocumentChunk(document_id=deleted.id, owner_id=alice.id, chunk_index=0, text="RADERAD HEMLIGHET tandkrämsrecept", embedding=[0.0] * dim),
        DocumentChunk(document_id=bob_doc.id, owner_id=bob.id, chunk_index=0, text="BOBS HEMLIGA dokument", embedding=[0.0] * dim),
    ]
    superuser_db.add_all(chunks)
    superuser_db.flush()
    old_version = KnowledgeVersion(source_id=old.id, owner_id=alice.id, version_number=1, checksum="1" * 64, extraction_version="v1", raw_metadata={"content_text": "Gammalt tandkrämsrecept"})
    live_v1 = KnowledgeVersion(source_id=live.id, owner_id=alice.id, version_number=1, checksum="2" * 64, extraction_version="v1", raw_metadata={"content_text": "Första tandkrämsreceptet"})
    live_v2 = KnowledgeVersion(source_id=live.id, owner_id=alice.id, version_number=2, checksum="3" * 64, extraction_version="v1", raw_metadata={"content_text": "Aktuellt tandkrämsrecept"})
    bob_version = KnowledgeVersion(source_id=bob_doc.id, owner_id=bob.id, version_number=1, checksum="4" * 64, extraction_version="v1", raw_metadata={"content_text": "BOBS HEMLIGA version"})
    superuser_db.add_all([old_version, live_v1, live_v2, bob_version])
    superuser_db.flush()
    superuser_db.add(SourceRelationship(owner_id=alice.id, from_source_id=live.id, to_source_id=old.id, relationship_type=RelationshipType.supersedes, note="V2 ersätter V1"))
    memory_id = get_or_create_memory_source_unit(superuser_db, DocumentSourceLocator(owner_id=alice.id, document_id=live.id, version_id=None, chunk_id=chunks[0].id, observed_at=datetime.now(timezone.utc), content_text=chunks[0].text, snapshot_status=SnapshotStatus.exact))
    revoked_id = get_or_create_memory_source_unit(superuser_db, DocumentSourceLocator(owner_id=alice.id, document_id=deleted.id, version_id=None, chunk_id=chunks[2].id, observed_at=datetime.now(timezone.utc), content_text=chunks[2].text, snapshot_status=SnapshotStatus.exact))
    get_or_create_memory_source_unit(superuser_db, DocumentSourceLocator(owner_id=bob.id, document_id=bob_doc.id, version_id=None, chunk_id=chunks[3].id, observed_at=datetime.now(timezone.utc), content_text=chunks[3].text, snapshot_status=SnapshotStatus.exact))
    superuser_db.add_all([
        FounderMemoryNote(owner_id=alice.id, note_type="decision", content="Beslut: nano HAp i tandkrämsrecept", status="active", authority="founder", basis="manual", idempotency_key="recall-real-adapter-decision", provenance={"source": "test"}),
        FounderMemoryNote(owner_id=bob.id, note_type="decision", content="BOBS HEMLIGA beslut", status="active", authority="founder", basis="manual", idempotency_key="recall-real-adapter-bob-decision", provenance={"source": "test"}),
    ])
    superuser_db.commit()
    superuser_db.execute(text("SELECT transition_memory_source_admin(:id, 'revoked', 'adapter tombstone proof', 'admin', NULL)"), {"id": revoked_id})
    superuser_db.commit()
    return alice.id, bob.id, memory_id, revoked_id


def _restricted_session(owner_id):
    from app.db import SessionLocal

    token = current_user_id.set(str(owner_id))
    return SessionLocal(), token


def _recall_authority(owner_id, *, jti="recall-real-session"):
    now = datetime.now(timezone.utc)
    return RecallAuthorizationContext(
        authorization_id="real-grant", authorization_version="v1", owner_id=str(owner_id), session_user_id=str(owner_id),
        session_jti=jti, session_issued_at=now, expires_at=now + timedelta(hours=1),
        allowed_source_types=frozenset(SourceType), project_scope=IdentifierScope(unrestricted=True), conversation_scope=IdentifierScope(unrestricted=True),
        allow_current=True, allow_historical=True, disclosure_level=DisclosureLevel.SNIPPET, allow_locator_open=True,
    )


def test_real_seeded_all_about_toothpaste_across_canonical_sources(superuser_db):
    alice_id, _bob_id, _memory_id, _revoked_id = _seed(superuser_db)
    db, token = _restricted_session(alice_id)
    try:
        adapters = canonical_personal_adapters(db, owner_id=alice_id)
        response = PersonalRecallEngine(adapters, expected_source_types={SourceType.CONVERSATION, SourceType.PREVIOUS_ANSWER, SourceType.FILE, SourceType.DURABLE_MEMORY, SourceType.DECISION_RECORD}).recall(owner_id=str(alice_id), raw_query="allt om tandkrämsrecept", now=datetime.now(timezone.utc))
        source_types = {result.item.source_type for result in response.results}
        assert {SourceType.CONVERSATION, SourceType.PREVIOUS_ANSWER, SourceType.FILE, SourceType.DURABLE_MEMORY, SourceType.DECISION_RECORD} <= source_types
        assert all("BOBS HEMLIGA" not in (result.item.text or "") for result in response.results)
        assert all("RADERAD HEMLIGHET" not in (result.item.text or "") for result in response.results)
        assert all("REVOKED" not in (result.item.text or "") for result in response.results)
        registry = SQLAlchemySourceRegistry(db, authorized_owner_id=alice_id)
        assert all(validate_open_locator(result.item, owner_id=str(alice_id), registry=registry).locator for result in response.results)
        projected = serialize_for_local_client(response, owner_id=str(alice_id))
        assert projected["owner_id"] == str(alice_id)
        assert all("text" not in row for row in projected["results"])
    finally:
        db.close()
        current_user_id.reset(token)


def test_rls_independently_blocks_cross_owner_even_when_adapter_is_bound_to_victim(superuser_db):
    alice_id, bob_id, _memory_id, _revoked_id = _seed(superuser_db)
    db, token = _restricted_session(alice_id)
    try:
        # Deliberately construct victim-authorized adapters on an attacker-RLS session. The
        # explicit query asks for Bob; PostgreSQL RLS must still return nothing.
        for adapter in canonical_personal_adapters(db, owner_id=bob_id):
            assert list(adapter.discover(owner_id=str(bob_id))) == [], adapter.name
        assert db.execute(select(Message).join(Conversation).where(Conversation.user_id == bob_id)).scalars().all() == []
    finally:
        db.close()
        current_user_id.reset(token)


def test_sql_authority_resolver_uses_canonical_session_epoch_and_jti_revocation(superuser_db):
    alice_id, _bob_id, _memory_id, _revoked_id = _seed(superuser_db)
    context = _recall_authority(alice_id)
    db, token = _restricted_session(alice_id)
    try:
        resolver = SQLAlchemyRecallAuthorityResolver(db, authenticated_user_id=alice_id, grant_loader=lambda presented: presented)
        assert resolver.resolve(context, now=datetime.now(timezone.utc)) == context
        superuser_db.add(RevokedAccessToken(jti=context.session_jti, expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)))
        superuser_db.commit()
        assert resolver.resolve(context, now=datetime.now(timezone.utc)) is None
    finally:
        db.close()
        current_user_id.reset(token)


def test_sql_authority_resolver_cannot_validate_foreign_owner_through_rls(superuser_db):
    alice_id, bob_id, _memory_id, _revoked_id = _seed(superuser_db)
    foreign = _recall_authority(bob_id, jti="foreign-session")
    db, token = _restricted_session(alice_id)
    try:
        resolver = SQLAlchemyRecallAuthorityResolver(db, authenticated_user_id=alice_id, grant_loader=lambda presented: presented)
        assert resolver.resolve(foreign, now=datetime.now(timezone.utc)) is None
    finally:
        db.close()
        current_user_id.reset(token)


def test_adapter_authority_rejects_owner_parameter_swap_before_query(superuser_db):
    alice_id, bob_id, _memory_id, _revoked_id = _seed(superuser_db)
    adapter = ConversationMessageAdapter(superuser_db, authorized_owner_id=alice_id)
    try:
        list(adapter.discover(owner_id=str(bob_id)))
    except AdapterAuthorizationError:
        pass
    else:
        raise AssertionError("owner parameter swap was accepted")


def test_soft_delete_revocation_and_version_supersession_propagate(superuser_db):
    alice_id, _bob_id, memory_id, revoked_id = _seed(superuser_db)
    db, token = _restricted_session(alice_id)
    try:
        document_items = list(DocumentChunkAdapter(db, authorized_owner_id=alice_id).discover(owner_id=str(alice_id)))
        memory_items = list(DurableMemoryAdapter(db, authorized_owner_id=alice_id).discover(owner_id=str(alice_id)))
        assert all("Raderat" not in (item.subject or "") for item in document_items)
        assert {item.source_id for item in memory_items} == {str(memory_id)}
        assert str(revoked_id) not in {item.source_id for item in memory_items}
        response = PersonalRecallEngine(canonical_personal_adapters(db, owner_id=alice_id)).recall(owner_id=str(alice_id), raw_query="senaste tandkrämsrecept", now=datetime.now(timezone.utc))
        version_items = [result.item for result in response.results if result.item.item_id.startswith("knowledge_version:")]
        assert len([item for item in version_items if item.superseded_by is None]) >= 1
        assert all(item.item_id != item.superseded_by for item in version_items)
    finally:
        db.close()
        current_user_id.reset(token)


def test_serialization_integration_rejects_owner_tampering(superuser_db):
    alice_id, bob_id, _memory_id, _revoked_id = _seed(superuser_db)
    db, token = _restricted_session(alice_id)
    try:
        response = PersonalRecallEngine([ConversationMessageAdapter(db, authorized_owner_id=alice_id)]).recall(owner_id=str(alice_id), raw_query="tandkrämsrecept", now=datetime.now(timezone.utc))
        response.results[0].item.owner_id = str(bob_id)
        try:
            serialize_for_local_client(response, owner_id=str(alice_id))
        except RecallSerializationError:
            pass
        else:
            raise AssertionError("tampered result serialized")
    finally:
        db.close()
        current_user_id.reset(token)


def test_real_soft_delete_tombstones_existing_snapshot_after_complete_refresh(superuser_db):
    alice_id, _bob_id, _memory_id, _revoked_id = _seed(superuser_db)
    db, token = _restricted_session(alice_id)
    try:
        adapter = DocumentChunkAdapter(db, authorized_owner_id=alice_id)
        original = list(adapter.discover(owner_id=str(alice_id)))
        live_record = next(item for item in original if item.item_id.startswith("document:") and "V2" in (item.subject or ""))
        index = LocalRecallIndex(owner_id=str(alice_id))
        index.replace(original)
    finally:
        db.close()
        current_user_id.reset(token)
    document_id = uuid.UUID(live_record.source_id)
    document = superuser_db.execute(select(Document).where(Document.id == document_id)).scalar_one()
    document.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    document.deletion_status = DeletionStatus.pending
    superuser_db.commit()
    db, token = _restricted_session(alice_id)
    try:
        result = synchronize_authoritative_sources(index, [DocumentChunkAdapter(db, authorized_owner_id=alice_id)], owner_id=str(alice_id), authoritative_source_types=[SourceType.FILE])
        assert live_record.item_id in result.tombstoned
        assert index.items[live_record.item_id].index_state.value == "deleted"
        assert index.items[live_record.item_id].text is None
    finally:
        db.close()
        current_user_id.reset(token)


def test_incremental_canonical_worker_edit_delete_and_recovery(superuser_db, tmp_path):
    from app.personal_recall.sqlalchemy_adapters import CanonicalSourceLoader
    from app.personal_recall.workers import RecallIndexWorker, SourceChange, ChangeKind
    from app.personal_recall.index import SnapshotStoragePolicy
    from app.personal_recall.snapshot_protection import DeterministicTestSnapshotProtector
    alice, bob, memory_id, _ = _seed(superuser_db)
    message = superuser_db.execute(select(Message).join(Conversation).where(Conversation.user_id == alice, Message.role == MessageRole.user)).scalars().first()
    source_id = str(message.id)
    tmp_path.chmod(0o700)
    db, token = _restricted_session(alice)
    try:
        def make_worker():
            return RecallIndexWorker(tmp_path / "index.sqlite", policy=SnapshotStoragePolicy(tmp_path), owner_id=str(alice),
                                     protector=DeterministicTestSnapshotProtector(), authorize=lambda owner: owner == str(alice),
                                     load_source=CanonicalSourceLoader(db, authorized_owner_id=alice), test_only=True)
        worker = make_worker()
        worker.enqueue(SourceChange("new", str(alice), source_id, ChangeKind.NEW_MESSAGE))
        assert worker.run_once()
        assert len(worker.read_items()) == 1
        message.content = "REVISED CANONICAL CONTENT"
        superuser_db.commit()
        worker.enqueue(SourceChange("edit", str(alice), source_id, ChangeKind.EDITED_MESSAGE))
        assert make_worker().run_once()
        assert worker.read_items()[0].text == "REVISED CANONICAL CONTENT"
        # A delayed create event must re-read canonical deletion, never resurrect old text.
        superuser_db.delete(message)
        superuser_db.commit()
        worker.enqueue(SourceChange("delayed", str(alice), source_id, ChangeKind.NEW_MESSAGE))
        assert make_worker().run_once()
        assert worker.read_items() == []
        loader = CanonicalSourceLoader(db, authorized_owner_id=alice)
        with __import__("pytest").raises(AdapterAuthorizationError):
            loader(owner_id=str(bob), family="message", source_id=source_id)
    finally:
        db.close()
        current_user_id.reset(token)


def test_real_postgres_fts_candidates_owner_scope_and_revocation(superuser_db):
    from dataclasses import replace
    from app.personal_recall.candidates import document_candidates, document_candidate_statement
    from app.personal_recall.authorization import RecallAuthorizationError
    alice, bob, _, _ = _seed(superuser_db)
    context = replace(_recall_authority(alice), project_scope=IdentifierScope(allow_unscoped=True))
    class Resolver:
        calls = 0
        revoke = False
        def resolve(self, presented, *, now):
            self.calls += 1
            return None if self.revoke and self.calls % 2 == 0 else presented
    resolver = Resolver()
    db, token = _restricted_session(alice)
    try:
        candidates = document_candidates(db, authorization=context, resolver=resolver, query="tandkrämsrecept")
        assert candidates
        for chunk_id, doc_id in candidates:
            row = superuser_db.get(DocumentChunk, uuid.UUID(chunk_id))
            assert row.owner_id == alice
            assert superuser_db.get(Document, uuid.UUID(doc_id)).deleted_at is None
        victim = replace(context, owner_id=str(bob), session_user_id=str(bob))
        assert db.execute(document_candidate_statement(victim, "tandkrämsrecept")).all() == []
        empty_scope = replace(context, project_scope=IdentifierScope())
        assert document_candidates(db, authorization=empty_scope, resolver=resolver, query="tandkrämsrecept") == ()
        assert document_candidates(db, authorization=context, resolver=resolver, query="'; DROP TABLE users; --") == ()
        resolver.calls, resolver.revoke = 0, True
        with __import__("pytest").raises(RecallAuthorizationError):
            document_candidates(db, authorization=context, resolver=resolver, query="tandkrämsrecept")
    finally:
        db.close()
        current_user_id.reset(token)


def test_canonical_worker_document_versions_delete_and_memory_lifecycle(superuser_db, tmp_path):
    from app.personal_recall.sqlalchemy_adapters import CanonicalSourceLoader
    from app.personal_recall.workers import RecallIndexWorker, SourceChange, ChangeKind
    from app.personal_recall.index import SnapshotStoragePolicy
    from app.personal_recall.snapshot_protection import DeterministicTestSnapshotProtector
    alice, _, memory_id, _ = _seed(superuser_db)
    document = _document(alice, "Independent source")
    superuser_db.add(document)
    superuser_db.flush()
    v1 = KnowledgeVersion(source_id=document.id, owner_id=alice, version_number=1, checksum="a" * 64, extraction_version="v1", raw_metadata={"content_text": "first"})
    superuser_db.add(v1)
    superuser_db.commit()
    tmp_path.chmod(0o700)
    db, token = _restricted_session(alice)
    try:
        w = RecallIndexWorker(tmp_path / "index.sqlite", policy=SnapshotStoragePolicy(tmp_path), owner_id=str(alice),
                              protector=DeterministicTestSnapshotProtector(), authorize=lambda owner: owner == str(alice),
                              load_source=CanonicalSourceLoader(db, authorized_owner_id=alice), test_only=True)
        def process(name, source, kind):
            w.enqueue(SourceChange(name, str(alice), str(source), kind))
            assert w.run_once()
        process("new-doc", document.id, ChangeKind.NEW_DOCUMENT)
        assert len(w.read_items()) == 2
        v2 = KnowledgeVersion(source_id=document.id, owner_id=alice, version_number=2, checksum="b" * 64, extraction_version="v1", raw_metadata={"content_text": "second"})
        superuser_db.add(v2)
        superuser_db.commit()
        process("version", document.id, ChangeKind.KNOWLEDGE_SUPERSESSION)
        versions = {item.item_id: item for item in w.read_items()}
        assert versions[f"knowledge_version:{v1.id}"].superseded_by == f"knowledge_version:{v2.id}"
        document.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
        document.deletion_status = DeletionStatus.pending
        superuser_db.commit()
        process("delete", document.id, ChangeKind.DELETED_DOCUMENT)
        assert w.read_items() == []
        # Events are invalidation hints: load active canonical memory before revoking it.
        process("memory-refresh", memory_id, ChangeKind.MEMORY_REVOKE)
        assert len(w.read_items()) == 1
        superuser_db.execute(text("SELECT transition_memory_source_admin(:id, 'revoked', 'worker proof', 'admin', NULL)"), {"id": memory_id})
        superuser_db.commit()
        process("memory-revoke", memory_id, ChangeKind.MEMORY_REVOKE)
        assert w.read_items() == []
        superuser_db.execute(text("SELECT transition_memory_source_admin(:id, 'purged', 'worker proof', 'admin', NULL)"), {"id": memory_id})
        superuser_db.commit()
        process("memory-purge", memory_id, ChangeKind.MEMORY_PURGE)
        assert w.read_items() == []
    finally:
        db.close()
        current_user_id.reset(token)


def test_cached_user_cannot_hide_session_epoch_revocation(superuser_db):
    from sqlalchemy import update
    alice, _, _, _ = _seed(superuser_db)
    context = _recall_authority(alice)
    db, token = _restricted_session(alice)
    try:
        cached_user = db.get(User, alice)
        resolver = SQLAlchemyRecallAuthorityResolver(db, authenticated_user_id=alice, grant_loader=lambda presented: presented)
        assert resolver.resolve(context, now=datetime.now(timezone.utc)) == context
        epoch = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=10)
        superuser_db.execute(update(User).where(User.id == alice).values(sessions_valid_after=epoch))
        superuser_db.commit()
        assert resolver.resolve(context, now=datetime.now(timezone.utc)) is None
        assert cached_user.sessions_valid_after == epoch
    finally:
        db.close()
        current_user_id.reset(token)
