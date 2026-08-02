"""Pass 28 (PR #31, second founder review round of the account-erasure slice): a narrow
SECURITY DEFINER function, `enqueue_account_erasure_storage_task()`, replacing the ordinary
ORM `INSERT` into `storage_deletion_tasks` that Pass 27 still left `mainai_app` doing directly.
Also adds `next_attempt_at` (worker backoff for `failed` tasks — see this migration's second
half and app/rag/account_erasure.py's `attempt_storage_deletion_task()`).

The gap this closes: Pass 27 narrowed `mainai_app` to INSERT-only on `storage_deletion_tasks`,
reasoning that INSERT alone was harmless since the table has no owner_id/RLS to bypass. A
founder review pointed out this was still wrong — INSERT into this specific table is not mere
metadata write access, it is INDIRECT ACCESS TO A PRIVILEGED PHYSICAL-DELETE OPERATION. Nothing
in the database verified that an inserted `storage_key` actually belonged to the inserting
owner, was ever created by a genuine account erasure, or even referenced anything real at all.
A compromised request path, or a future SQL bug anywhere reachable by `mainai_app`, could queue
an ARBITRARY storage_key — and the maintenance worker would later check only `documents`/
`knowledge_import_jobs` (migration 0020's `storage_key_still_referenced_global()`) and, finding
no reference there, physically delete it. `app/project_memory.py`'s blobs (`ProjectSource`/
`ProjectCheckpoint`, founder-wide project memory, deliberately outside those two reference
tables — see app/rag/account_erasure.py's blob-write-path audit) are exactly the kind of data a
maliciously- or accidentally-queued key could destroy with zero trace.

`mainai_app` now gets ZERO direct privileges on `storage_deletion_tasks` (enforced by
backend/scripts/s1a_privilege_policy.py, not here — see migration 0019's module docstring for
why a literal GRANT/REVOKE naming `mainai_app` can't live in a migration itself). The ONLY way
an ordinary request session can create a task row is this function, which:
  - reads the caller from `current_setting('app.current_user_id')` (the SAME session variable
    RLS policies already check, and `erase_owner_memory()`/`transition_own_memory_source()`
    already rely on — see migration 0019),
  - requires an authenticated caller,
  - VERIFIES `p_storage_key` currently belongs to that caller via `Document.storage_key` OR
    `ImportJob.source_storage_key` — explicitly, in the function body, not via RLS (RLS is
    irrelevant here regardless of whether the owning role has BYPASSRLS, since the query
    always filters by the caller's own id explicitly, the same reasoning `erase_owner_memory()`
    already established: "no RLS bypass needed... the explicit ownership check is the real,
    independent enforcement"),
  - sets `reason`/`status` itself — a caller can never supply either,
  - is idempotent on `(operation_id, storage_key)` (`ON CONFLICT DO NOTHING`, matching the
    table's own unique constraint) — safe to call once per key per erasure operation without a
    pre-check.

No BYPASSRLS requirement on the owning role (like `transition_own_memory_source`/
`erase_owner_memory` above it, unlike `storage_key_still_referenced_global`/the two `_admin`
functions): this function only ever touches the CALLING owner's own rows, matched explicitly,
so it needs no cross-owner visibility at all.

`next_attempt_at` (second half of this migration): Pass 27's worker retry scan and Pass 28's
fix for the immediate-attempt infinite-retry bug (see app/rag/account_erasure.py's module
docstring) both need a durable backoff signal for a `failed` task — a permanent storage error
must not be retried again within the same immediate call, and the worker's own later retries
must back off exponentially rather than hammering the same broken key every poll cycle.
Nullable: only ever set when a task transitions to `failed`; `pending`/`processing` tasks
(never yet attempted, or actively being attempted) have no backoff to apply.
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE storage_deletion_tasks ADD COLUMN next_attempt_at timestamptz;

        CREATE OR REPLACE FUNCTION public.enqueue_account_erasure_storage_task(
            p_operation_id uuid,
            p_storage_key text
        ) RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        DECLARE
            v_caller uuid;
            v_owns   boolean;
        BEGIN
            v_caller := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_caller IS NULL THEN
                RAISE EXCEPTION 'enqueue_account_erasure_storage_task: no authenticated user context';
            END IF;

            IF p_storage_key IS NULL OR btrim(p_storage_key) = '' THEN
                RAISE EXCEPTION 'enqueue_account_erasure_storage_task: storage_key is required';
            END IF;

            SELECT EXISTS (
                SELECT 1 FROM public.documents
                WHERE storage_key = p_storage_key AND uploaded_by = v_caller
                UNION ALL
                SELECT 1 FROM public.knowledge_import_jobs
                WHERE source_storage_key = p_storage_key AND owner_id = v_caller
            ) INTO v_owns;

            IF NOT v_owns THEN
                RAISE EXCEPTION
                    'enqueue_account_erasure_storage_task: storage_key % is not owned by the caller', p_storage_key;
            END IF;

            INSERT INTO public.storage_deletion_tasks (id, operation_id, storage_key, reason, status)
            VALUES (gen_random_uuid(), p_operation_id, p_storage_key, 'account_erasure', 'pending')
            ON CONFLICT (operation_id, storage_key) DO NOTHING;
        END;
        $$;

        REVOKE ALL ON FUNCTION public.enqueue_account_erasure_storage_task(uuid, text) FROM PUBLIC;
        -- EXECUTE for mainai_app is granted by backend/scripts/s1a_privilege_policy.py
        -- (applied by ensure_app_role.py/apply_runtime_privileges.py on every boot), not
        -- here -- see migration 0019's module docstring for why.
    """)


def downgrade() -> None:
    op.execute("""
        DROP FUNCTION IF EXISTS public.enqueue_account_erasure_storage_task(uuid, text);
        ALTER TABLE storage_deletion_tasks DROP COLUMN IF EXISTS next_attempt_at;
    """)
