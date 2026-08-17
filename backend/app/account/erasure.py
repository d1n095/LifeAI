"""Account erasure domain service (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8, PR #31's
Pass 26 account-integration slice) — the ONE place `app/routers/account.py`'s
`DELETE /api/account` delegates to, matching the shared-domain-service pattern established for
source purging (app/storage/purge.py).

Three real gaps a founder review found in the PRE-Pass-26 account deletion:

1. **`erase_owner_memory()` was never called.** S1A's controlled memory-erasure function
   (migration 0019) existed, but nothing invoked it — worse, the old `delete_account` deleted
   `documents` directly, which `document_source_units.document_id`'s plain-RESTRICT FK (no
   `ON DELETE` action) would reject outright the moment ANY `memory_source_units` row existed
   for the account, the same gap app/storage/purge.py's own module docstring already
   documents for single-source deletion. `erase_account_data()` below calls
   `erase_owner_memory()` BEFORE any document/chunk/version row is touched — the same ordering
   requirement purge.py's purge-before-chunk-delete already established, just at
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
   after its own commit succeeds (mirroring purge.py's Phase A/Phase B split); anything
   left `pending`/`failed` is retried later by app/worker.py, across ALL owners' tasks — see
   that module's `_retry_storage_deletion_tasks`.

3. **The `account_deleted` audit entry was written by the ROUTER, in a SEPARATE commit, AFTER
   the main erasure transaction had already committed** (`record_audit`'s default
   `commit=True`) — the exact same class of bug Pass 22 already fixed for
   `source_purged` (see purge.py's module docstring). A failure in that second commit
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
app/storage/purge.py's module docstring already establishes for single-source purges.

Pass 26 also closes a race a founder review found between account erasure and a CONCURRENT
upload for the SAME owner: `acquire_owner_erasure_lock()` (app/storage/references.py) is
held for this whole transaction, and `POST /api/library/import` (app/routers/library.py)
takes the SAME lock, as the very first thing it does, before `storage.write_stream()` even
runs — see that lock function's own docstring for the full race and why acquiring it
"before write_stream, never after" is what actually closes it.

Pass 27 (a second founder review) closed one more real gap: `storage_deletion_tasks` has no
owner_id/RLS at all (see migration 0021's docstring), and the boot privilege policy used to
grant the ordinary, request-scoped `mainai_app` role SELECT+INSERT+UPDATE on the WHOLE table —
meaning any request session (not just this module's own code) could technically read every
account's pending erasure operation ids/storage keys, or rewrite any task's status. Fixed on
two fronts:
  - `backend/scripts/security/s1a_privilege_policy.py` now grants `mainai_app` INSERT ONLY on this
    table — the account-erasure transaction below still runs on the ordinary per-request
    session, but it only ever needs to CREATE task rows, never read or update them back.
  - `attempt_pending_storage_deletions_for_operation()` no longer accepts the caller's
    request-scoped session at all — it opens its OWN connection on `_MaintenanceSession`
    (bound to the same admin/migration engine app/worker.py's `_ClaimSession` already uses for
    exactly this "must see/claim rows with no owner scope" shape), claims this operation's own
    tasks atomically via `claim_storage_deletion_tasks()`, and closes that session again before
    returning — the request session `erase_account_data()` itself runs on never touches this
    table beyond the initial INSERTs.
  - `claim_storage_deletion_tasks()` replaces a plain, unlocked `.all()` scan (both here and in
    app/worker.py's retry loop) with the same atomic `... FOR UPDATE SKIP LOCKED` claim pattern
    app/jobs/lease.py's `claim_next_job()` already uses for `knowledge_import_jobs` — a bounded
    batch, so two concurrent claimers (this module's own immediate attempt racing the worker's
    broad retry scan, or two worker processes) can never claim and re-process the same row at
    the same time, and a task stuck `processing` because its claimer crashed becomes
    reclaimable again once its lease (`updated_at` plus `lease_seconds`) has expired — the same
    "abandoned claim" shape `claim_next_job`'s own lease-expiry reclaim already handles for
    import jobs. The storage-key advisory lock (`acquire_storage_key_lock`) remains the final
    integrity boundary underneath all of this, unchanged.

Pass 27, blob-write-path audit (every call site that durably writes a user-owned blob, per the
founder's explicit request): grepped the whole backend for `storage.write`/`storage.write_
stream`/`StorageBackend`. Findings:
  - `app/routers/library.py`'s upload endpoint — already covered (Pass 26): takes
    `acquire_owner_erasure_lock` before `storage.write_stream()`, then re-verifies the owner
    still exists.
  - `app/rag/library_import.py`'s `_store_bytes()` (called from `_import_one_file`/
    `_resume_incomplete_document` while app/worker.py processes an ALREADY-CLAIMED
    `ImportJob`) — genuinely user-owned, and NOT covered by the upload endpoint's lock, which
    only guards the moment that job's raw package was first accepted, not the worker's later,
    asynchronous per-file extraction. A `pg_advisory_xact_lock` held for an entire import job's
    duration isn't the right tool here either — `run_import_job` commits after every file to
    stay resumable, and a transaction-scoped lock doesn't survive past its own commit. Instead
    of a lock that wouldn't actually hold, `erase_account_data()` below refuses to proceed at
    all while ANY of this owner's `ImportJob` rows is currently claimed and actively running
    (`status='running'` with an unexpired `lease_expires_at`) — see `AccountErasureBlockedError`
    — closing the realistic, sustained-duration case (a worker actively extracting/embedding a
    multi-file import) rather than leaving it silently racy. A narrower residual window remains
    between that check and this transaction's own commit, where a `pending` job already queued
    for this owner could still be claimed by `claim_next_job()` before erasure's DELETE of
    `knowledge_import_jobs` commits (Postgres MVCC: the claim query runs against the
    not-yet-committed snapshot) — closing that fully would need `claim_next_job()` itself to
    take a per-owner lock before claiming ANY job, which conflicts with its whole reason for
    being a single, un-owner-scoped, cross-owner query; left as documented, explicit follow-up
    scope rather than a rushed, unverified locking redesign under this pass.
  - `app/project_memory.py`'s three `storage.write_stream()` call sites
    (`ingest_doc`/`ProjectCheckpoint` brief storage/system-map storage) — NOT user-owned data.
    `ProjectSource`/`ProjectCheckpoint` are explicitly documented (see
    app/models/project_memory.py) as founder-WIDE, singleton project-memory state — "Not
    RLS-protected... founder-wide project state, not per-user data" — the same category as
    `provider_config`/`provider_verification_checks`. `created_by`/`ingested_by` are free-text
    labels (`String(64)`), never a `users.id` foreign key. Correctly out of account erasure's
    scope entirely: there is no per-account ownership here to erase.

Pass 28 (a third founder review) closed three more real gaps:

1. **Immediate-attempt infinite retry loop.** `attempt_pending_storage_deletions_for_operation()`'s
   `while True` loop, combined with Pass 27's `claim_storage_deletion_tasks()` treating
   `pending` and `failed` as equally, immediately claimable, reintroduced a known bug class: a
   task that fails with a PERMANENT `StorageError` got marked `failed`, and the very next loop
   iteration claimed and re-attempted that SAME task again — forever, an unbounded busy loop
   inside a single HTTP request, for as long as the error stayed permanent. Fixed by
   `claim_storage_deletion_tasks()`'s new `include_failed` parameter: the immediate attempt now
   claims with `include_failed=False`, so it can only ever see `pending` (or lease-expired
   `processing`) rows — each task created by THIS operation is tried at most once here.
   Anything left `failed` is entirely the worker's (app/worker.py's retry loop) responsibility,
   which claims with the default `include_failed=True` and correctly honors a bounded,
   exponential, jittered backoff (`next_attempt_at`, migration 0022, set by
   `attempt_storage_deletion_task()` via `app.jobs.retry.compute_backoff_seconds` on a failed
   attempt) instead of retrying immediately.

2. **INSERT-only was still dangerous.** Pass 27 narrowed `mainai_app` to INSERT-only on
   `storage_deletion_tasks`, reasoning that a bare metadata INSERT was harmless. A founder
   review pointed out this was still wrong: INSERT into this specific table is INDIRECT ACCESS
   TO A PRIVILEGED PHYSICAL-DELETE OPERATION, since nothing in the database verified an
   inserted `storage_key` actually belonged to the inserting owner, or referenced anything
   real at all — `app/project_memory.py`'s founder-wide blobs are exactly the kind of data a
   maliciously- or accidentally-queued arbitrary key could destroy with zero trace, since the
   worker's own reference check (`storage_key_still_referenced_global()`, migration 0020) only
   ever looks at `documents`/`knowledge_import_jobs`. Fixed by `mainai_app` getting ZERO direct
   privileges on `storage_deletion_tasks` at all (see `backend/scripts/security/s1a_privilege_policy.py`)
   and a new SECURITY DEFINER function, `enqueue_account_erasure_storage_task()` (migration
   0022), the ONLY way an ordinary session may create a task row — it re-derives the caller
   from `app.current_user_id`, explicitly re-verifies `p_storage_key` belongs to that caller via
   `Document.storage_key`/`ImportJob.source_storage_key` (never trusting the calling Python
   code's own inventory query as authorization), sets `reason`/`status` itself, and is
   idempotent on `(operation_id, storage_key)`. `erase_account_data()`'s storage-key inventory
   loop below calls this function via `db.execute(sa_text("SELECT enqueue_account_erasure_
   storage_task(...)"))` instead of `db.add(StorageDeletionTask(...))`.

3. **The pending-ImportJob/account-erasure race, previously left as documented follow-up
   scope (see item 2 above, "A narrower residual window remains...") — a founder review
   explicitly rejected leaving it open: "Det räcker inte att dokumentera racet som follow-up.
   Det är precis den race account-slicen skulle stänga."** Closed by redesigning
   `app/jobs/lease.py`'s `claim_next_job()` into a two-phase, owner-locked claim (see that
   module's own docstring for the full mechanism): a lock-free candidate SELECT, THEN
   `acquire_owner_erasure_lock()` for that candidate's owner BEFORE any row-level lock is
   taken (never after — the same ordering `erase_account_data()` above already follows, so the
   two can never deadlock against each other), THEN an atomic re-validated claim of exactly
   that candidate, retrying with a fresh candidate on a lost race. Whichever side (a worker's
   claim, or this function's own erasure transaction) acquires a given owner's lock first now
   fully commits or rolls back before the other's row-level work can even begin — the pending
   job is either safely swept into this transaction's own storage-key inventory before a
   worker can start writing new blobs against it, or the worker safely claims and starts the
   job before erasure gets far enough to see it as blocking (via the existing
   `AccountErasureBlockedError` guard above, unchanged).
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import or_
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session, sessionmaker

from app.audit import record_audit
from app.db import migration_engine
from app.jobs.retry import compute_backoff_seconds
from app.models.audit import AuditLog
from app.models.conversation import Conversation, Message
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.email_verification_token import EmailVerificationToken
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.knowledge_version import KnowledgeVersion
from app.models.mainai_job import MainAIJob
from app.models.password_reset_token import PasswordResetToken
from app.models.project import Project, Task
from app.models.refresh_token import RefreshToken
from app.models.source_relationship import SourceRelationship
from app.models.storage_deletion_task import StorageDeletionStatus, StorageDeletionTask
from app.models.usage import UsageLog
from app.models.user import User
from app.storage import StorageError, get_storage
from app.storage.references import acquire_owner_erasure_lock, acquire_storage_key_lock, storage_key_still_referenced

logger = logging.getLogger("mainai.account.erasure")

ERASURE_REASON = "account_erasure"


class AccountErasureBlockedError(Exception):
    """Raised by `erase_account_data()` when a genuinely in-flight worker job for this owner
    is currently claimed and running (see that function's docstring, "known residual upload
    race" section) — erasure refuses to proceed rather than delete `knowledge_import_jobs`/
    `documents` rows out from under a worker actively writing NEW, not-yet-inventoried blobs
    for them. Always propagates to the caller (never caught by `erase_account_data()` itself);
    `app/routers/account.py` maps this to a distinct, non-500 HTTP response."""

# Pass 27: the SAME superuser/admin connection app/worker.py's `_ClaimSession` already uses
# for cross-owner `knowledge_import_jobs` claiming — a second, independently-defined
# sessionmaker bound to the same engine (exactly like tests/backend/jobs/test_worker.py's own
# `_ClaimSession` already does), not a shared import from worker.py, to avoid a needless
# cross-module coupling between the account-erasure and durable-worker domains.
_MaintenanceSession = sessionmaker(bind=migration_engine)

# How long a claimed-but-not-yet-terminal `processing` task's claim is honored before another
# claimer may reclaim it — mirrors app/config.py's `worker_lease_seconds` default (a storage
# deletion attempt is a single, fast local/network I/O call, not a long-running import, but
# reusing the same order of magnitude keeps one fewer config knob to reason about).
STORAGE_DELETION_LEASE_SECONDS = 300


def claim_storage_deletion_tasks(
    db: Session,
    *,
    limit: int,
    lease_seconds: int = STORAGE_DELETION_LEASE_SECONDS,
    operation_id: uuid.UUID | None = None,
    include_failed: bool = True,
) -> list[uuid.UUID]:
    """Atomically claims up to `limit` eligible `storage_deletion_tasks` rows for the CALLER of
    this function, marking them `processing` with a fresh lease (`updated_at`) in the same
    statement — the same `UPDATE ... WHERE id = ANY(SELECT ... FOR UPDATE SKIP LOCKED ...)
    RETURNING id` shape app/jobs/lease.py's `claim_next_job()` already uses for
    `knowledge_import_jobs`, applied here so no two concurrent callers (this module's own
    immediate best-effort attempt racing app/worker.py's broad retry scan, or two worker
    processes) can ever claim and reprocess the same row at the same time.

    Eligible: `pending` (never yet attempted) always; `processing` whose lease has expired
    (`updated_at` older than `lease_seconds` ago — the same "claimer crashed/was killed
    mid-attempt" shape `claim_next_job`'s own `lease_expires_at` reclaim already handles); and,
    when `include_failed=True`, `failed` tasks whose backoff has elapsed (`next_attempt_at` is
    NULL or already past — see `attempt_storage_deletion_task`'s docstring for how that's set).
    A `processing` row still within its lease is left alone — someone else is genuinely still
    working it right now.

    `include_failed=False` (Pass 28 — see module docstring's immediate-retry-loop fix): the
    caller wants ONLY tasks this exact call has never touched yet, e.g. the immediate
    post-erasure best-effort attempt, which must try each newly created task at most once and
    leave anything that fails to the worker's own backed-off retry, not loop on it again within
    the same request.

    `operation_id`, when given, scopes the claim to ONE erasure operation's own tasks (the
    immediate best-effort attempt right after that operation's own commit, never a broader
    cross-operation view); omitted, claims across ALL operations (app/worker.py's retry scan).
    MUST be called on a privileged session (this table grants `mainai_app` no direct
    privileges at all — see module docstring) — `_MaintenanceSession`/`app.worker._ClaimSession`,
    never a request-scoped `SessionLocal`.

    Returns only task ids; callers re-load each via `db.get(StorageDeletionTask, task_id)` on
    THIS SAME session so the loaded ORM object reflects the just-claimed `processing` status,
    not a stale pre-claim snapshot."""
    params: dict = {"limit": limit, "lease_seconds": lease_seconds}
    operation_filter = ""
    if operation_id is not None:
        operation_filter = "AND operation_id = :operation_id"
        params["operation_id"] = str(operation_id)
    failed_clause = (
        "OR (status = 'failed' AND (next_attempt_at IS NULL OR next_attempt_at <= now()))" if include_failed else ""
    )
    rows = db.execute(
        sa_text(f"""
            UPDATE storage_deletion_tasks
            SET status = 'processing', updated_at = now()
            WHERE id = ANY(
                SELECT id FROM storage_deletion_tasks
                WHERE (
                    status = 'pending'
                    {failed_clause}
                    OR (status = 'processing' AND updated_at < now() - make_interval(secs => :lease_seconds))
                )
                {operation_filter}
                ORDER BY created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT :limit
            )
            RETURNING id
        """),
        params,
    ).fetchall()
    db.commit()
    return [row[0] for row in rows]


@dataclass
class AccountErasureResult:
    operation_id: uuid.UUID
    storage_tasks_created: int = 0
    storage_tasks_purged_immediately: int = 0
    storage_tasks_retained_shared_immediately: int = 0
    storage_tasks_pending_or_failed: list[str] = field(default_factory=list)


def attempt_storage_deletion_task(db: Session, task: StorageDeletionTask) -> None:
    """Attempts ONE storage_deletion_task: acquires the SAME storage-key-scoped advisory lock
    app/storage/purge.py's `retry_source_blob_purge()` and the upload endpoint already
    share (closing the same TOCTOU race described there), re-checks the global, cross-owner
    reference policy (`storage_key_still_referenced()` / migration 0020's
    `storage_key_still_referenced_global()`), and commits this task's OWN terminal status in
    its own, independent transaction — never touches any other table. Safe to call any number
    of times for the same task: `StorageBackend.delete()` (LocalFilesystemStorage) is
    idempotent on an already-missing key, so a delete that succeeds but whose status-commit
    then fails is correctly retried without erroring on the second attempt.

    A no-op if the task is already at a terminal-success status (`purged`/`retained_shared`)
    — callers (the immediate best-effort attempt, and app/worker.py's retry loop) may safely
    call this for any task regardless of its current status.

    Pass 28: on a `StorageError` (a real, presumed-transient-or-permanent I/O failure — the
    caller can't tell which from here), sets `next_attempt_at` to now plus an exponential,
    jittered backoff (`app.jobs.retry.compute_backoff_seconds`, the SAME pure policy function
    STEG 11's import-job retries already use, applied here to a different table) keyed off
    `attempt_count` — so `claim_storage_deletion_tasks()`'s worker-facing scan
    (`include_failed=True`) won't reclaim this task again until that backoff has elapsed,
    however many times it keeps failing. `completed_at`/`next_attempt_at` are both explicitly
    reset to `None` the moment a claim starts processing: a task reclaimed after a previous
    `failed` attempt must not still read as "completed" or "still backing off" while it's
    actively being retried right now."""
    if task.status in (StorageDeletionStatus.purged, StorageDeletionStatus.retained_shared):
        return

    acquire_storage_key_lock(db, task.storage_key)
    task.status = StorageDeletionStatus.processing
    task.attempt_count += 1
    task.completed_at = None
    task.next_attempt_at = None
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
            task.next_attempt_at = datetime.utcnow() + timedelta(
                seconds=compute_backoff_seconds(task.attempt_count, base=5.0, cap=3600.0)
            )
            logger.exception("Kunde inte radera blob %s fysiskt (task %s).", task.storage_key, task.id)

    db.add(task)
    db.commit()


def attempt_pending_storage_deletions_for_operation(operation_id: uuid.UUID) -> AccountErasureResult:
    """The immediate, best-effort attempt `erase_account_data()` makes right after its own DB
    phase commits — scoped ONLY to the tasks that SAME operation just created (never a broad,
    cross-operation scan; app/worker.py's retry loop owns that).

    Pass 27: takes NO session parameter — `storage_deletion_tasks` grants the ordinary
    request-scoped role no direct privileges at all (see module docstring), so this opens its
    own `_MaintenanceSession`, claims this operation's tasks atomically via
    `claim_storage_deletion_tasks()` (bounded batches, looped until nothing more is claimable),
    and closes that session again before returning — the caller's own request session is never
    touched by this function at all.

    Pass 28: claims with `include_failed=False`. The old default (`include_failed=True`) made
    this loop and `claim_storage_deletion_tasks()` jointly reintroduce a known bug class (the
    same one a prior backfill fix already closed elsewhere): a task that fails with a
    PERMANENT `StorageError` (e.g. a corrupt/unreadable path) got marked `failed` by
    `attempt_storage_deletion_task()`, and because `failed` was just as immediately claimable
    as `pending`, the very next iteration of this `while True` loop claimed and re-attempted
    the SAME task again — forever, for as long as the error stayed permanent, an unbounded
    busy loop inside a single HTTP request. `include_failed=False` means this call can only
    ever claim `pending` (or lease-expired `processing`) rows, so each task created by THIS
    operation is tried at most once here; anything that ends up `failed` is left entirely to
    app/worker.py's retry loop, which claims with the default `include_failed=True` and so
    correctly honors `next_attempt_at` backoff instead of retrying immediately.

    A failure attempting any individual task is caught, logged, and never re-raised: the
    account is already, durably erased by the time this runs, so a per-task failure here must
    never look like the erasure itself failed. Returns counts for observability/tests; callers
    that don't need them may ignore the return value."""
    result = AccountErasureResult(operation_id=operation_id)
    db = _MaintenanceSession()
    try:
        while True:
            claimed_ids = claim_storage_deletion_tasks(
                db, limit=100, operation_id=operation_id, include_failed=False
            )
            if not claimed_ids:
                break
            for task_id in claimed_ids:
                task = db.get(StorageDeletionTask, task_id)
                if task is None:
                    continue
                try:
                    attempt_storage_deletion_task(db, task)
                except Exception:
                    db.rollback()
                    logger.exception(
                        "Omedelbart raderingsförsök misslyckades för storage_deletion_task %s (operation %s) -- "
                        "kvarstår återförsökbar via worker-retryn.",
                        task_id,
                        operation_id,
                    )
                    continue
                if task.status == StorageDeletionStatus.purged:
                    result.storage_tasks_purged_immediately += 1
                elif task.status == StorageDeletionStatus.retained_shared:
                    result.storage_tasks_retained_shared_immediately += 1
                else:
                    result.storage_tasks_pending_or_failed.append(str(task_id))
    finally:
        db.close()
    return result


def erase_account_data(db: Session, user: User, *, client_ip: str | None = None) -> AccountErasureResult:
    """Permanent, irreversible account erasure — one atomic DB transaction (see module
    docstring), followed by a best-effort blob-deletion attempt that never rolls the DB phase
    back regardless of outcome. Password verification is the ROUTER's responsibility (see
    app/routers/account.py) — this function assumes the caller has already authenticated the
    request as genuinely coming from `user` themselves; it never re-checks a password.

    `client_ip` is a plain string, not a fastapi.Request — the router extracts it (same
    convention as app/storage/purge.py) so this domain-layer module never imports fastapi.
    """
    owner_id = user.id
    operation_id = uuid.uuid4()

    try:
        # Pass 28: acquires the owner-erasure advisory lock BEFORE taking any row-level lock
        # on `users` (below) — a real deadlock a founder-review-triggered full-suite run
        # surfaced (not merely theoretical): the OLD ordering took `FOR UPDATE` on the User
        # row FIRST, then acquired this advisory lock. A concurrent upload
        # (app/routers/library.py) that had already acquired the SAME advisory lock and was
        # mid-transaction inserting an ImportJob row could then block on a FOR KEY SHARE lock
        # Postgres itself takes on `users.id` to validate ImportJob.owner_id's foreign key —
        # while THIS transaction sat blocked waiting for that same advisory lock, held by the
        # very upload transaction now blocked on the row lock THIS transaction already held.
        # Classic deadlock (Postgres's own detector aborts one side with "deadlock detected").
        # Acquiring the advisory lock first removes the row-lock/advisory-lock cycle entirely:
        # by the time this transaction ever touches the `users` row below, it already holds
        # the SAME lock every other writer for this owner (uploads, claim_next_job) also takes
        # before touching anything, so no other transaction can be mid-flight needing that row.
        acquire_owner_erasure_lock(db, owner_id)

        # Serializes a concurrent SECOND erasure attempt for the SAME account — the second
        # call's own acquire_owner_erasure_lock above already blocks until the first either
        # commits or rolls back; by the time it proceeds to here, this FOR UPDATE either finds
        # the row gone (the first attempt committed — get_current_user's own earlier lookup for
        # that request would already have 401'd before ever reaching here on any request AFTER
        # the first commits) or unchanged (the first attempt rolled back, second proceeds
        # normally).
        locked_user = db.query(User).filter_by(id=owner_id).with_for_update().first()
        if locked_user is None:
            raise LookupError(f"user {owner_id} not found (already erased?)")

        # Pass 26: the owner-erasure lock acquired above (now BEFORE the User row lock, see
        # Pass 28's comment there) is what closes the erasure/upload race — a concurrent
        # upload either fully commits before this proceeds, and so is correctly swept into the
        # storage-key inventory below, or is still blocked and so cannot yet have written
        # anything this erasure would need to account for.

        # Pass 27: refuses to proceed while a worker is ACTIVELY, currently processing an
        # import job for this owner — see module docstring's blob-write-path audit for why a
        # lock alone can't close this the way it closes the upload-endpoint race (a running
        # import job's own worker keeps calling storage.write_stream() for each extracted
        # file, asynchronously, well past the moment its ImportJob row was first created).
        # Deleting knowledge_import_jobs/documents rows out from under that worker would risk
        # both an orphaned physical blob (bytes written, then the Document insert referencing
        # a since-deleted owner_id fails) and a confusing mid-job crash. `lease_expires_at`
        # (app/jobs/lease.py) is the SAME signal claim_next_job uses to decide a job is still
        # genuinely being worked on, not abandoned.
        running_job_exists = (
            db.query(ImportJob.id)
            .filter(
                ImportJob.owner_id == owner_id,
                ImportJob.status == ImportJobStatus.running,
                ImportJob.lease_expires_at.isnot(None),
                ImportJob.lease_expires_at > datetime.utcnow(),
            )
            .first()
            is not None
        )
        if running_job_exists:
            raise AccountErasureBlockedError(
                "En importkörning pågår fortfarande för det här kontot -- vänta tills den är klar (eller "
                "misslyckas) innan kontot kan raderas."
            )

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
        # Pass 28: `mainai_app` now has ZERO direct privileges on `storage_deletion_tasks` (see
        # module docstring) — a plain ORM `db.add(StorageDeletionTask(...))` would fail with
        # permission denied. The SECURITY DEFINER `enqueue_account_erasure_storage_task()`
        # (migration 0022) is the only way this session may create a task row: it re-derives
        # the caller from the SAME `app.current_user_id` session variable this transaction is
        # already running under, independently re-verifies each key still belongs to that
        # caller via `documents`/`knowledge_import_jobs` (never trusting the Python-side
        # `document_keys`/`import_job_keys` query above as authorization), and sets
        # `reason`/`status` itself.
        for key in storage_keys:
            db.execute(
                sa_text("SELECT enqueue_account_erasure_storage_task(:operation_id, :storage_key)"),
                {"operation_id": str(operation_id), "storage_key": key},
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

        # --- MainAI Runtime Truthfulness and Durable Job Foundation (migrations 0026/0027,
        # see docs/MAINAI_JOB_RUNTIME.md): mainai_job_events/mainai_job_proposals are
        # append-only at the DB level — mainai_app has no DELETE privilege on them at all,
        # only EXECUTE on this narrow SECURITY DEFINER function, which is the sole path that
        # ever removes those rows. erase_own_mainai_job_children() takes NO owner argument —
        # it derives the owner from this session's own app.current_user_id (already set to
        # owner_id for the duration of this request), so there is no parameter this call site
        # could ever get wrong or that a caller could point at a different user. Children
        # first (their own composite FK requires the parent mainai_jobs row to still exist),
        # THEN the parent below — never relying on the FK's ON DELETE CASCADE for this, same
        # explicit-delete convention as every other table in this function.
        db.execute(sa_text("SELECT erase_own_mainai_job_children()"))
        db.query(MainAIJob).filter_by(owner_id=owner_id).delete(synchronize_session=False)

        # --- Multi-Agent Work Coordination foundation (migration 0046, see
        # docs/LIFE_MULTI_AGENT_WORK_COORDINATION.md): agent_work_assignment_events is
        # append-only at the DB level, same "no DELETE privilege at all outside this one
        # narrow SECURITY DEFINER function" shape as mainai_job_events above. This single call
        # deletes the WHOLE owner-scoped coordination family (events, leases, dependency
        # edges, assignments, parallel-exploration groups) -- takes NO owner argument, same
        # "no parameter this call site could ever get wrong" reasoning as
        # erase_own_mainai_job_children() above. coordination_agents (the agent REGISTRY
        # itself) is deliberately untouched: it is founder-wide system knowledge, not this
        # owner's personal data, same as engineering_lessons.
        db.execute(sa_text("SELECT erase_own_agent_coordination_children()"))

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
    except AccountErasureBlockedError:
        # Not a failure -- a deliberate refusal, nothing was changed. Rolled back the same as
        # any other exception (a no-op here, since nothing was written before this check
        # runs), but logged as an ordinary, expected outcome rather than "misslyckades".
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("Kontoradering misslyckades för user_id=%s (operation=%s), återställd (rollback).", owner_id, operation_id)
        raise

    result = AccountErasureResult(operation_id=operation_id, storage_tasks_created=len(storage_keys))
    try:
        # Pass 27: runs entirely on its own privileged maintenance session now (see that
        # function's docstring) — the request-scoped `db` above is never touched again after
        # its own commit, so there is nothing on `db` left to roll back if this fails.
        immediate = attempt_pending_storage_deletions_for_operation(operation_id)
        result.storage_tasks_purged_immediately = immediate.storage_tasks_purged_immediately
        result.storage_tasks_retained_shared_immediately = immediate.storage_tasks_retained_shared_immediately
        result.storage_tasks_pending_or_failed = immediate.storage_tasks_pending_or_failed
    except Exception:
        # The erasure transaction above already committed successfully — that's the real,
        # durable outcome of this call. A failure making the immediate best-effort attempt
        # must never be raised as erase_account_data's own failure, and app/worker.py's retry
        # loop (see that module) can always finish the job later.
        logger.exception(
            "Omedelbart blob-raderingsförsök misslyckades helt för operation=%s efter en lyckad kontoradering -- "
            "kvarstår återförsökbart via worker-retryn.",
            operation_id,
        )

    return result
