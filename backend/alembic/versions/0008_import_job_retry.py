"""STEG 11: retry/coordination bookkeeping on knowledge_import_jobs — attempt_count and
max_attempts (exponential-backoff retry, app/jobs/retry.py) and last_failure_transient (so
the Library UI can distinguish "will retry automatically" from "needs a manual re-import",
see app/rag/library_import.py). No new tables — this app already has exactly one Redis
instance and the JobLock coordination primitive (app/jobs/lock.py) needs no schema of its
own, it's entirely Redis-side (a lease key with a TTL).

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-20
"""

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE knowledge_import_jobs
            ADD COLUMN attempt_count integer NOT NULL DEFAULT 0,
            ADD COLUMN max_attempts integer NOT NULL DEFAULT 3,
            ADD COLUMN last_failure_transient boolean;
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE knowledge_import_jobs
            DROP COLUMN attempt_count,
            DROP COLUMN max_attempts,
            DROP COLUMN last_failure_transient;
    """)
