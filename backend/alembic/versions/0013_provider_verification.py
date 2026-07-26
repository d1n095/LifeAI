"""P1 (provider pre-flight verification) package.

Splits the single, odifferentierade `IndexStatus.failed` into five distinguishable outcomes
so a founder never mistakes "AI-leverantören är otillgänglig just nu" for "importen
misslyckades i grunden" (see docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.7):

  - `awaiting_provider`: no provider configured for the role at all — pauses, never fails.
  - `blocked_provider`: a provider is configured but pre-flight verification did not return
    `ok` (invalid key, unreachable, rate-limited, or the wrong provider for the role) —
    pauses, never fails.
  - `storage_failed` / `extraction_failed` / `indexing_failed`: the three previously
    undifferentiated `except Exception: status = failed` sites in
    app/rag/library_import.py / app/rag/ingest.py, now each keeping their own name.

Purely additive to the existing `indexstatus` enum — same `ADD VALUE IF NOT EXISTS` pattern
as migrations 0011/0012, no existing row is touched or reclassified.

provider_verification_checks: one row per real verification attempt (see
app/providers/verification.py) — the record that lets both Admin -> Providers and the
worker's automatic requeue (app/worker.py) answer "is the active provider actually usable
right now" without re-verifying on every single request. Deliberately NOT RLS-protected —
same as provider_config (see migration 0001): provider verification is founder-wide
configuration state, not per-user data.

knowledge_import_jobs.blocked_count: the same additive-column pattern PR #6 already
established for succeeded_count/failed_count/skipped_count — a job where every file is
paused on the provider (not genuinely failed) gets its own, honest count rather than being
folded into failed_count.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-23
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE indexstatus ADD VALUE IF NOT EXISTS 'awaiting_provider';")
    op.execute("ALTER TYPE indexstatus ADD VALUE IF NOT EXISTS 'blocked_provider';")
    op.execute("ALTER TYPE indexstatus ADD VALUE IF NOT EXISTS 'storage_failed';")
    op.execute("ALTER TYPE indexstatus ADD VALUE IF NOT EXISTS 'extraction_failed';")
    op.execute("ALTER TYPE indexstatus ADD VALUE IF NOT EXISTS 'indexing_failed';")

    op.execute("""
        CREATE TABLE provider_verification_checks (
            id uuid PRIMARY KEY,
            provider_name varchar(32) NOT NULL,
            role varchar(16) NOT NULL,
            model varchar(128) NOT NULL,
            result varchar(16) NOT NULL,
            message varchar(256) NOT NULL,
            checked_by varchar(16) NOT NULL,
            checked_at timestamp without time zone NOT NULL DEFAULT now()
        );

        CREATE INDEX ix_provider_verification_checks_lookup
            ON provider_verification_checks (provider_name, role, checked_at DESC);
    """)

    op.execute("""
        ALTER TABLE knowledge_import_jobs
            ADD COLUMN blocked_count integer NOT NULL DEFAULT 0;
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE knowledge_import_jobs
            DROP COLUMN blocked_count;
    """)

    op.execute("""
        DROP TABLE provider_verification_checks;
    """)

    # Postgres has no ALTER TYPE ... DROP VALUE — same recreate-the-type workaround as
    # migrations 0005/0011/0012's downgrades. Any row already sitting in one of the five new
    # states is remapped to its closest legacy predecessor first, so the USING cast below
    # completes instead of aborting on rows this migration itself produced.
    op.execute("""
        UPDATE documents SET status = 'failed'
            WHERE status IN ('awaiting_provider', 'blocked_provider', 'storage_failed', 'extraction_failed', 'indexing_failed');
    """)
    op.execute("""
        ALTER TYPE indexstatus RENAME TO indexstatus_old;
        CREATE TYPE indexstatus AS ENUM (
            'pending', 'received', 'original_storing', 'original_stored', 'extracting', 'extracted',
            'awaiting_classification', 'classifying', 'embedding', 'indexing', 'indexed', 'failed', 'cancelled'
        );
        ALTER TABLE documents ALTER COLUMN status TYPE indexstatus USING status::text::indexstatus;
        DROP TYPE indexstatus_old;
    """)
