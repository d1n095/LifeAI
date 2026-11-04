"""Bounded PostgreSQL candidate IDs; deliberately not wired into recall disclosure."""
import uuid

from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy import cast

from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.personal_recall.authorization import DisclosureLevel, RecallAuthorizationError, require_fresh_authority
from app.personal_recall.types import SourceType


def document_candidate_statement(context, query: str, *, limit: int = 100):
    if not query.strip() or len(query) > 4_000 or not 1 <= limit <= 200:
        raise ValueError("invalid candidate query bounds")
    if context.disclosure_level == DisclosureLevel.NONE or SourceType.FILE not in context.allowed_source_types:
        raise RecallAuthorizationError("file candidates are outside authority")
    owner = uuid.UUID(context.owner_id)
    config = cast(literal("simple"), REGCONFIG)
    vector = func.to_tsvector(config, DocumentChunk.text)
    terms = func.plainto_tsquery(config, query)
    scope = context.project_scope
    project_filter = Document.project_id.in_([uuid.UUID(p) for p in scope.identifiers])
    if scope.unrestricted:
        project_filter = Document.project_id.is_not(None)
    if scope.allow_unscoped:
        project_filter = project_filter | Document.project_id.is_(None)
    return (select(DocumentChunk.id, DocumentChunk.document_id)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(DocumentChunk.owner_id == owner, Document.uploaded_by == owner,
                   Document.deleted_at.is_(None), project_filter, vector.op("@@")(terms))
            .order_by(func.ts_rank_cd(vector, terms).desc(), DocumentChunk.id).limit(limit))


def document_candidates(db, *, authorization, resolver, query, limit=100):
    start = require_fresh_authority(authorization, resolver=resolver)
    rows = db.execute(document_candidate_statement(start, query, limit=limit)).all()
    end = require_fresh_authority(authorization, resolver=resolver)
    if end != start:
        raise RecallAuthorizationError("authority changed during candidate generation")
    return tuple((str(row[0]), str(row[1])) for row in rows)
