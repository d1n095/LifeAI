"""Pass 31 (PR #31, a sixth founder review round of the account-erasure/blob-integrity slice):
widens `storage_deletion_tasks.reason` to also allow `'rejected_upload_cleanup'`, alongside
the existing `'account_erasure'`.

The gap this closes: Pass 30's `delete_if_unreferenced()` (app/rag/blob_references.py) does
the check-then-act protocol correctly (lock -> global reference check -> delete only when
unreferenced) for a rejected empty upload, but on a genuine `StorageError` during the physical
delete itself, it used to only `logger.exception(...)` and return a `failed` outcome -- no
durable, retryable record was ever created. A founder review rejected this as insufficient:
"En loggrad är inte en beständig cleanup-plan." -- a confirmed-unreferenced blob that fails to
physically delete becomes an invisible, uninventoried orphan on disk forever (or until a human
notices from logs), with no automated way to ever find or retry it -- exactly the same shape
of problem `storage_deletion_tasks` (migration 0021) was already built to solve for account
erasure, just not reachable from this second, later-discovered code path.

Reuses the EXISTING table and its EXISTING worker/backoff/lease/reference-check machinery
(`app/rag/account_erasure.py`'s `claim_storage_deletion_tasks()`/`attempt_storage_deletion_
task()`, `app/worker.py`'s `_retry_storage_deletion_tasks()`) rather than building a second,
parallel retry system -- none of that code branches on `reason` at all, it already treats
every task uniformly by `storage_key`, so widening this one CHECK constraint is sufficient to
make the existing worker pick up and retry a rejected-upload cleanup exactly like it already
does for an account-erasure one.

Deliberately NOT routed through a new SECURITY DEFINER function the way `enqueue_account_
erasure_storage_task()` (migration 0022) is for `account_erasure` tasks -- see
app/rag/blob_references.py's `enqueue_rejected_upload_cleanup_task()` docstring for why that
function's ownership-verification model (checking `documents`/`knowledge_import_jobs` for the
caller's own row) has no equivalent here: a rejected upload was, BY DESIGN, never given a
Document/ImportJob row to verify ownership against (see delete_if_unreferenced()'s own
docstring -- the whole point is no such row is ever created). Exposing a SECURITY DEFINER
function callable by the ordinary, request-scoped `mainai_app` role with an arbitrary
caller-supplied `storage_key` and no ownership check to enforce would recreate exactly the
"indirect access to a privileged physical-delete operation" gap Pass 28 already closed for the
account-erasure path. Instead, `enqueue_rejected_upload_cleanup_task()` runs entirely on the
same privileged admin/migration connection `attempt_pending_storage_deletions_for_operation()`
already uses (`_MaintenanceSession`, bound to `app.db.migration_engine`) -- `mainai_app`
continues to get ZERO direct privileges on this table, for ANY reason value, and the new
enqueue path is only ever reachable from `delete_if_unreferenced()`'s own internal exception
handler with the exact `storage_key` that same call was already given -- never from a
request-controlled parameter.

Downgrade deletes any `rejected_upload_cleanup` rows before re-tightening the CHECK constraint
back to `('account_erasure')` only -- a downgrade to the OLDER schema version cannot represent
a reason value that schema version never knew about; losing those specific retry records on
a genuine rollback is the same ordinary "downgrade may discard data the newer version
introduced" behavior migration 0022's own `DROP COLUMN next_attempt_at` already accepts for
that column's data.
"""

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE storage_deletion_tasks DROP CONSTRAINT ck_storage_deletion_tasks_reason;
        ALTER TABLE storage_deletion_tasks ADD CONSTRAINT ck_storage_deletion_tasks_reason
            CHECK (reason IN ('account_erasure', 'rejected_upload_cleanup'));
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM storage_deletion_tasks WHERE reason = 'rejected_upload_cleanup';
        ALTER TABLE storage_deletion_tasks DROP CONSTRAINT ck_storage_deletion_tasks_reason;
        ALTER TABLE storage_deletion_tasks ADD CONSTRAINT ck_storage_deletion_tasks_reason
            CHECK (reason IN ('account_erasure'));
    """)
