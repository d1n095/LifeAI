"""Pass 23 (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md's Life Library storage layer, PR #31's
blob-reference hardening): a single, narrow, database-owned `storage_key_still_referenced_
global(text) RETURNS boolean` function — the fix for a real cross-owner gap a founder review
caught in `app/rag/blob_references.py::storage_key_still_referenced()`.

The bug this closes: content-addressed blob storage is GLOBAL — two different owners whose
uploads happen to be byte-identical share the exact same `storage_key`, by design (see
app/storage/local_fs.py). But `documents` and `knowledge_import_jobs` both have `FORCE ROW
LEVEL SECURITY` (app/rls.py) with owner-scoped policies (`uploaded_by = app.current_user_id`,
`owner_id = app.current_user_id`). The Python reference check that decided whether a blob was
safe to physically delete ran as an ordinary ORM query inside the CALLING owner's own
RLS-scoped session — which structurally CANNOT see a different owner's live Document or
pending/running/blocked ImportJob referencing the same key. Owner A deleting a document could
therefore delete a blob owner B's still-pending import job depended on, with RLS itself being
the reason the danger was invisible to the check that was supposed to prevent it.

The fix is NOT `SET row_security = off` in the calling session — per Postgres's own docs (and
per this exact codebase's own established precedent, see migration 0019's
transition_memory_source_admin/erase_owner_memory_admin), that setting does not grant a
non-exempt role anything RLS would otherwise deny; it only turns a silently-filtered result
into an error. The only real way to see across all owners despite FORCE RLS is a role that
genuinely has BYPASSRLS (or is superuser) — which is exactly what the migration/admin role
already has, and already proves via apply_runtime_privileges.py's verification of the two
existing *_admin functions above. This function follows the identical, already-reviewed
pattern: SECURITY DEFINER, owned by that same admin/migration role, `SET search_path =
pg_catalog` (every relation reference below is `public.`-qualified, so mainai_app's own
temp-table-shadowing risk — see migration 0019's module docstring — doesn't apply here either),
REVOKE ALL FROM PUBLIC, and (via backend/scripts/s1a_privilege_policy.py, applied by
ensure_app_role.py/apply_runtime_privileges.py on every boot, never assumed) EXECUTE granted
ONLY to mainai_app.

Deliberately information-minimal: the function returns a single boolean, nothing else. It
never returns which owner, which document, which job, or any other identifying detail — the
whole point is that `mainai_app` (running as an arbitrary owner's request) gets a yes/no
answer about a blob's global reference count without ever being able to read a DIFFERENT
owner's row directly. `app/rag/blob_references.py::storage_key_still_referenced()` now calls
this function instead of doing its own owner-scoped ORM queries against `documents`/
`knowledge_import_jobs` — the actual reference-check LOGIC (which ImportJob statuses still
need the blob, matching app/worker.py's real resumption paths) is unchanged from Pass 22,
just re-homed into a query that can actually see the whole table instead of one owner's slice
of it.
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION storage_key_still_referenced_global(p_storage_key text)
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
            -- read from -- the exact same policy app/rag/blob_references.py's module
            -- docstring documents (matched against app/worker.py's real resumption paths,
            -- not guessed at). NOTE: app.models.document.RESUMABLE_INDEX_STATUSES's string
            -- values are hardcoded below, same as every other lifecycle string literal
            -- already hardcoded in this schema's trigger/SECURITY DEFINER functions (see
            -- migration 0019) -- if that Python frozenset ever changes, this list must be
            -- updated in the same change, or this function silently falls out of sync.
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

        REVOKE ALL ON FUNCTION storage_key_still_referenced_global(text) FROM PUBLIC;
        -- EXECUTE for mainai_app is granted by backend/scripts/s1a_privilege_policy.py
        -- (applied by ensure_app_role.py/apply_runtime_privileges.py on every boot), not
        -- here -- see migration 0019's module docstring for exactly why a literal GRANT/
        -- REVOKE naming mainai_app can't live in a migration itself (it would break the
        -- "Backend — Alembic migration check" CI job, whose database never creates that
        -- role).
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS storage_key_still_referenced_global(text);")
