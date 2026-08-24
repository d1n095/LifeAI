"""Canonical blob-reference policy (Pass 22, cross-owner-hardened in Pass 23): the ONE place
that decides whether a content-addressed storage_key is still needed by anything in the
system, and the ONE place that serializes a check-then-act sequence against it. Shared by
app/routers/library.py's upload-finalization path and app/storage/purge.py's
retry_source_blob_purge() (phase B).

Why this had to become a shared module rather than staying app/rag/library_import.py's
`maybe_purge_blob()`-local inline query: that query only ever looked at live
`Document.storage_key` rows. It never looked at `ImportJob.source_storage_key` -- the RAW
uploaded package (a single file or a ZIP) that POST /api/library/import streams straight to
durable storage BEFORE any Document row exists, which app/worker.py's poll loop later opens
itself to do the actual extraction/indexing (see app/rag/library_import.py's module
docstring). Content-addressing means a byte-identical raw upload and a byte-identical
already-imported Document share the exact same storage_key -- so an older document being
purged could physically delete a blob a newer, still-pending import was relying on to even
begin.

Which ImportJob statuses actually still need the raw blob is read directly off
app/worker.py's real resumption paths, not guessed at:
  - `pending`/`running` -- the ordinary in-flight path; run_import_job() (library_import.py)
    opens job.source_storage_key itself on every claim, including every retry.
  - `blocked` -- `_requeue_blocked_jobs` flips this back to `pending` in bulk once the active
    embedding provider verifies ok again; no re-upload.
  - `partial` with `blocked_count > 0` -- the exact same requeue query matches this too (see
    that function's own comment on the 2026-07-28 incident where a job rolled up to `partial`
    instead of `blocked`).
  - ANY status at all, including a fully terminal `completed`/`partial`/`failed` -- if the job
    still has a live (non-deleted) Document sitting in
    app.models.document.RESUMABLE_INDEX_STATUSES, `_reconcile_orphaned_documents` resets that
    job back to `pending` and resumption needs the SAME raw original again, with no re-upload.
    This is the one non-obvious case: a single ZIP job can produce many Documents, so purging
    ONE already-purged sibling document must never destroy the blob a DIFFERENT, still-stuck
    sibling document from the exact same job still needs.

A job that's `cancelled`, or `completed`/`partial`/`failed` with no remaining live Document in
a resumable status, matches none of the above -- its blob reference is correctly treated as
expired, exactly like an already-purged Document's is.

Pass 23, a blocking cross-owner gap a founder review caught: `documents` and
`knowledge_import_jobs` both have `FORCE ROW LEVEL SECURITY` (app/rls.py) with owner-scoped
policies. Content-addressed blob storage is GLOBAL -- two different owners' byte-identical
uploads share the exact same storage_key. `storage_key_still_referenced()` USED to run
ordinary ORM queries against those two tables directly, inside the calling owner's own
RLS-scoped session -- which structurally CANNOT see a different owner's live Document or
pending/running/blocked ImportJob referencing the same key. Owner A deleting a document could
therefore delete a blob owner B's still-pending import job depended on, with RLS itself being
the reason the danger was invisible to the very check meant to prevent it.

The fix is NOT `SET row_security = off` in this session -- that setting does not grant a
non-exempt role anything RLS would otherwise deny (see migration 0019's module docstring,
which already established this for the same reason). `storage_key_still_referenced()` now
delegates entirely to `storage_key_still_referenced_global(text) RETURNS boolean` (migration
0020) -- a narrow, SECURITY DEFINER SQL function owned by the admin/migration role (which
genuinely has BYPASSRLS, externally verified by apply_runtime_privileges.py, never assumed),
`SET search_path = pg_catalog`, every relation `public.`-qualified, granted EXECUTE ONLY to
mainai_app (never PUBLIC) via backend/scripts/security/s1a_privilege_policy.py. It implements the
EXACT SAME policy described above, just able to see every owner's rows -- and returns nothing
but a boolean: no owner id, job id, or document detail ever crosses back into the calling
(possibly cross-owner-relative-to-that-data) request. See that migration's module docstring
for the full incident writeup.

Locking: `acquire_storage_key_lock()` is a Postgres advisory lock scoped to the CALLING
session's current transaction (`pg_advisory_xact_lock` -- released automatically at that
transaction's next commit or rollback, never leaked across a crash the way a Redis lease with
a TTL could be). It exists to close a real TOCTOU race a founder review caught: POST
/api/library/import's storage.write_stream() call durably writes a blob to disk BEFORE any DB
row references it (content-addressing means the key isn't even known until the bytes are
fully hashed, so it's structurally impossible to lock the key any earlier than that). Without
a shared lock, a concurrent retry_source_blob_purge() could run its own reference check in
that exact window, see nothing pointing at the just-written key yet, and physically delete it
before the new ImportJob row is created -- leaving that row committed with a
source_storage_key pointing at nothing. Both call sites take the SAME lock (same hashed key)
before doing their own check-then-act sequence, so whichever side gets there first fully
commits or rolls back before the other's check can even run. Schema-qualified
(`pg_catalog.pg_advisory_xact_lock`/`pg_catalog.hashtextextended`) for the same reason every
relation reference inside a `SET search_path = pg_catalog` function is -- this call itself
runs on an ordinary mainai_app session, not inside a pinned-search_path function, but staying
schema-qualified here keeps the convention consistent and removes any dependency on the
calling session's own search_path ever including pg_catalog implicitly (it always does, but
never by an assumption this code relies on).
"""

