"""Canonical blob-reference policy (Pass 22, cross-owner-hardened in Pass 23): the ONE place
that decides whether a content-addressed storage_key is still needed by anything in the
system, and the ONE place that serializes a check-then-act sequence against it. Shared by
app/routers/library.py's upload-finalization path and app/rag/source_purge.py's
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
mainai_app (never PUBLIC) via backend/scripts/s1a_privilege_policy.py. It implements the
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

import uuid

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

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
#      tests/backend/test_source_purge.py's Pass 29 section for the pattern), AND a matching
#      row in tests/backend/test_source_purge.py's drift test that exercises THIS registry
#      against the real SQL function so a registry entry with no matching SQL coverage (or vice
#      versa) fails a test immediately instead of silently reopening this exact gap again.
KNOWN_STORAGE_KEY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("documents", "storage_key"),
    ("knowledge_import_jobs", "source_storage_key"),
    ("project_sources", "storage_key"),
    ("project_checkpoints", "brief_storage_key"),
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
    """Account erasure (app/rag/account_erasure.py), Pass 26: a transaction-scoped advisory
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
