"""Pass 29 (PR #31, third founder review round of the account-erasure slice): extends
`public.storage_key_still_referenced_global(text)` (migration 0020) to also see
`project_sources.storage_key` and `project_checkpoints.brief_storage_key` — closing a real
cross-domain orphan-blob gap a founder review found.

The bug this closes: `app/storage/local_fs.py`'s storage backend is content-addressed and
GLOBAL — the storage_key is a hash of the bytes, so two completely unrelated rows in two
completely unrelated tables end up with the identical key the moment they happen to store
byte-identical content, by design (the exact same property migration 0020's own docstring
already established for `documents` vs `knowledge_import_jobs` across DIFFERENT OWNERS).
Migration 0020's function only ever checked `documents`/`knowledge_import_jobs` — but
`app/project_memory.py`'s founder-wide project-memory system (`ProjectSource.storage_key`,
`ProjectCheckpoint.brief_storage_key`, see `app/models/project_memory.py`) writes through the
SAME `get_storage()`/`write_stream()` backend to store doc-ingestion content and resumption
briefs. The concrete scenario:

  1. Project Memory ingests some content X -> storage_key = hash(X), stored in
     `project_sources.storage_key`.
  2. A founder later uploads byte-identical content X via the ordinary Life Library upload
     path -> the SAME storage_key, now also referenced by a `Document`/`ImportJob` row.
  3. The founder's account is erased. `enqueue_account_erasure_storage_task()` (migration
     0022) correctly accepts the key -- the founder genuinely does own that Document/ImportJob.
  4. The Document/ImportJob rows are deleted as part of the erasure.
  5. The maintenance worker's `storage_key_still_referenced_global(key)` check runs: it has
     no idea `project_sources` exists, sees no live Document/ImportJob, and returns false.
  6. The physical blob is deleted -- taking Project Memory's still-live reference down with it.
     `project_checkpoints.brief_storage_key` is NOT NULL (see that model's docstring): a
     checkpoint whose brief blob vanishes underneath it is a permanently broken row, not a
     recoverable one.

This is real, physical, cross-domain data loss caused by the SAME content-addressing property
this codebase already had to defend against once (cross-owner, migration 0020) -- just not yet
defended against across DIFFERENT DATA DOMAINS (per-account library data vs. founder-wide
project memory). `enqueue_account_erasure_storage_task()`'s per-caller ownership check
(migration 0022) is the wrong layer to fix this at: it correctly verifies the ENQUEUING
request is legitimate (the caller really does own SOME Document/ImportJob with this key) --
the gap is entirely in the WORKER's later, separate decision about whether the key is safe to
physically delete, which must account for every live reference across the whole storage
domain, not just the two tables that happened to exist when migration 0020 was written.

CREATE OR REPLACE, not a new function -- same exact signature (`text -> boolean`), same
`SECURITY DEFINER`, same `SET search_path = pg_catalog`, same `REVOKE ALL FROM PUBLIC` (EXECUTE
for `mainai_app` is unaffected -- backend/scripts/s1a_privilege_policy.py's existing
`storage_key_still_referenced_global` entry already covers this signature, nothing to change
there). The `documents`/`knowledge_import_jobs` branches are copied verbatim from migration
0020 -- this migration adds two new OR-branches, it does not change the existing logic at all
(the Pass 24 status-drift regression tests in tests/backend/test_source_purge.py, which
exhaustively compare this function's behavior against RESUMABLE_INDEX_STATUSES/
import_job_still_needs_raw_blob() for every enum value, are expected to keep passing
unmodified).

`project_sources.storage_key` is nullable (a `git_commit`/`github_branch`/`github_pr` source
may have no blob at all, only `raw_data`) -- `p_storage_key IS NULL` is already handled at the
top of the function (returns false immediately), so no extra NULL-guard is needed for this
branch beyond the existing one. `project_checkpoints.brief_storage_key` is NOT NULL, so every
checkpoint row always has a real key to compare against.

See `docs/BRANCH_REGISTRY.md`'s Pass 29 entry for the full storage-domain inventory this
migration is based on (every `storage_key`-shaped column and every `get_storage()`/
`write_stream()`/`.delete()` call site in the backend, not just the two tables fixed here).
"""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