import dataclasses
import enum
import io
import logging
import uuid
from datetime import datetime

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session, sessionmaker

from app.audit import record_audit
from app.db import migration_engine
from app.storage import StorageBackend, StorageError
from app.storage.base import StoredBlob

logger = logging.getLogger("mainai.storage.references")

# Pass 31: the SAME privileged admin/migration connection app/account/erasure.py's own
# `_MaintenanceSession` and app/worker.py's `_ClaimSession` already use for
# `storage_deletion_tasks` -- a second, independently-defined sessionmaker bound to the same
# engine (matching the established convention of NOT sharing one across modules, see
# erasure.py's own module-level comment on its `_MaintenanceSession`), never the
# ordinary request-scoped `mainai_app` session, which has ZERO direct privileges on this
# table for ANY reason value (see migration 0024's module docstring).
_MaintenanceSession = sessionmaker(bind=migration_engine)

# Pass 29: the canonical, hand-maintained registry of every table.column known to persist a
# value returned by `StorageBackend.write_stream()` (app/storage/) -- i.e. every place a live
# row can make a content-addressed physical blob unsafe to delete. `storage_key_still_
# referenced_global()` (migration 0020, extended by migration 0023 for the two Project Memory
# entries below) MUST check every single one of these; this constant exists so that claim is
# machine-checkable rather than a comment someone has to remember to re-read.
#
# A founder review found migration 0020 checked only the first two entries -- `project_sources`/
# `project_checkpoints` share the EXACT SAME storage backend (app/project_memory.py) but were
# invisible to the global reference check, so an account erasure that happened to share a
# content-addressed key with founder-wide Project Memory could physically delete a blob Project
# Memory still needed. See migration 0023's own module docstring for the full incident writeup.
#
# Adding a NEW persistent storage_key-shaped column anywhere in this codebase requires, in the
# SAME change:
#   1. Add the (table, column) pair here.
#   2. A new migration that CREATE OR REPLACEs storage_key_still_referenced_global() to also
#      check it (never edit an already-shipped migration in place).
#   3. A retention test proving a live row in the new table blocks physical deletion (see
#      tests/backend/storage/test_source_purge.py's Pass 29 section for the pattern), AND a matching
#      row in tests/backend/storage/test_source_purge.py's drift test that exercises THIS registry
#      against the real SQL function so a registry entry with no matching SQL coverage (or vice
#      versa) fails a test immediately instead of silently reopening this exact gap again.
KNOWN_STORAGE_KEY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("documents", "storage_key"),
    ("knowledge_import_jobs", "source_storage_key"),
    ("project_sources", "storage_key"),
    ("project_checkpoints", "brief_storage_key"),
)

