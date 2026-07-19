import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

logger = logging.getLogger("mainai.account")

from app.audit import record_audit
from app.cookies import clear_session_cookies
from app.db import get_db
from app.deps import get_current_user
from app.limiter import limiter
from app.models.audit import AuditLog
from app.models.conversation import Conversation, Message
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.email_verification_token import EmailVerificationToken
from app.models.import_job import ImportJob
from app.models.knowledge_version import KnowledgeVersion
from app.models.password_reset_token import PasswordResetToken
from app.models.project import Project, Task
from app.models.refresh_token import RefreshToken
from app.models.source_relationship import SourceRelationship
from app.models.usage import UsageLog
from app.models.user import User
from app.schemas import DeleteAccountIn
from app.security import verify_password

router = APIRouter(prefix="/api/account", tags=["account"])


@router.get("/export")
def export_account(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Everything personally identifiable tied to this account: the profile itself, the
    user's own conversations (RLS-isolated per user already — see app/rls.py) and their
    audit trail. Deliberately does NOT include projects/tasks/documents: those are modeled
    as shared company knowledge in this app (only `created_by` for attribution, not access
    control — see app/rls.py), not personal data belonging to the individual who happened to
    create them, so they're out of scope for a personal-data export."""
    conversations = db.query(Conversation).filter_by(user_id=user.id).order_by(Conversation.created_at).all()
    conversations_export = []
    for conversation in conversations:
        messages = db.query(Message).filter_by(conversation_id=conversation.id).order_by(Message.created_at).all()
        conversations_export.append(
            {
                "id": str(conversation.id),
                "title": conversation.title,
                "created_at": conversation.created_at.isoformat(),
                "updated_at": conversation.updated_at.isoformat(),
                "messages": [
                    {
                        "role": m.role.value,
                        "content": m.content,
                        "provider": m.provider,
                        "model": m.model,
                        "created_at": m.created_at.isoformat(),
                    }
                    for m in messages
                ],
            }
        )

    audit_entries = db.query(AuditLog).filter_by(user_id=user.id).order_by(AuditLog.created_at).all()

    record_audit(db, user_id=user.id, action="account_data_exported", request=request)

    return {
        "account": {
            "id": str(user.id),
            "email": user.email,
            "role": user.role.value,
            "email_verified": user.email_verified,
            "created_at": user.created_at.isoformat(),
        },
        "conversations": conversations_export,
        "audit_log": [
            {
                "action": a.action,
                "entity_type": a.entity_type,
                "entity_id": a.entity_id,
                "created_at": a.created_at.isoformat(),
            }
            for a in audit_entries
        ],
    }


@router.delete("")
@limiter.limit("5/minute")
def delete_account(
    request: Request,
    response: Response,
    payload: DeleteAccountIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Permanent, irreversible deletion. Requires re-entering the current password: the
    session cookie alone (which is what every other authenticated endpoint relies on) isn't
    treated as sufficient proof of intent for a destructive, unrecoverable action.

    Explicit manual cleanup rather than DB-level ON DELETE CASCADE/SET NULL: none of the
    relevant foreign keys are declared with CASCADE/SET NULL at the database level (see
    backend/alembic/versions/) — only usage_log.user_id/conversation_id are, for a different,
    already-documented reason (see app/models/usage.py). Doing the cleanup explicitly here
    works correctly regardless of what's enforced at the DB level, and keeps the exact
    all-personal-data-deleted / all-shared-data-anonymized split in one auditable place.
    """
    if not verify_password(payload.password, user.password_hash):
        record_audit(db, user_id=user.id, action="account_deletion_failed_password", request=request)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Fel lösenord. Kontot har inte raderats.")

    user_id = user.id

    # Everything below runs as one transaction — either the whole account disappears
    # cleanly, or none of it does. autocommit=False (app/db.py) already means nothing here
    # is visible to any other connection until the single db.commit() at the end; the
    # explicit try/except/rollback makes that guarantee active too, not just passive: if
    # anything raises partway through, the transaction is rolled back explicitly before the
    # exception propagates, rather than relying on Session.close() (app/db.py's get_db())
    # to clean up an already-failed transaction implicitly.
    try:
        # Personal data: deleted outright, not anonymized.
        conversation_ids = [row.id for row in db.query(Conversation.id).filter_by(user_id=user_id).all()]
        if conversation_ids:
            db.query(Message).filter(Message.conversation_id.in_(conversation_ids)).delete(synchronize_session=False)
            db.query(Conversation).filter_by(user_id=user_id).delete(synchronize_session=False)
        db.query(RefreshToken).filter_by(user_id=user_id).delete(synchronize_session=False)
        db.query(EmailVerificationToken).filter_by(user_id=user_id).delete(synchronize_session=False)
        db.query(PasswordResetToken).filter_by(user_id=user_id).delete(synchronize_session=False)

        # Shared company data the user merely created or used: kept, attribution scrubbed —
        # deleting a person's account must not silently delete company knowledge other users
        # still rely on (same reasoning as app/rls.py's access-control model for these tables).
        db.query(Project).filter_by(created_by=user_id).update({"created_by": None}, synchronize_session=False)
        db.query(Task).filter_by(created_by=user_id).update({"created_by": None}, synchronize_session=False)

        # Documents (Founder Knowledge Studio): unlike Project/Task, these are now
        # owner-scoped, RLS-protected personal/founder data (migration 0006 — see
        # app/models/document.py's docstring), not shared company knowledge — anonymizing
        # uploaded_by to NULL would both violate the new documents_isolation RLS policy's
        # WITH CHECK (NULL never matches any current_user_id) and, even if it didn't, would
        # just leave permanently unreadable orphan rows instead of a clean deletion. Deleted
        # outright instead, same as conversations above. document_chunks/knowledge_versions/
        # source_relationships all have ON DELETE CASCADE or ondelete="CASCADE" back to
        # documents.id (see those models) so this single delete is enough for the bulk of
        # them, but knowledge_import_jobs has no FK to documents (a job outlives the
        # documents it created, e.g. to show "3 succeeded, 1 failed" after a partial import)
        # so it's deleted explicitly by owner_id here too.
        document_ids = [row.id for row in db.query(Document.id).filter_by(uploaded_by=user_id).all()]
        if document_ids:
            db.query(DocumentChunk).filter(DocumentChunk.document_id.in_(document_ids)).delete(synchronize_session=False)
            db.query(KnowledgeVersion).filter(KnowledgeVersion.source_id.in_(document_ids)).delete(synchronize_session=False)
            db.query(SourceRelationship).filter(
                or_(
                    SourceRelationship.from_source_id.in_(document_ids),
                    SourceRelationship.to_source_id.in_(document_ids),
                )
            ).delete(synchronize_session=False)
            db.query(Document).filter_by(uploaded_by=user_id).delete(synchronize_session=False)
        db.query(ImportJob).filter_by(owner_id=user_id).delete(synchronize_session=False)

        db.query(UsageLog).filter_by(user_id=user_id).update({"user_id": None}, synchronize_session=False)
        # Audit trail: kept for security/compliance purposes independent of the erasure
        # request, actor identity scrubbed rather than the events themselves being deleted.
        db.query(AuditLog).filter_by(user_id=user_id).update({"user_id": None}, synchronize_session=False)

        db.delete(user)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Kontoradering misslyckades för user_id=%s, återställd (rollback).", user_id)
        # Best-effort only, in a fresh transaction now that the failed one has been rolled
        # back — if the DB itself is unreachable this write can fail too, which is fine:
        # the HTTP 500 and the exception log above are the actual guarantee here, this is
        # just supplementary visibility.
        try:
            record_audit(db, user_id=user_id, action="account_deletion_failed_error", request=request)
        except Exception:
            logger.exception("Kunde inte heller skriva audit-loggen för den misslyckade raderingen.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Kontot kunde inte raderas just nu. Inga ändringar gjordes — försök igen senare.",
        )

    clear_session_cookies(response)
    record_audit(db, user_id=None, action="account_deleted", request=request)
    return {"status": "account_deleted"}
