"""Read-only adapters over LifeAI's canonical SQLAlchemy models.

Every query carries an explicit owner predicate even though production sessions also enforce
RLS. The constructor-bound authorized owner prevents a caller from turning an adapter backed
by a privileged/test session into a cross-owner read primitive.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.conversation import Conversation, Message, MessageRole, MessageStatus
from app.models.document import ActiveTruthStatus, Document, IndexStatus
from app.models.document_chunk import DocumentChunk
from app.models.founder_memory import FounderMemoryNote
from app.models.knowledge_version import KnowledgeVersion
from app.models.memory_source_unit import LifecycleStatus, MemorySourceUnit, SourceRole
from app.models.problem_learning import LifeProblem, LifeProblemDecision
from app.models.project_entities import ProjectEntity
from app.models.source_relationship import SourceRelationship
from app.personal_recall.types import DecisionState, IndexState, PersonalKnowledgeItem, Provenance, SourceAuthority, SourceType, VerificationState
from app.personal_recall.locator import SourceRegistryRecord


class AdapterAuthorizationError(PermissionError):
    pass


class _OwnerBoundAdapter:
    name = "canonical"
    source_types: frozenset[SourceType] = frozenset()

    def __init__(self, db: Session, *, authorized_owner_id: uuid.UUID | str, limit: int = 10_000):
        if limit < 1 or limit > 10_000:
            raise ValueError("adapter limit must be between 1 and 10000")
        self.db = db
        self.owner_id = uuid.UUID(str(authorized_owner_id))
        self.limit = limit

    def _owner(self, requested: str) -> uuid.UUID:
        owner = uuid.UUID(str(requested))
        if owner != self.owner_id:
            raise AdapterAuthorizationError("requested owner is outside adapter authority")
        return owner


class ConversationMessageAdapter(_OwnerBoundAdapter):
    name = "canonical_conversations"
    source_types = frozenset({SourceType.CONVERSATION, SourceType.PREVIOUS_ANSWER})

    def discover(self, *, owner_id: str) -> Iterable[PersonalKnowledgeItem]:
        owner = self._owner(owner_id)
        rows = self.db.execute(
            select(Message, Conversation)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Conversation.user_id == owner, Message.status == MessageStatus.succeeded)
            .order_by(Message.created_at, Message.id)
            .limit(self.limit)
        ).all()
        for message, conversation in rows:
            is_user = message.role == MessageRole.user
            source_type = SourceType.CONVERSATION if is_user else SourceType.PREVIOUS_ANSWER
            yield PersonalKnowledgeItem(
                item_id=f"message:{message.id}", source_type=source_type, source_id=str(message.id), owner_id=str(owner),
                conversation_id=str(conversation.id), created_at=message.created_at, updated_at=conversation.updated_at,
                subject=conversation.title, text=message.content,
                content_reference=f"conversation:{conversation.id}:message:{message.id}",
                provenance=Provenance(source_type, str(message.id), f"conversation://{conversation.id}/{message.id}", conversation_id=str(conversation.id), occurred_at=message.created_at),
                decision_state=DecisionState.MENTION if is_user else DecisionState.CLAIM,
                verification_state=VerificationState.UNVERIFIED,
                source_authority=SourceAuthority.USER if is_user else SourceAuthority.ASSISTANT,
                index_state=IndexState.INDEXED,
                metadata={"role": _value(message.role), "sequence_number": message.sequence_number},
            )


class DocumentChunkAdapter(_OwnerBoundAdapter):
    name = "canonical_documents"
    source_types = frozenset({SourceType.FILE})

    def discover(self, *, owner_id: str) -> Iterable[PersonalKnowledgeItem]:
        owner = self._owner(owner_id)
        documents = self.db.execute(
            select(Document)
            .where(Document.uploaded_by == owner, Document.deleted_at.is_(None))
            .order_by(Document.created_at, Document.id)
            .limit(self.limit)
        ).scalars().all()
        remaining = self.limit
        for document in documents:
            if remaining <= 0:
                return
            remaining -= 1
            yield _document_item(document, owner)
        if remaining <= 0:
            return
        chunks = self.db.execute(
            select(DocumentChunk, Document)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(DocumentChunk.owner_id == owner, Document.uploaded_by == owner, Document.deleted_at.is_(None))
            .order_by(Document.created_at, DocumentChunk.chunk_index, DocumentChunk.id)
            .limit(remaining)
        ).all()
        for chunk, document in chunks:
            yield PersonalKnowledgeItem(
                item_id=f"document_chunk:{chunk.id}", source_type=SourceType.FILE, source_id=str(document.id), owner_id=str(owner),
                project_id=_str(document.project_id), file_id=str(document.id), created_at=chunk.created_at, updated_at=document.updated_at,
                subject=document.title, topic=document.category, text=chunk.text,
                content_reference=f"document:{document.id}:chunk:{chunk.id}",
                provenance=Provenance(SourceType.FILE, str(document.id), f"document://{document.id}/{chunk.id}", file_id=str(document.id), occurred_at=chunk.created_at),
                decision_state=_document_decision(document.active_truth_status), verification_state=_document_verification(document.active_truth_status),
                source_authority=SourceAuthority.PRIMARY, index_state=_document_index(document.status), content_hash=None,
                metadata={"chunk_index": chunk.chunk_index, "filename": document.original_filename, "media_type": document.media_type},
            )


class DurableMemoryAdapter(_OwnerBoundAdapter):
    name = "canonical_durable_memory"
    source_types = frozenset({SourceType.DURABLE_MEMORY})

    def discover(self, *, owner_id: str) -> Iterable[PersonalKnowledgeItem]:
        owner = self._owner(owner_id)
        rows = self.db.execute(
            select(MemorySourceUnit)
            .where(MemorySourceUnit.owner_id == owner, MemorySourceUnit.lifecycle_status == LifecycleStatus.active)
            .order_by(MemorySourceUnit.occurred_at, MemorySourceUnit.created_at, MemorySourceUnit.id)
            .limit(self.limit)
        ).scalars().all()
        for row in rows:
            authority = {SourceRole.founder: SourceAuthority.USER, SourceRole.assistant: SourceAuthority.ASSISTANT, SourceRole.external: SourceAuthority.PRIMARY}.get(row.source_role, SourceAuthority.UNKNOWN)
            yield PersonalKnowledgeItem(
                item_id=f"memory_source:{row.id}", source_type=SourceType.DURABLE_MEMORY, source_id=str(row.id), owner_id=str(owner),
                project_id=_str(row.project_id), created_at=row.created_at, subject=row.source_identity_key, text=row.content_text,
                content_reference=f"memory_source:{row.id}", provenance=Provenance(SourceType.DURABLE_MEMORY, str(row.id), f"memory://{row.id}", occurred_at=row.occurred_at),
                decision_state=DecisionState.MENTION, verification_state=VerificationState.UNVERIFIED,
                source_authority=authority, index_state=IndexState.INDEXED, content_hash=row.content_hash,
                metadata={"source_kind": _value(row.source_kind), "snapshot_status": _value(row.snapshot_status)},
            )


class KnowledgeVersionAdapter(_OwnerBoundAdapter):
    name = "canonical_knowledge_versions"
    source_types = frozenset({SourceType.FILE})

    def discover(self, *, owner_id: str) -> Iterable[PersonalKnowledgeItem]:
        owner = self._owner(owner_id)
        rows = self.db.execute(
            select(KnowledgeVersion, Document)
            .join(Document, Document.id == KnowledgeVersion.source_id)
            .where(KnowledgeVersion.owner_id == owner, Document.uploaded_by == owner, Document.deleted_at.is_(None))
            .order_by(KnowledgeVersion.source_id, KnowledgeVersion.version_number, KnowledgeVersion.id)
            .limit(self.limit)
        ).all()
        relationships = self.db.execute(
            select(SourceRelationship)
            .where(SourceRelationship.owner_id == owner)
            .order_by(SourceRelationship.created_at, SourceRelationship.id)
            .limit(self.limit)
        ).scalars().all()
        versions_by_source: dict[str, list[KnowledgeVersion]] = {}
        for version, _document in rows:
            versions_by_source.setdefault(str(version.source_id), []).append(version)
        latest_by_source = {source_id: f"knowledge_version:{versions[-1].id}" for source_id, versions in versions_by_source.items()}
        edges: dict[str, dict[str, list[str]]] = {}
        for relationship in relationships:
            target = latest_by_source.get(str(relationship.to_source_id))
            if target:
                edges.setdefault(str(relationship.from_source_id), {}).setdefault(_value(relationship.relationship_type), []).append(target)
        for version, document in rows:
            raw = version.raw_metadata or {}
            text = raw.get("content_text") if isinstance(raw.get("content_text"), str) else document.content_preview
            chain = versions_by_source[str(version.source_id)]
            position = chain.index(version)
            replacement = f"knowledge_version:{chain[position + 1].id}" if position + 1 < len(chain) else None
            yield PersonalKnowledgeItem(
                item_id=f"knowledge_version:{version.id}", source_type=SourceType.FILE, source_id=str(document.id), owner_id=str(owner),
                project_id=_str(document.project_id), file_id=str(document.id), created_at=version.created_at, subject=document.title, text=text,
                source_version=str(version.version_number), content_hash=version.checksum,
                content_reference=f"document:{document.id}:version:{version.id}",
                provenance=Provenance(SourceType.FILE, str(document.id), f"document://{document.id}/version/{version.id}", file_id=str(document.id), version=str(version.version_number), occurred_at=version.created_at),
                decision_state=_document_decision(document.active_truth_status), verification_state=_document_verification(document.active_truth_status), source_authority=SourceAuthority.PRIMARY,
                superseded_by=replacement, index_state=_document_index(document.status), relationship_edges={key: tuple(values) for key, values in edges.get(str(document.id), {}).items()},
                metadata={"extraction_version": version.extraction_version},
            )


class DecisionRecordAdapter(_OwnerBoundAdapter):
    name = "canonical_decisions"
    source_types = frozenset({SourceType.DECISION_RECORD})

    def discover(self, *, owner_id: str) -> Iterable[PersonalKnowledgeItem]:
        owner = self._owner(owner_id)
        notes = self.db.execute(select(FounderMemoryNote).where(FounderMemoryNote.owner_id == owner, FounderMemoryNote.note_type.in_(("decision", "correction"))).order_by(FounderMemoryNote.created_at, FounderMemoryNote.id).limit(self.limit)).scalars().all()
        note_replacement = {note.supersedes_note_id: note.id for note in notes if note.supersedes_note_id}
        yielded = 0
        for note in notes:
            yielded += 1
            yield _decision_item(f"founder_memory:{note.id}", str(note.id), owner, note.content, note.created_at, note.status, note.authority, note_replacement.get(note.id), note.supersedes_note_id, note.provenance)
        if yielded >= self.limit:
            return
        entities = self.db.execute(select(ProjectEntity).where(ProjectEntity.owner_id == owner, ProjectEntity.entity_type == "decision").order_by(ProjectEntity.created_at, ProjectEntity.id).limit(self.limit - yielded)).scalars().all()
        entity_replacement = {entity.supersedes_entity_id: entity.id for entity in entities if entity.supersedes_entity_id}
        for entity in entities:
            yielded += 1
            yield _decision_item(f"project_entity:{entity.id}", str(entity.id), owner, entity.summary or entity.title, entity.decided_at or entity.created_at, entity.status, entity.authority, entity_replacement.get(entity.id), entity.supersedes_entity_id, entity.provenance, subject=entity.title)
        if yielded >= self.limit:
            return
        rows = self.db.execute(select(LifeProblemDecision, LifeProblem).join(LifeProblem, LifeProblem.id == LifeProblemDecision.problem_id).where(LifeProblemDecision.owner_id == owner, LifeProblem.owner_id == owner).order_by(LifeProblemDecision.decided_at, LifeProblemDecision.id).limit(self.limit - yielded)).all()
        problem_replacement = {decision.supersedes_decision_id: decision.id for decision, _problem in rows if decision.supersedes_decision_id}
        for decision, problem in rows:
            yield _decision_item(f"life_problem_decision:{decision.id}", str(decision.id), owner, decision.decision, decision.decided_at, decision.status, decision.authority, problem_replacement.get(decision.id), decision.supersedes_decision_id, decision.provenance, subject=problem.title, project_id=problem.project_id)


class SQLAlchemySourceRegistry(_OwnerBoundAdapter):
    """Canonical existence/ownership check used immediately before opening a source."""

    name = "canonical_source_registry"

    def resolve(self, *, source_id: str, owner_id: str, source_type: str, locator: str) -> SourceRegistryRecord | None:
        owner = self._owner(owner_id)
        try:
            source_uuid = uuid.UUID(source_id)
        except ValueError:
            return None
        if source_type in (SourceType.CONVERSATION.value, SourceType.PREVIOUS_ANSWER.value):
            row = self.db.execute(select(Message, Conversation).join(Conversation, Conversation.id == Message.conversation_id).where(Message.id == source_uuid, Conversation.user_id == owner, Message.status == MessageStatus.succeeded)).one_or_none()
            if row is None:
                return None
            message, conversation = row
            expected_type = SourceType.CONVERSATION.value if message.role == MessageRole.user else SourceType.PREVIOUS_ANSWER.value
            expected = f"conversation://{conversation.id}/{message.id}"
        elif source_type == SourceType.FILE.value:
            document = self.db.execute(select(Document).where(Document.id == source_uuid, Document.uploaded_by == owner, Document.deleted_at.is_(None))).scalar_one_or_none()
            if document is None:
                return None
            expected_type = SourceType.FILE.value
            expected = locator
            if locator == f"document://{document.id}":
                pass
            elif "/version/" in locator:
                version_id = _locator_uuid(locator)
                if version_id is None or self.db.execute(select(KnowledgeVersion.id).where(KnowledgeVersion.id == version_id, KnowledgeVersion.source_id == document.id, KnowledgeVersion.owner_id == owner)).scalar_one_or_none() is None:
                    return None
            else:
                chunk_id = _locator_uuid(locator)
                if chunk_id is None or self.db.execute(select(DocumentChunk.id).where(DocumentChunk.id == chunk_id, DocumentChunk.document_id == document.id, DocumentChunk.owner_id == owner)).scalar_one_or_none() is None:
                    return None
        elif source_type == SourceType.DURABLE_MEMORY.value:
            row = self.db.execute(select(MemorySourceUnit).where(MemorySourceUnit.id == source_uuid, MemorySourceUnit.owner_id == owner, MemorySourceUnit.lifecycle_status == LifecycleStatus.active)).scalar_one_or_none()
            if row is None:
                return None
            expected_type, expected = SourceType.DURABLE_MEMORY.value, f"memory://{row.id}"
        elif source_type == SourceType.DECISION_RECORD.value:
            exists = any((
                self.db.execute(select(FounderMemoryNote.id).where(FounderMemoryNote.id == source_uuid, FounderMemoryNote.owner_id == owner)).scalar_one_or_none(),
                self.db.execute(select(ProjectEntity.id).where(ProjectEntity.id == source_uuid, ProjectEntity.owner_id == owner, ProjectEntity.entity_type == "decision")).scalar_one_or_none(),
                self.db.execute(select(LifeProblemDecision.id).where(LifeProblemDecision.id == source_uuid, LifeProblemDecision.owner_id == owner)).scalar_one_or_none(),
            ))
            if not exists:
                return None
            expected_type, expected = SourceType.DECISION_RECORD.value, f"memory://{source_uuid}"
        else:
            return None
        if expected_type != source_type or expected != locator:
            return None
        return SourceRegistryRecord(source_id, str(owner), source_type, locator, True)


def canonical_personal_adapters(db: Session, *, owner_id: uuid.UUID | str, limit_per_adapter: int = 10_000) -> list[_OwnerBoundAdapter]:
    return [ConversationMessageAdapter(db, authorized_owner_id=owner_id, limit=limit_per_adapter), DocumentChunkAdapter(db, authorized_owner_id=owner_id, limit=limit_per_adapter), DurableMemoryAdapter(db, authorized_owner_id=owner_id, limit=limit_per_adapter), KnowledgeVersionAdapter(db, authorized_owner_id=owner_id, limit=limit_per_adapter), DecisionRecordAdapter(db, authorized_owner_id=owner_id, limit=limit_per_adapter)]


def _document_item(document: Document, owner: uuid.UUID) -> PersonalKnowledgeItem:
    text = " ".join(value for value in (document.title, document.original_filename, document.content_preview) if value)
    return PersonalKnowledgeItem(item_id=f"document:{document.id}", source_type=SourceType.FILE, source_id=str(document.id), owner_id=str(owner), project_id=_str(document.project_id), file_id=str(document.id), created_at=document.created_at, updated_at=document.updated_at, subject=document.title, topic=document.category, text=text, source_version=str(document.version_number), content_hash=document.checksum, content_reference=f"document:{document.id}", provenance=Provenance(SourceType.FILE, str(document.id), f"document://{document.id}", file_id=str(document.id), version=str(document.version_number), occurred_at=document.imported_at or document.created_at), decision_state=_document_decision(document.active_truth_status), verification_state=_document_verification(document.active_truth_status), source_authority=SourceAuthority.PRIMARY, index_state=_document_index(document.status), metadata={"filename": document.original_filename, "media_type": document.media_type, "deletion_status": _value(document.deletion_status)})


def _decision_item(item_id: str, source_id: str, owner: uuid.UUID, text: str, created_at, status: str, authority: str, superseded_by_id, supersedes_id, provenance: dict, *, subject: str | None = None, project_id=None) -> PersonalKnowledgeItem:
    decision_state = DecisionState.SUPERSEDED if status == "superseded" else DecisionState.DECISION
    verification = VerificationState.DISPUTED if status == "disputed" else VerificationState.VERIFIED if authority in ("founder", "deterministic_source") else VerificationState.UNVERIFIED
    source_authority = SourceAuthority.USER if authority in ("founder", "repeated_founder_preference") else SourceAuthority.VERIFIED_DERIVED if authority == "deterministic_source" else SourceAuthority.DERIVED
    edges = {"supersedes": (f"{item_id.split(':', 1)[0]}:{supersedes_id}",)} if supersedes_id else {}
    replacement = f"{item_id.split(':', 1)[0]}:{superseded_by_id}" if superseded_by_id else None
    return PersonalKnowledgeItem(item_id=item_id, source_type=SourceType.DECISION_RECORD, source_id=source_id, owner_id=str(owner), project_id=_str(project_id), created_at=created_at, subject=subject or text, text=text, content_reference=item_id, provenance=Provenance(SourceType.DECISION_RECORD, source_id, f"memory://{source_id}", occurred_at=created_at), decision_state=decision_state, verification_state=verification, superseded_by=replacement, source_authority=source_authority, index_state=IndexState.INDEXED, relationship_edges=edges, metadata={"canonical_status": status, "canonical_provenance": dict(provenance or {})})


def _document_index(status: IndexStatus) -> IndexState:
    if status == IndexStatus.indexed:
        return IndexState.INDEXED
    if status in (IndexStatus.failed, IndexStatus.storage_failed, IndexStatus.extraction_failed, IndexStatus.indexing_failed, IndexStatus.cancelled):
        return IndexState.FAILED
    return IndexState.PARTIAL


def _document_decision(status: ActiveTruthStatus) -> DecisionState:
    return DecisionState.SUPERSEDED if status in (ActiveTruthStatus.historical, ActiveTruthStatus.superseded) else DecisionState.CLAIM


def _document_verification(status: ActiveTruthStatus) -> VerificationState:
    return VerificationState.DISPUTED if status == ActiveTruthStatus.disputed else VerificationState.VERIFIED if status == ActiveTruthStatus.active else VerificationState.UNVERIFIED


def _str(value) -> str | None:
    return str(value) if value is not None else None


def _value(value) -> str:
    return str(getattr(value, "value", value))


def _locator_uuid(locator: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(locator.rstrip("/").rsplit("/", 1)[-1])
    except ValueError:
        return None