# Pass 31: the companion registry to KNOWN_STORAGE_KEY_COLUMNS above -- every place in this
# codebase that calls (or references, e.g. as a higher-order callable passed to
# run_in_threadpool) `StorageBackend.write_stream()`, together with what protects it against
# the write-before-reference race (a live blob on disk with no DB row protecting it yet --
# see this module's own docstring and `store_content_with_reference_lock()`'s docstring for
# the concrete incidents this shape has already caused twice: the Life Library upload path in
# Pass 22, and Project Memory's three writers in Pass 31).
#
# A founder review's explicit point: KNOWN_STORAGE_KEY_COLUMNS only protects reference
# COLUMNS (what makes a key look "still needed"); it says nothing about DELETE call sites
# (that is what tests/backend/storage/test_source_purge.py's AST allowlist test covers) or about
# WRITE call sites -- this registry, and its own drift test
# (test_every_storage_write_stream_reference_is_on_the_known_write_path_registry), close that
# third gap: a new persistent writer added anywhere in the backend must be reviewed and
# entered here, in the SAME change, or the drift test fails immediately.
#
# Format: (relative file path under app/, enclosing function name, lock protocol + status).
KNOWN_STORAGE_WRITE_PATHS: tuple[tuple[str, str, str], ...] = (
    (
        "routers/library.py",
        "import_package",
        "acquire_owner_erasure_lock() held for the whole request BEFORE write_stream() runs; "
        "no storage-key lock needed around the ordinary accept path itself, since the fresh "
        "ImportJob row referencing the new key is created and committed in this SAME request "
        "-- acquire_storage_key_lock() instead protects the EMPTY-upload rejection branch "
        "specifically, via delete_if_unreferenced() below.",
    ),
    (
        "routers/documents.py",
        "upload_document",
        "Legacy /api/documents/upload thin adapter onto the Library durable path: "
        "acquire_owner_erasure_lock() before write_stream(); after the blob returns, "
        "acquire_storage_key_lock() then commit ImportJob(pending) + Document "
        "(storage_key/checksum/import_job_id) in the same request; retain_pending_* is called "
        "AFTER that commit -- never pre-commit, since retain commits on its own maintenance "
        "connection and would outlive a rolled-back reference (see retain_pending_rejected_"
        "upload_cleanup_tasks()'s own docstring). No BackgroundTasks. Verified by "
        "tests/backend/test_documents_upload_durable_delivery.py.",
    ),
    (
        "storage/references.py",
        "store_content_with_reference_lock",
        "acquire_storage_key_lock() taken AFTER write_stream() returns (structurally cannot "
        "be taken any earlier -- the key isn't known until the bytes are fully hashed), the "
        "blob is verified present under the lock, and republished from the same in-memory "
        "bytes if a concurrent deleter won the race; callers (ingest_doc()/ingest_system_"
        "map()/create_checkpoint() in app/project_memory.py) commit their DB row while still "
        "holding this same lock. Verified by tests/backend/test_project_memory.py's Pass 31 "
        "section.",
    ),
    (
        "rag/library_import.py",
        "_store_bytes",
        "Pass 32 (a seventh founder review round): FIXED -- was a known, PRE-EXISTING, "
        "documented gap (flagged, not fixed, in Pass 27's own blob-write-path audit and again "
        "in Pass 31's registry). The worker's per-file extraction write (app/worker.py's poll "
        "loop, processing an ALREADY-CLAIMED ImportJob) now goes through "
        "_store_bytes_with_reference_lock() (same file), which wraps this exact write in the "
        "same acquire_storage_key_lock()+verify+republish protocol store_content_with_"
        "reference_lock() already provides for Project Memory -- the caller (_import_one_file) "
        "sets Document.storage_key and commits while still holding that lock. Verified by "
        "tests/backend/rag/test_library_import.py's Pass 32 section.",
    ),
)


def acquire_storage_key_lock(db: Session, storage_key: str) -> None:
    """Blocks until this session holds the advisory lock for `storage_key`, released
    automatically when the CURRENT transaction commits or rolls back -- never call this and
    then hold the session open indefinitely without committing/rolling back soon after, or a
    concurrent holder blocks for that entire time. hashtextextended's 64-bit output makes an
    accidental collision between two different storage_keys astronomically unlikely; even if
    one ever happened, the effect is only unnecessary serialization between two unrelated
    keys, never an incorrect result. Seed `0` -- see acquire_owner_erasure_lock's seed `1` for
    why the two locks deliberately live in separate hash namespaces."""
    db.execute(
        sa_text("SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:key, 0))"),
        {"key": storage_key},
    )


def acquire_owner_erasure_lock(db: Session, owner_id: uuid.UUID) -> None:
    """Account erasure (app/account/erasure.py), Pass 26: a transaction-scoped advisory
    lock scoped to ONE owner, closing a real TOCTOU race a founder review caught between
    account erasure and a concurrent upload for the SAME owner. `POST /api/library/import`
    (app/routers/library.py) durably writes a blob to disk via `storage.write_stream()`
    BEFORE any database row references it -- exactly the same "bytes exist before any DB row
    can protect them" shape `acquire_storage_key_lock` above already closes for uploads vs.
    purges, except here the race is against account erasure's OWN storage-key inventory
    (`storage_deletion_tasks`, migration 0021), which only ever runs once, at erasure time,
    and can never re-scan a blob written after it already finished.

    Both call sites take this SAME lock (same hashed owner_id) before doing their own
    check-then-act sequence:
      - Account erasure acquires it FIRST, before inventorying `Document.storage_key`/
        `ImportJob.source_storage_key` for this owner -- so a concurrent upload either fully
        commits (and its fresh ImportJob row is correctly swept into the inventory) or is
        still blocked (and so cannot yet have written anything a rolled-back erasure would
        need to account for) before erasure's own inventory query ever runs.
      - The upload endpoint acquires it as the FIRST thing it does, before
        `storage.write_stream()` even starts (never after) -- if erasure already committed
        and holds this owner_id's row gone, the upload path re-verifies the owner still
        exists (see app/routers/library.py) and fails closed BEFORE writing any bytes at all,
        rather than writing an orphaned blob and finding out only when the later `ImportJob`
        insert hits the now-dangling `owner_id` foreign key.

    Seed `1` (distinct from acquire_storage_key_lock's seed `0` above): hashtextextended's
    seed parameter puts the two lock kinds in separate hash namespaces, so a storage_key that
    happens to collide numerically with some owner_id's UUID string can never make these two,
    semantically unrelated locks contend with each other."""
    db.execute(
        sa_text("SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:key, 1))"),
        {"key": str(owner_id)},
    )


