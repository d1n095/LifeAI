"""Account export/erasure integration (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8, PR
#31's Pass 26): `storage_deletion_tasks` — a durable, retryable record of every physical blob
a completed account erasure still needs to (attempt to) delete from disk.

Why this table has to exist at all: `Document.storage_key`/`ImportJob.source_storage_key`
point at real original files in `app/storage/` (content-addressed, see
app/storage/local_fs.py). Account erasure's DB phase (app/rag/account_erasure.py) deletes
every DB row referencing those keys in one atomic transaction — but `storage.delete()` is a
real filesystem call that can fail (disk I/O error) or simply never run (process crash
between the DB commit and the best-effort delete attempt right after it, mirroring
app/rag/source_purge.py's own Phase A/Phase B split). Without a durable record of "these keys
still need to be checked/deleted," a crash in that narrow window would leave an orphaned
original file on disk with no way to ever find or clean it up again — the DB row that used to
point at it is gone, by design (the whole point of erasure).

Deliberately NOT scoped by `owner_id` and NOT foreign-keyed to `users.id` — the whole reason
this table exists is to outlive the very User row whose erasure created it (see
docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8's erasure model: the person is gone, but a
background retry must still be able to finish cleaning up their old files). `operation_id` is
a synthetic, unattributable identifier for one erasure run — never the erased user's id
itself — so a row here reveals nothing about which account it came from once that account is
gone. No email, name, or other PII is stored: `storage_key` is a content hash, not personal
data on its own.

`reason` is deliberately a fixed, closed enum (`account_erasure`) rather than a free-text
field — a future second reason (e.g. a scheduled retention-policy purge) gets its own literal
added here, not a caller-supplied string, keeping this table's CHECK constraint the single
source of truth for what's allowed to write to it.

Status lifecycle (mirrors app/models/document.py's DeletionStatus, but kept as its own
lowercase-string CHECK rather than a shared Python enum, since this table's status values are
richer -- `processing` and `retained_shared` have no DeletionStatus equivalent):
  - `pending` -- freshly inserted, not yet attempted.
  - `processing` -- claimed by a retry attempt (worker or the immediate best-effort call);
    reset back to `pending`/`failed` if that attempt doesn't reach a terminal status.
  - `purged` -- the file was physically deleted (or was already gone -- idempotent).
  - `retained_shared` -- terminal, NOT a failure: `storage_key_still_referenced_global()`
    (migration 0020) says another owner's live Document/ImportJob still needs this exact
    content-addressed blob. The erased owner's own DB rows are already gone; the shared
    physical file correctly survives for whoever else still needs it. A task must never sit
    `pending` forever just because the bytes happen to be shared.
  - `failed` -- a real I/O error; retryable, same as DeletionStatus.failed elsewhere.

EXECUTE/GRANT on `mainai_app` is applied by backend/scripts/s1a_privilege_policy.py (via
ensure_app_role.py/apply_runtime_privileges.py on every boot), never a literal GRANT here —
see migration 0019's module docstring for why a hardcoded `mainai_app` reference in a
migration itself would break the "Backend — Alembic migration check" CI job, whose database
never creates that role.
"""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE storage_deletion_tasks (
            id uuid PRIMARY KEY,
            operation_id uuid NOT NULL,
            storage_key varchar(140) NOT NULL,
            reason varchar(32) NOT NULL DEFAULT 'account_erasure',
            status varchar(16) NOT NULL DEFAULT 'pending',
            attempt_count integer NOT NULL DEFAULT 0,
            last_error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz,
            CONSTRAINT uq_storage_deletion_tasks_operation_key UNIQUE (operation_id, storage_key),
            CONSTRAINT ck_storage_deletion_tasks_reason CHECK (reason IN ('account_erasure')),
            CONSTRAINT ck_storage_deletion_tasks_status CHECK (
                status IN ('pending', 'processing', 'purged', 'retained_shared', 'failed')
            ),
            CONSTRAINT ck_storage_deletion_tasks_attempt_count CHECK (attempt_count >= 0)
        );

        CREATE INDEX ix_storage_deletion_tasks_status ON storage_deletion_tasks (status);
        CREATE INDEX ix_storage_deletion_tasks_operation_id ON storage_deletion_tasks (operation_id);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS storage_deletion_tasks;")
