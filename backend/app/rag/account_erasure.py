"""Account erasure domain service (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8, PR #31's
Pass 26 account-integration slice) — the ONE place `app/routers/account.py`'s
`DELETE /api/account` delegates to, matching the shared-domain-service pattern established for
source purging (app/rag/source_purge.py).

Three real gaps a founder review found in the PRE-Pass-26 account deletion:

1. **`erase_owner_memory()` was never called.** S1A's controlled memory-erasure function
   (migration 0019) existed, but nothing invoked it — worse, the old `delete_account` deleted
   `documents` directly, which `document_source_units.document_id`'s plain-RESTRICT FK (no
   `ON DELETE` action) would reject outright the moment ANY `memory_source_units` row existed
   for the account, the same gap app/rag/source_purge.py's own module docstring already
   documents for single-source deletion. `erase_account_data()` below calls
   `erase_owner_memory()` BEFORE any document/chunk/version row is touched — the same ordering
   requirement source_purge.py's purge-before-chunk-delete already established, just at
   account scope instead of single-document scope. No direct Python `DELETE`/`UPDATE` against
   `knowledge_claims`/`memory_source_units`/`document_source_units` — those tables' own
   triggers (`trg_msu_no_delete`/`trg_dsu_no_delete`, migration 0019) reject anything that
   isn't `erase_owner_memory()`/`erase_owner_memory_admin()` or their sibling transition
   functions.

2. **No physical blob deletion at all.** `Document.storage_key`/`ImportJob.source_storage_key`
   point at real original files in `app/storage/` — the pre-Pass-26 deletion only ever removed
   DATABASE rows, silently orphaning every original file the account ever uploaded. Fixed via
   `storage_deletion_tasks` (migration 0021): every unique storage_key this owner's
   Document/ImportJob rows reference is recorded as a durable, retryable task BEFORE those
   rows are deleted, in the SAME transaction as the rest of the erasure — see that migration's
   module docstring for why a durable record (not just an in-memory best-effort attempt) is
   required. `erase_account_data()` makes one immediate best-effort deletion attempt right
   after its own commit succeeds (mirroring source_purge.py's Phase A/Phase B split); anything
   left `pending`/`failed` is retried later by app/worker.py, across ALL owners' tasks — see
   that module's `_retry_storage_deletion_tasks`.

3. **The `account_deleted` audit entry was written by the ROUTER, in a SEPARATE commit, AFTER
   the main erasure transaction had already committed** (`record_audit`'s default
   `commit=True`) — the exact same class of bug Pass 22 already fixed for
   `source_purged` (see source_purge.py's module docstring). A failure in that second commit
   meant an HTTP caller could see a 500 for an account that had, in fact, already been
   permanently, durably erased. Fixed by writing the audit row here, inside the SAME
   transaction (`record_audit(..., commit=False)`), with `user_id=None` (the account no longer
   exists once this commits) and a synthetic, unattributable `operation_id` as `entity_id`
   instead of the now-meaningless `user_id`.

Everything from "lock the User row" through "delete the User row" is ONE transaction: any
failure anywhere in that sequence — including inserting a `storage_deletion_tasks` row, or
`erase_owner_memory()` itself raising — rolls back the ENTIRE erasure. `storage.delete()` is
NEVER called before that transaction commits (see item 2 above) — the exact same "physical
delete must never precede the DB commit that makes it safe" discipline
app/rag/source_purge.py's module docstring already establishes for single-source purges.

Pass 26 also closes a race a founder review found between account erasure and a CONCURRENT
upload for the SAME owner: `acquire_owner_erasure_lock()` (app/rag/blob_references.py) is
held for this whole transaction, and `POST /api/library/import` (app/routers/library.py)
takes the SAME lock, as the very first thing it does, before `storage.write_stream()` even
runs — see that lock function's own docstring for the full race and why acquiring it
"before write_stream, never after" is what actually closes it.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import or_
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.audit import record_audit
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
from app.models.storage_deletion_task import StorageDeletionReason, StorageDeletionStatus, StorageDeletionTask
from app.models.usage import UsageLog
from app.models.user import User
from app.rag.blob_references import acquire_owner_erasure_lock, acquire_storage_key_lock, storage_key_still_referenced
from app.storage import StorageError, get_storage

logger = logging.getLogger("mainai.rag.account_erasure")

ERASURE_REASON = "account_erasure"


@dataclass
class AccountErasureResult:
    operation_id: uuid.UUID
    storage_tasks_created: int = 0
    storage_tasks_purged_immediately: int = 0
    storage_tasks_retained_shared_immediately: int = 0
    storage_tasks_pending_or_failed: list[str] = field(default_factory=list)


def attempt_storage_deletion_task(db: Session, task: StorageDeletionTask) -> None:
    """Attempts ONE storage_deletion_task: acquires the SAME storage-key-scoped advisory lock
    app/rag/source_purge.py's `retry_source_blob_purge()` and the upload endpoint already
    share (closing the same TOCTOU race described there), re-checks the global, cross-owner
    reference policy (`storage_key_still_referenced()` / migration 0020's
    `storage_key_still_referenced_global()`), and commits this task's OWN terminal status in
    its own, independent transaction — never touches any other table. Safe to call any number
    of times for the same task: `StorageBackend.delete()` (LocalFilesystemStorage) is
    idempotent on an already-missing key, so a delete that succeeds but whose status-commit
    then fails is correctly retried without erroring on the second attempt.

    A no-op if the task is already at a terminal-success status (`purged`/`retained_shared`)
    — callers (the immediate best-effort attempt, and app/worker.py's retry loop) may safely
    call this for any task regardless of its current status."""
    if task.status in (StorageDeletionStatus.purged, StorageDeletionStatus.retained_shared):
        return

    acquire_storage_key_lock(db, task.storage_key)
    task.status = StorageDeletionStatus.processing
    task.attempt_count += 1
    db.add(task)
    db.flush()

    storage = get_storage()
    if storage_key_still_referenced(db, task.storage_key):
        # Another owner's live Document/ImportJob still needs this exact content-addressed
        # blob — the erased owner's own rows are already gone (this task only ever exists
        # once the erasure transaction that created it has already committed), so this is a
        # correct, terminal SUCCESS, not a failure. The physical file must NOT be deleted.
        task.status = StorageDeletionStatus.retained_shared
        task.last_error = None
        task.completed_at = datetime.utcnow()
    else:
        try:
            storage.delete(task.storage_key)
            task.status = StorageDeletionStatus.purged
            task.last_error = None
            task.completed_at = datetime.utcnow()
        except StorageError as exc:
            task.status = StorageDeletionStatus.failed
            task.last_error = str(exc)[:2000]
            logger.exception("Kunde inte radera blob %s fysiskt (task %s).", task.storage_key, task.id)

    db.add(task)
    db.commit()


def attempt_pending_storage_deletions_for_operation(db: Session, operation_id: uuid.UUID) -> AccountErasureResult:
    """The immediate, best-effort attempt `erase_account_data()` makes right after its own DB
    phase commits — scoped ONLY to the tasks that SAME operation just created (never a broad,
    cross-operation scan; app/worker.py's retry loop owns that). A failure attempting any
    individual task is caught, logged, and never re-raised: the account is already, durably
    erased by the time this runs, so a per-task failure here must never look like the erasure
    itself failed. Returns counts for observability/tests; callers that don't need them may
    ignore the return value."""
    result = AccountErasureResult(operation_id=operation_id)
    tasks = db.query(StorageDeletionTask).filter_by(operation_id=operation_id, status=StorageDeletionStatus.pending).all()
    for task in tasks:
        try:
            attempt_storage_deletion_task(db, task)
        except Exception:
            db.rollback()
            logger.exception(
                "Omedelbart raderingsförsök misslyckades för storage_deletion_task %s (operation %s) -- "
                "kvarstår återförsökbar via worker-retryn.",
                task.id,
                operation_id,
            )
            continue
        if task.status == StorageDeletionStatus.purged:
            result.storage_tasks_purged_immediately += 1
        elif task.status == StorageDeletionStatus.retained_shared:
            result.storage_tasks_retained_shared_immediately += 1
        else:
            result.storage_tasks_pending_or_failed.append(str(task.id))
    return result


def erase_account_data(db: Session, user: User, *, client_ip: str | None = None) -> AccountErasureResult:
    """Permanent, irreversible account erasure — one atomic DB transaction (see module
    docstring), followed by a best-effort blob-deletion attempt that never rolls the DB phase
    back regardless of outcome. Password verification is the ROUTER's responsibility (see
    app/routers/account.py) — this function assumes the caller has already authenticated the
    request as genuinely coming from `user` themselves; it never re-checks a password.

    `client_ip` is a plain string, not a fastapi.Request — the router extracts it (same
    convention as app/rag/source_purge.py) so this domain-layer module never imports fastapi.
    """
    owner_id = user.id
    operation_id = uuid.uuid4()

    try:
        # Serializes a concurrent SECOND erasure attempt for the SAME account — the second
        # call's own FOR UPDATE blocks until the first either commits (this row disappears,
        # so the second query below returns nothing and get_current_user's own earlier lookup
        # for that request would already have 401'd before ever reaching here on any request
        # AFTER the first commits) or rolls back (row unchanged, second attempt proceeds
        # normally).
        locked_user = db.query(User).filter_by(id=owner_id).with_for_update().first()
        if locked_user is None:
            raise LookupError(f"user {owner_id} not found (already erased?)")

        # Pass 26: closes the erasure/upload race — see acquire_owner_erasure_lock's own
        # docstring for the full race and why this must be acquired BEFORE inventorying
        # storage keys (a concurrent upload either fully commits before this proceeds, and so
        # is correctly swept into the inventory below, or is still blocked and so cannot yet
        # have written anything this erasure would need to account for).
        acquire_owner_erasure_lock(db, owner_id)

        # --- Storage-key inventory + durable deletion tasks (BEFORE any personal row dies) ---
        document_keys = {
            row[0]
            for row in db.query(Document.storage_key)
            .filter(Document.uploaded_by == owner_id, Document.storage_key.isnot(None))
            .all()
        }
        import_job_keys = {
            row[0]
            for row in db.query(ImportJob.source_storage_key)
            .filter(ImportJob.owner_id == owner_id, ImportJob.source_storage_key.isnot(None))
            .all()
        }
        storage_keys = document_keys | import_job_keys  # unique keys, deduplicated across both sources
        for key in storage_keys:
            db.add(
                StorageDeletionTask(
                    operation_id=operation_id,
                    storage_key=key,
                    reason=StorageDeletionReason.account_erasure,
                    status=StorageDeletionStatus.pending,
                )
            )

        # --- S1A memory erasure via the controlled, SECURITY DEFINER, owner-verified function
        # (migration 0019) — never a direct Python DELETE/UPDATE against knowledge_claims/
        # memory_source_units/document_source_units, which their own triggers reject from any
        # other path. Must run BEFORE the Document delete below: document_source_units.
        # document_id's plain-RESTRICT FK would otherwise reject that delete outright the
        # moment any memory_source_units row exists for this account (see module docstring).
        db.execute(sa_text("SELECT erase_owner_memory(:owner_id)"), {"owner_id": str(owner_id)})

        # --- Personal data: deleted outright, not anonymized. ---
        conversation_ids = [row.id for row in db.query(Conversation.id).filter_by(user_id=owner_id).all()]
        if conversation_ids:
            db.query(Message).filter(Message.conversation_id.in_(conversation_ids)).delete(synchronize_session=False)
            db.query(Conversation).filter_by(user_id=owner_id).delete(synchronize_session=False)
        db.query(RefreshToken).filter_by(user_id=owner_id).delete(synchronize_session=False)
        db.query(EmailVerificationToken).filter_by(user_id=owner_id).delete(synchronize_session=False)
        db.query(PasswordResetToken).filter_by(user_id=owner_id).delete(synchronize_session=False)

        # --- Shared company data the user merely created or used: kept, attribution scrubbed.
        db.query(Project).filter_by(created_by=owner_id).update({"created_by": None}, synchronize_session=False)
        db.query(Task).filter_by(created_by=owner_id).update({"created_by": None}, synchronize_session=False)

        # --- Documents (owner-scoped, RLS-protected — see app/routers/account.py's previous
        # docstring for why these are deleted outright, same as conversations, not anonymized).
        document_ids = [row.id for row in db.query(Document.id).filter_by(uploaded_by=owner_id).all()]
        if document_ids:
            db.query(DocumentChunk).filter(DocumentChunk.document_id.in_(document_ids)).delete(synchronize_session=False)
            db.query(KnowledgeVersion).filter(KnowledgeVersion.source_id.in_(document_ids)).delete(synchronize_session=False)
            db.query(SourceRelationship).filter(
                or_(
                    SourceRelationship.from_source_id.in_(document_ids),
                    SourceRelationship.to_source_id.in_(document_ids),
                )
            ).delete(synchronize_session=False)
            db.query(Document).filter_by(uploaded_by=owner_id).delete(synchronize_session=False)
        db.query(ImportJob).filter_by(owner_id=owner_id).delete(synchronize_session=False)

        db.query(UsageLog).filter_by(user_id=owner_id).update({"user_id": None}, synchronize_session=False)
        # Audit trail: kept for security/compliance purposes independent of the erasure
        # request, actor identity scrubbed rather than the events themselves being deleted.
        db.query(AuditLog).filter_by(user_id=owner_id).update({"user_id": None}, synchronize_session=False)

        db.delete(locked_user)

        # Pass 26: written HERE, inside this same transaction (commit=False — joins the
        # single commit below, not a separate one) — see module docstring, item 3. user_id is
        # None (the account is gone the instant this commits); entity_id is the synthetic
        # operation_id, never the now-erased user's own id, so this row itself reveals nothing
        # about which account it was once the erasure has happened.
        record_audit(
            db,
            user_id=None,
            action="account_deleted",
            entity_type="account",
            entity_id=str(operation_id),
            ip_address=client_ip,
            commit=False,
        )

        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Kontoradering misslyckades för user_id=%s (operation=%s), återställd (rollback).", owner_id, operation_id)
        raise

    result = AccountErasureResult(operation_id=operation_id, storage_tasks_created=len(storage_keys))
    try:
        immediate = attempt_pending_storage_deletions_for_operation(db, operation_id)
        result.storage_tasks_purged_immediately = immediate.storage_tasks_purged_immediately
        result.storage_tasks_retained_shared_immediately = immediate.storage_tasks_retained_shared_immediately
        result.storage_tasks_pending_or_failed = immediate.storage_tasks_pending_or_failed
    except Exception:
        # The erasure transaction above already committed successfully — that's the real,
        # durable outcome of this call. A failure making the immediate best-effort attempt
        # must never be raised as erase_account_data's own failure, and app/worker.py's retry
        # loop (see that module) can always finish the job later.
        db.rollback()
        logger.exception(
            "Omedelbart blob-raderingsförsök misslyckades helt för operation=%s efter en lyckad kontoradering -- "
            "kvarstår återförsökbart via worker-retryn.",
            operation_id,
        )

    return result