def storage_key_still_referenced(db: Session, storage_key: str) -> bool:
    """True if `storage_key` is still needed by ANYTHING, across ALL owners -- a live
    Document.storage_key row, or an ImportJob.source_storage_key the worker could still read
    from (see module docstring). Delegates entirely to the database's own
    storage_key_still_referenced_global() (migration 0020, SECURITY DEFINER) rather than
    querying documents/knowledge_import_jobs directly -- this session's own RLS scope can only
    ever see the calling owner's rows, which is exactly the gap that function exists to close.
    Callers must hold acquire_storage_key_lock(db, storage_key) for the duration of their own
    check-then-act sequence around this; this function itself does no locking."""
    result = db.execute(
        sa_text("SELECT public.storage_key_still_referenced_global(:key)"),
        {"key": storage_key},
    ).scalar()
    return bool(result)


class DeleteIfUnreferencedOutcome(str, enum.Enum):
    """Result of `delete_if_unreferenced()` below."""

    retained = "retained"  # still referenced elsewhere -- correctly NOT deleted
    purged = "purged"  # not referenced anywhere -- physically deleted
    # Pass 31: not referenced, the physical delete raised StorageError, AND a durable
    # `storage_deletion_tasks` row now exists to retry it (see
    # `enqueue_rejected_upload_cleanup_task()` below) -- the normal, expected shape of a
    # transient I/O failure.
    failed_queued_for_retry = "failed_queued_for_retry"
    # Pass 31: not referenced, the physical delete raised StorageError, AND creating the
    # durable retry task ITSELF also failed -- the blob is a genuine, invisible orphan on disk
    # until a human/automated disk-usage sweep notices it. Logged at ERROR either way; this
    # outcome exists so callers/tests can distinguish "will self-heal via the worker" from
    # "needs manual follow-up right now", not so any caller currently branches on it.
    failed_not_queued = "failed_not_queued"


def retain_pending_rejected_upload_cleanup_tasks(storage_key: str) -> int:
    """Pass 33 + runtime clock sweep: supersede stale `rejected_upload_cleanup` tasks AFTER a
    durable DB reference to `storage_key` is committed.

    Those tasks are enqueued when a blob correctly appeared unreferenced (e.g. an empty upload
    rejected before any Document/ImportJob row existed). A concurrent writer can legitimately
    need the SAME key again via `store_content_with_reference_lock()` /
    `_store_bytes_with_reference_lock()`. The advisory lock held during write→reference commit
    closes the physical-delete TOCTOU until that commit; `storage_key_still_referenced()` then
    protects the blob. Calling retain *before* the reference exists (the old shape) marked
    cleanup tasks `retained_shared` even if the writer crashed mid-transaction — leaving an
    unreferenced blob that cleanup would never delete.

    Call AFTER the writer's Document / ProjectSource / ProjectCheckpoint / ImportJob reference
    row is committed. Safe if cleanup already ran: reference check wins; retain is a no-op for
    terminal rows. Runs on `_MaintenanceSession` (mainai_app has zero direct privileges on
    `storage_deletion_tasks`)."""
    db = _MaintenanceSession()
    try:
        result = db.execute(
            sa_text("""
                UPDATE storage_deletion_tasks
                SET status = 'retained_shared',
                    last_error = NULL,
                    completed_at = now(),
                    updated_at = now(),
                    next_attempt_at = NULL
                WHERE storage_key = :storage_key
                  AND reason = 'rejected_upload_cleanup'
                  AND status IN ('pending', 'processing', 'failed')
            """),
            {"storage_key": storage_key},
        )
        db.commit()
        return int(result.rowcount or 0)
    finally:
        db.close()