_UPGRADE_SQL = """
    CREATE OR REPLACE FUNCTION public.storage_key_still_referenced_global(p_storage_key text)
    RETURNS boolean
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
    AS $$
    BEGIN
        IF p_storage_key IS NULL THEN
            RETURN false;
        END IF;

        -- 1. Any live Document, across ALL owners, still pointing at this key.
        IF EXISTS (
            SELECT 1 FROM public.documents
            WHERE storage_key = p_storage_key AND deleted_at IS NULL
        ) THEN
            RETURN true;
        END IF;

        -- 2. Any ImportJob, across ALL owners, whose raw upload the worker could still
        -- read from -- see migration 0020's own comment for the full policy this mirrors
        -- verbatim (app/models/import_job.py's import_job_still_needs_raw_blob(),
        -- cross-checked by tests/backend/test_source_purge.py's status-drift tests).
        IF EXISTS (
            SELECT 1 FROM public.knowledge_import_jobs j
            WHERE j.source_storage_key = p_storage_key
              AND (
                  j.status IN ('pending', 'running', 'blocked')
                  OR (j.status = 'partial' AND j.blocked_count > 0)
                  OR EXISTS (
                      SELECT 1 FROM public.documents sib
                      WHERE sib.import_job_id = j.id
                        AND sib.deleted_at IS NULL
                        AND sib.status::text IN (
                            'pending', 'received', 'original_storing', 'original_stored',
                            'extracting', 'extracted', 'awaiting_classification',
                            'classifying', 'embedding', 'indexing'
                        )
                  )
              )
        ) THEN
            RETURN true;
        END IF;

        -- 3. Pass 29: any ProjectSource (founder-wide project memory, app/project_memory.py)
        -- still pointing at this key -- ingested doc content stored via the SAME
        -- content-addressed backend as Document/ImportJob above. Never soft-deleted (no
        -- deleted_at column on this table), so existence alone is the live-reference test.
        IF EXISTS (
            SELECT 1 FROM public.project_sources
            WHERE storage_key = p_storage_key
        ) THEN
            RETURN true;
        END IF;

        -- 4. Pass 29: any ProjectCheckpoint whose resumption-brief blob is this key -- same
        -- shared storage backend, brief_storage_key is NOT NULL on every row.
        IF EXISTS (
            SELECT 1 FROM public.project_checkpoints
            WHERE brief_storage_key = p_storage_key
        ) THEN
            RETURN true;
        END IF;

        RETURN false;
    END;
    $$;

    REVOKE ALL ON FUNCTION public.storage_key_still_referenced_global(text) FROM PUBLIC;
    -- EXECUTE for mainai_app remains granted by backend/scripts/s1a_privilege_policy.py
    -- (same signature as migration 0020 -- nothing to change in that policy file).
"""

_DOWNGRADE_SQL = """
    CREATE OR REPLACE FUNCTION public.storage_key_still_referenced_global(p_storage_key text)
    RETURNS boolean
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
    AS $$
    BEGIN
        IF p_storage_key IS NULL THEN
            RETURN false;
        END IF;

        IF EXISTS (
            SELECT 1 FROM public.documents
            WHERE storage_key = p_storage_key AND deleted_at IS NULL
        ) THEN
            RETURN true;
        END IF;

        IF EXISTS (
            SELECT 1 FROM public.knowledge_import_jobs j
            WHERE j.source_storage_key = p_storage_key
              AND (
                  j.status IN ('pending', 'running', 'blocked')
                  OR (j.status = 'partial' AND j.blocked_count > 0)
                  OR EXISTS (
                      SELECT 1 FROM public.documents sib
                      WHERE sib.import_job_id = j.id
                        AND sib.deleted_at IS NULL
                        AND sib.status::text IN (
                            'pending', 'received', 'original_storing', 'original_stored',
                            'extracting', 'extracted', 'awaiting_classification',
                            'classifying', 'embedding', 'indexing'
                        )
                  )
              )
        ) THEN
            RETURN true;
        END IF;

        RETURN false;
    END;
    $$;

    REVOKE ALL ON FUNCTION public.storage_key_still_referenced_global(text) FROM PUBLIC;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    # Restores migration 0020's exact original body (not a DROP -- the function itself was
    # created by 0020, this migration only ever CREATE OR REPLACEs it), so
    # test_migration_roundtrip.py's whole-schema fingerprint (which hashes
    # pg_get_functiondef()) sees a real, different body after this downgrade and the exact
    # SAME body again after re-upgrading past 0020 without this migration.
    op.execute(_DOWNGRADE_SQL)
