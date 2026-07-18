from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.cookies import clear_session_cookies
from app.db import get_db
from app.deps import get_current_user
from app.limiter import limiter
from app.models.audit import AuditLog
from app.models.conversation import Conversation, Message
from app.models.document import Document
from app.models.email_verification_token import EmailVerificationToken
from app.models.password_reset_token import PasswordResetToken
from app.models.project import Project, Task
from app.models.refresh_token import RefreshToken
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

    Explicit manual cleanup rather than DB-level ON DELETE CASCADE/SET NULL: this project has
    no migration tool yet (Base.metadata.create_all only creates missing tables — see
    app/main.py — it never alters existing ones), so a constraint added to a model today
    would not retroactively apply to an already-running database. Doing it here instead
    works correctly regardless of what's actually enforced at the DB level.
    """
    if not verify_password(payload.password, user.password_hash):
        record_audit(db, user_id=user.id, action="account_deletion_failed_password", request=request)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Fel lösenord. Kontot har inte raderats.")

    user_id = user.id

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
    db.query(Document).filter_by(uploaded_by=user_id).update({"uploaded_by": None}, synchronize_session=False)
    db.query(UsageLog).filter_by(user_id=user_id).update({"user_id": None}, synchronize_session=False)
    # Audit trail: kept for security/compliance purposes independent of the erasure request,
    # actor identity scrubbed rather than the events themselves being deleted.
    db.query(AuditLog).filter_by(user_id=user_id).update({"user_id": None}, synchronize_session=False)

    db.delete(user)
    db.commit()

    clear_session_cookies(response)
    record_audit(db, user_id=None, action="account_deleted", request=request)
    return {"status": "account_deleted"}