def enqueue_rejected_upload_cleanup_task(storage_key: str) -> uuid.UUID:
    """Pass 31 (a sixth founder review round): creates a durable `storage_deletion_tasks` row
    (`reason='rejected_upload_cleanup'`) so `app/worker.py`'s existing retry loop -- the SAME
    one that already retries a failed account-erasure blob delete -- picks this key up and
    keeps retrying it with the same backoff/lease machinery, instead of a `StorageError`
    during `delete_if_unreferenced()`'s cleanup silently vanishing into a log line forever.

    MUST be called ONLY from `delete_if_unreferenced()`'s own `StorageError` handler, while
    that caller's `db` session STILL holds `acquire_storage_key_lock(db, storage_key)` for
    this exact key (it has not committed or rolled back yet). This function deliberately does
    NO locking of its own -- taking `acquire_storage_key_lock` again here, on the DIFFERENT
    connection this function uses (`_MaintenanceSession`, below), would try to acquire a lock
    the CALLING session already holds and is still blocked inside this very call stack, i.e. a
    guaranteed self-deadlock (this connection would block forever waiting for a lock the
    caller cannot release until this call returns). Correctness instead relies entirely on the
    caller's own already-held lock: ANY other concurrent attempt to reach this function for
    the SAME storage_key (e.g. a second, simultaneous rejected empty upload sharing the same
    empty-content key) is necessarily still blocked at ITS OWN `acquire_storage_key_lock` call,
    on ITS OWN session, until this transaction ends -- so two concurrent callers can never both
    be inside this function for the same key at the same time.

    Unlike `enqueue_account_erasure_storage_task()` (migration 0022's SECURITY DEFINER
    function), this is NOT exposed to the ordinary, request-scoped `mainai_app` role at all --
    see migration 0024's module docstring for why an ownership-verifying SQL function has no
    equivalent here (a rejected upload was, by design, never given a Document/ImportJob row to
    verify ownership against). Runs entirely on the privileged admin/migration connection
    (`_MaintenanceSession`), the same "backend code on a maintenance connection" shape
    `attempt_pending_storage_deletions_for_operation()` already uses -- `mainai_app` continues
    to get ZERO direct privileges on `storage_deletion_tasks`, for any reason value.

    Idempotent per outstanding cleanup, not per call: if a task for this exact `storage_key`
    with `reason='rejected_upload_cleanup'` already exists in a non-terminal-success status
    (`pending`/`processing`/`failed`), its `operation_id` is returned unchanged and no new row
    is inserted -- repeated failures for the SAME still-empty-content key (a realistic
    scenario: content-addressing means every empty upload from any founder shares the exact
    same key) must not pile up an unbounded number of redundant retry tasks for one physical
    file. A NEW row is created only once the previous one has reached a terminal outcome
    (`purged`/`retained_shared`)."""
    db = _MaintenanceSession()
    try:
        existing = db.execute(
            sa_text("""
                SELECT operation_id FROM storage_deletion_tasks
                WHERE storage_key = :storage_key
                  AND reason = 'rejected_upload_cleanup'
                  AND status IN ('pending', 'processing', 'failed')
                LIMIT 1
            """),
            {"storage_key": storage_key},
        ).first()
        if existing is not None:
            return uuid.UUID(str(existing[0]))

        operation_id = uuid.uuid4()
        db.execute(
            sa_text("""
                INSERT INTO storage_deletion_tasks (id, operation_id, storage_key, reason, status)
                VALUES (gen_random_uuid(), :operation_id, :storage_key, 'rejected_upload_cleanup', 'pending')
            """),
            {"operation_id": str(operation_id), "storage_key": storage_key},
        )
        db.commit()
        return operation_id
    finally:
        db.close()


def _record_storage_orphan_risk_audit(storage_key: str, *, detail: str) -> None:
    """Pass 32 (a seventh founder review round): `failed_not_queued` means BOTH the physical
    delete AND creating a durable retry task failed -- the blob is a genuine, invisible orphan
    on disk that nothing else in this codebase will ever look for again on its own. The
    founder's explicit requirement: this degraded state must never be silently folded into an
    ordinary log line with zero operational signal -- founder ops status must be able to see
    that a blob may need a manual storage sweep.

    Writes on a FRESH `_MaintenanceSession`, never the caller's own `db` session: at least one
    real caller (`app/routers/library.py`'s empty-upload rejection) rolls its own session back
    immediately after `delete_if_unreferenced()` returns (there is no DB row to commit for a
    rejected, never-persisted upload) -- adding this audit row to that SAME session would get
    silently rolled back along with it. Uses its own independent commit instead, exactly like
    `enqueue_rejected_upload_cleanup_task()` above, so this record survives regardless of what
    the caller's own transaction does next.

    Never raises -- a failure recording an audit trail entry about an already-degraded state
    must not itself become a second failure the caller has to handle; logged at CRITICAL either
    way so this is never silently lost even if the audit write itself fails. `user_id=None`:
    this is a system-detected condition, not any specific founder's action."""
    db = _MaintenanceSession()
    try:
        record_audit(
            db,
            user_id=None,
            action="storage_orphan_risk",
            entity_type="storage_key",
            # AuditLog.entity_id is varchar(64); a full storage_key ("{2 hex}/{64 hex}") is 67
            # characters, so this strips the two-hex-char shard prefix and keeps only the bare
            # sha256 (exactly 64 characters) -- the full storage_key is still in `detail`.
            entity_id=storage_key.rsplit("/", 1)[-1],
            detail=detail,
        )
    except Exception:
        logger.critical(
            "Kunde inte heller skriva en audit-rad för orphan-risk-blob %s -- kräver manuell "
            "diskuppföljning baserat på loggarna ovan.",
            storage_key,
            exc_info=True,
        )
    finally:
        db.close()


