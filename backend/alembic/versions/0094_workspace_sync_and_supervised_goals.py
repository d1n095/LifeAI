"""Workspace optimistic versions, durable sync runs and supervised MainAI goal links.

Additive only. The workspace product still uses the existing `projects`, `documents`,
`tasks`, `conversations`, `mainai_goals` and `mainai_tasks` tables; this migration adds the
missing concurrency/synchronization state and a nullable workspace link on the existing
MainAI goal family. It does not touch the Security Kernel or R15 migration lane.

Revision ID: 0094
Revises: 0093
Create Date: 2026-11-21
"""

from alembic import op

revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE projects
            ADD COLUMN version integer NOT NULL DEFAULT 1;

        ALTER TABLE mainai_goals
            ADD COLUMN project_id uuid REFERENCES projects(id) ON DELETE SET NULL;
        CREATE INDEX ix_mainai_goals_project_created
            ON mainai_goals(project_id, created_at DESC);

        CREATE TABLE workspace_sync_runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            idempotency_key varchar(128) NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'queued',
            snapshot_hash varchar(64) NOT NULL,
            snapshot_version integer NOT NULL DEFAULT 1,
            progress_current integer NOT NULL DEFAULT 0,
            progress_total integer NOT NULL DEFAULT 0,
            attempt integer NOT NULL DEFAULT 1,
            error_category varchar(64),
            result jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp NOT NULL DEFAULT now(),
            started_at timestamp,
            completed_at timestamp,
            heartbeat_at timestamp,
            CONSTRAINT uq_workspace_sync_runs_key
                UNIQUE (owner_id, project_id, idempotency_key),
            CONSTRAINT ck_workspace_sync_runs_status
                CHECK (status IN ('queued','running','completed','failed','cancelled','stale')),
            CONSTRAINT ck_workspace_sync_runs_result
                CHECK (jsonb_typeof(result) = 'object'),
            CONSTRAINT ck_workspace_sync_runs_progress
                CHECK (progress_current >= 0 AND progress_total >= 0)
        );
        CREATE INDEX ix_workspace_sync_runs_project_created
            ON workspace_sync_runs(project_id, created_at DESC);
        CREATE INDEX ix_workspace_sync_runs_owner_status
            ON workspace_sync_runs(owner_id, status, created_at DESC);

        CREATE TABLE workspace_sync_items (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id uuid NOT NULL REFERENCES workspace_sync_runs(id) ON DELETE CASCADE,
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            entity_type varchar(24) NOT NULL,
            entity_id uuid NOT NULL,
            entity_version integer NOT NULL DEFAULT 1,
            content_hash varchar(64) NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'pending',
            error varchar(512),
            processed_at timestamp,
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_workspace_sync_items_entity
                UNIQUE (run_id, entity_type, entity_id),
            CONSTRAINT ck_workspace_sync_items_status
                CHECK (status IN ('pending','applied','skipped','failed'))
        );
        CREATE INDEX ix_workspace_sync_items_run_status
            ON workspace_sync_items(run_id, status);
        CREATE INDEX ix_workspace_sync_items_project_entity
            ON workspace_sync_items(project_id, entity_type, entity_id, created_at DESC);

        ALTER TABLE workspace_sync_runs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE workspace_sync_runs FORCE ROW LEVEL SECURITY;
        CREATE POLICY workspace_sync_runs_isolation ON workspace_sync_runs
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        ALTER TABLE workspace_sync_items ENABLE ROW LEVEL SECURITY;
        ALTER TABLE workspace_sync_items FORCE ROW LEVEL SECURITY;
        CREATE POLICY workspace_sync_items_isolation ON workspace_sync_items
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        GRANT SELECT, INSERT, UPDATE, DELETE ON workspace_sync_runs TO mainai_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON workspace_sync_items TO mainai_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS workspace_sync_items_isolation ON workspace_sync_items;
        DROP POLICY IF EXISTS workspace_sync_runs_isolation ON workspace_sync_runs;
        DROP TABLE IF EXISTS workspace_sync_items;
        DROP TABLE IF EXISTS workspace_sync_runs;

        DROP INDEX IF EXISTS ix_mainai_goals_project_created;
        ALTER TABLE mainai_goals DROP COLUMN IF EXISTS project_id;

        ALTER TABLE projects DROP COLUMN IF EXISTS version;
        """
    )