def delete_if_unreferenced(db: Session, storage: StorageBackend, storage_key: str) -> DeleteIfUnreferencedOutcome:
    """Pass 30 (a fourth founder review round): the ONE canonical, self-contained
    check-then-act sequence for physically deleting a content-addressed blob that has NOT YET
    been recorded in any durable DB row -- e.g. a just-written upload discovered to be
    rejectable (empty) before any Document/ImportJob is ever created for it.

    The bug this closes: `app/routers/library.py`'s empty-upload rejection used to call
    `storage.delete(blob.storage_key)` directly, completely bypassing BOTH
    `acquire_storage_key_lock()` and `storage_key_still_referenced()` -- migration 0023's
    newly-widened global reference check (Pass 29, covering `project_sources`/
    `project_checkpoints` too) can only ever protect a delete that actually goes THROUGH the
    canonical protocol; it cannot protect a delete call that skips it entirely. Because storage
    is content-addressed, EVERY empty upload hashes to the exact same `storage_key` -- so an
    unrelated, already-live reference to that same (empty-content) key, in ANY domain this
    codebase knows about (a Document, an ImportJob, a ProjectSource, a ProjectCheckpoint),
    could have been physically deleted by a completely unrelated founder's own rejected empty
    upload. This is the exact real, physical, cross-domain data-loss shape migration 0023
    itself was written to close -- just reachable through a second, ungated code path that
    happened to exist in the same PR.

    Distinct from `app/rag/library_import.py::maybe_purge_blob()`: that function assumes the
    CALLER already holds `acquire_storage_key_lock()` for the duration of a LARGER surrounding
    transaction (e.g. deleting an existing `Document` row and persisting its `deletion_status`
    in the same transaction) -- it deliberately does no locking itself. This function has no
    such surrounding transaction to piggyback on (there is no row yet, nothing to persist a
    status onto), so it owns the whole lock-acquire + reference-check + delete sequence itself,
    self-contained. Callers get back an outcome instead of a bare boolean/exception so they can
    log/report distinctly without this function importing any HTTP-layer concern.

    Pass 31 (a sixth founder review round): a genuine `StorageError` deleting an
    ALREADY-CONFIRMED-UNREFERENCED key used to be logged and forgotten -- "en loggrad är inte
    en beständig cleanup-plan" -- leaving a physically orphaned, uninventoried blob on disk
    with no automated way to ever find or retry it. Now enqueues a durable
    `storage_deletion_tasks` row (`enqueue_rejected_upload_cleanup_task()` above,
    `reason='rejected_upload_cleanup'`) so `app/worker.py`'s existing retry loop picks it up
    and keeps retrying with the same backoff/lease/global-reference-check machinery an
    account-erasure blob delete already gets. Still logged at ERROR (`logger.exception`)
    either way, for immediate visibility regardless of whether the durable task itself could
    be created.

    Pass 32 (a seventh founder review round): the founder's explicit point -- "DB-durability
    kan inte alltid garanteras, men detta ska aldrig tystas till en vanlig 400 med noll
    operationell signal" -- means `failed_not_queued` specifically (both the physical delete
    AND the durable-retry-task insert failed) is no longer just an ERROR-level log line. It now
    also logs at CRITICAL and writes a durable `AuditLog` row (`_record_storage_orphan_risk_
    audit()` below) so founder ops status can surface "this blob may need a manual storage
    sweep" instead of the failure vanishing once the log rotates. `failed_queued_for_retry`
    deliberately keeps the ORIGINAL ERROR-level treatment unchanged -- that outcome already has
    a durable `storage_deletion_tasks` row tracking it and will self-heal via app/worker.py's
    retry loop, so it is not the degraded, unmonitored state this upgrade targets. This does NOT
    add a second local outbox or a deterministic orphan-inventory/sweep mechanism -- only makes
    the existing degraded state visible; a future sweep mechanism is still future work."""
    acquire_storage_key_lock(db, storage_key)
    if storage_key_still_referenced(db, storage_key):
        return DeleteIfUnreferencedOutcome.retained
    try:
        storage.delete(storage_key)
        return DeleteIfUnreferencedOutcome.purged
    except StorageError:
        logger.exception("Kunde inte radera en obekräftad, orefererad blob %s fysiskt.", storage_key)
        try:
            enqueue_rejected_upload_cleanup_task(storage_key)
            return DeleteIfUnreferencedOutcome.failed_queued_for_retry
        except Exception:
            logger.critical(
                "Kunde inte heller skapa en durable rejected_upload_cleanup-task för blob %s -- "
                "detta är INTE en täckt, självläkande degradering: ingen kod i systemet kommer "
                "att försöka radera denna blob automatiskt igen. Kräver manuell/automatiserad "
                "diskuppföljning.",
                storage_key,
                exc_info=True,
            )
            _record_storage_orphan_risk_audit(
                storage_key,
                detail=(
                    f"Fysisk radering OCH den durable rejected_upload_cleanup-tasken misslyckades "
                    f"båda för storage_key={storage_key}. Blobben är en overifierad, potentiellt "
                    f"orefererad fil som ingen automatiserad process längre känner till -- kräver "
                    f"manuell diskuppföljning/sweep."
                ),
            )
            return DeleteIfUnreferencedOutcome.failed_not_queued


def store_content_with_reference_lock(
    db: Session, storage: StorageBackend, content: bytes, *, max_bytes: int
) -> StoredBlob:
    """Pass 31 (a sixth founder review round): writes `content` durably via `storage.write_
    stream()`, then -- BEFORE returning control to the caller to create/commit its own DB
    row -- acquires `acquire_storage_key_lock()` for the resulting key and verifies the blob
    is still actually there. Closes the exact same write-before-reference race Pass 22 already
    closed for the Life Library upload path (see `acquire_storage_key_lock`'s own docstring),
    just for a second class of writer that never took that lock: `app/project_memory.py`'s
    `ingest_doc()`/`ingest_system_map()`/`create_checkpoint()` all write through this SAME
    content-addressed storage backend but used to commit their `ProjectSource`/
    `ProjectCheckpoint` row with no lock at all between the physical write and that commit.

    The race this closes: writer A calls `storage.write_stream()`, getting back key K, but has
    not yet committed a DB row referencing K. A concurrent deleter B (source purge, account
    erasure, or `delete_if_unreferenced()` itself) takes `acquire_storage_key_lock` for K,
    finds -- correctly, at that moment -- nothing anywhere references K yet, and physically
    deletes it. A now commits its `ProjectSource`/`ProjectCheckpoint` row pointing at K,
    completing with a live DB reference to a blob that no longer exists on disk.

    Callers MUST create and commit their DB row referencing the returned blob's `storage_key`
    while STILL holding the SAME `db` session's open transaction this function ran on -- the
    lock releases at that session's next commit/rollback, exactly like every other caller of
    `acquire_storage_key_lock`.

    If the blob has vanished OR is corrupt by the time the lock is acquired (the deleter above
    won the race, or something replaced the file at that path with the wrong bytes),
    republishes it from the SAME in-memory `content` bytes this call already has --
    `write_stream()` is naturally idempotent for identical content (same hash -> same key), so
    a second call safely reproduces the identical blob. If it's STILL missing or still corrupt
    immediately after that republish attempt (an even rarer, pathological repeat-loss), raises
    `StorageError` rather than letting the caller commit a DB row that might reference nothing
    or the wrong bytes -- fail closed, never a silent dangling or corrupt reference.

    Pass 32 (an eighth founder review round): uses `storage.verify(expected_sha256=...,
    expected_size=...)` here, not `storage.exists()` -- a founder review correctly pointed out
    that `exists()` alone can't tell a genuinely-published blob apart from a same-path file
    whose bytes don't actually match this content's hash (disk corruption, or a manual
    out-of-band edit at that content-addressed path). `write_stream()`'s own dedup path
    (`LocalFilesystemStorage._publish()`) now repairs that case itself when it can, but this
    caller-side check is the second, independent line of defense: even if the storage backend
    somehow returned a `StoredBlob` for content that isn't ACTUALLY on disk correctly, this
    function must not commit a DB reference to it."""
    reader = io.BytesIO(content)
    blob = storage.write_stream(lambda: reader.read(1 << 20), max_bytes=max_bytes)

    acquire_storage_key_lock(db, blob.storage_key)
    if storage.verify(blob.storage_key, expected_sha256=blob.sha256, expected_size=blob.size_bytes):
        return blob

    # Lost the race, or the blob at this key is corrupt: either something else's reference
    # check ran (correctly, since our row didn't exist yet) and physically deleted this key
    # before we got here, or the bytes now at this path don't match its own name. Safe to
    # republish now, WHILE holding the lock, so no concurrent deleter's reference check can
    # run again until this transaction commits or rolls back.
    logger.warning(
        "Blob %s saknas eller är korrupt vid referens-låset -- återpublicerar från minnet.", blob.storage_key
    )
    reader = io.BytesIO(content)
    blob = storage.write_stream(lambda: reader.read(1 << 20), max_bytes=max_bytes)
    if not storage.verify(blob.storage_key, expected_sha256=blob.sha256, expected_size=blob.size_bytes):
        raise StorageError(
            f"Kunde inte publicera en verifierad blob {blob.storage_key} -- innehållet matchar "
            f"fortfarande inte den förväntade sha256:n efter återpublicering."
        )
    return blob


@dataclasses.dataclass(frozen=True)
class StorageCleanupOpsStatus:
    """Pass 32 (an eighth founder review round): aggregated-only view of the storage cleanup
    degradation state, for founder ops-status (app/routers/library.py's `ops_status()`) --
    see `get_storage_cleanup_ops_status()`'s own docstring for the exact policy. NEVER carries
    a raw storage_key or any other identifying detail -- only counts and timestamps, matching
    `OpsStatusOut`'s existing "aggregated counts/booleans/timestamps only" contract."""

    storage_cleanup_degraded: bool
    storage_orphan_risk_count: int
    latest_storage_orphan_risk_at: datetime | None
    pending_storage_cleanup_tasks: int
    failed_storage_cleanup_tasks: int
    oldest_failed_storage_cleanup_age_seconds: float | None


def get_storage_cleanup_ops_status(db: Session) -> StorageCleanupOpsStatus:
    """Pass 32 (an eighth founder review round): the founder's explicit point -- writing
    `AuditLog(action='storage_orphan_risk')` (see `_record_storage_orphan_risk_audit()` above)
    is not, on its own, "founder ops-status can see this" -- until this function existed,
    nothing ever read those rows back. This is the one place that does, aggregated into counts
    a founder-only endpoint can safely expose.

    Two different privilege routes, because the two tables involved have different grants:
      - `audit_log` is readable directly on the caller's own ordinary `db` session (mainai_app
        has ordinary SELECT there already -- see e.g. app/routers/auth.py's own direct
        AuditLog query for login-failure rate limiting; audit_log was never narrowed the way
        storage_deletion_tasks was).
      - `storage_deletion_tasks` is NOT (Pass 27/28: mainai_app has ZERO direct privileges on
        it, for any reason -- see backend/scripts/security/s1a_privilege_policy.py). Reads it via the
        SAME privileged `_MaintenanceSession` this module already uses for durable-task writes.

    Degradation policy (documented here since there is no UI/API to acknowledge or resolve
    this yet -- deliberately out of scope for this pass, see the founder's own "kvitterar/
    resolverar" instruction):
      - `storage_orphan_risk_count`/`latest_storage_orphan_risk_at` reflect EVERY
        `storage_orphan_risk` audit row that has ever existed -- `audit_log` is an immutable,
        append-only trail (see that model's own docstring) with no resolution/acknowledgment
        column, so there is currently no way to mark one of these as "handled". Any occurrence
        at all makes `storage_cleanup_degraded` permanently True until a FUTURE deterministic
        orphan-inventory/sweep mechanism (not built in this pass) adds a real resolution
        marker -- a deliberate fail-towards-visibility choice, not an oversight.
      - `pending_storage_cleanup_tasks` (status IN ('pending', 'processing')) does NOT by
        itself drive `storage_cleanup_degraded` -- a freshly-enqueued durable retry task
        waiting for the worker's next poll cycle is completely normal, expected, self-healing
        operation (see `delete_if_unreferenced()`'s own docstring: "the normal, expected shape
        of a transient I/O failure"), not a degradation.
      - `failed_storage_cleanup_tasks` (status = 'failed') DOES drive `storage_cleanup_degraded`
        -- unlike the audit-log case, this one IS genuinely self-resolving: once
        `app/worker.py`'s retry loop (via `attempt_storage_deletion_task()`) succeeds for that
        task, its status moves to `purged`/`retained_shared` and it stops being counted here,
        so `storage_cleanup_degraded` can return to False on its own once nothing else is
        contributing to it."""
    audit_row = db.execute(
        sa_text("SELECT count(*), max(created_at) FROM audit_log WHERE action = 'storage_orphan_risk'")
    ).first()
    storage_orphan_risk_count = int(audit_row[0]) if audit_row else 0
    latest_storage_orphan_risk_at = audit_row[1] if audit_row else None

    task_session = _MaintenanceSession()
    try:
        pending_storage_cleanup_tasks = int(
            task_session.execute(
                sa_text("SELECT count(*) FROM storage_deletion_tasks WHERE status IN ('pending', 'processing')")
            ).scalar()
            or 0
        )
        failed_row = task_session.execute(
            sa_text(
                "SELECT count(*), extract(epoch FROM (now() - min(created_at))) "
                "FROM storage_deletion_tasks WHERE status = 'failed'"
            )
        ).first()
    finally:
        task_session.close()

    failed_storage_cleanup_tasks = int(failed_row[0]) if failed_row else 0
    oldest_failed_storage_cleanup_age_seconds = (
        float(failed_row[1]) if failed_row and failed_row[1] is not None else None
    )

    return StorageCleanupOpsStatus(
        storage_cleanup_degraded=storage_orphan_risk_count > 0 or failed_storage_cleanup_tasks > 0,
        storage_orphan_risk_count=storage_orphan_risk_count,
        latest_storage_orphan_risk_at=latest_storage_orphan_risk_at,
        pending_storage_cleanup_tasks=pending_storage_cleanup_tasks,
        failed_storage_cleanup_tasks=failed_storage_cleanup_tasks,
        oldest_failed_storage_cleanup_age_seconds=oldest_failed_storage_cleanup_age_seconds,
    )
