"""Personal work management: milestones, task graph/history and calendar context.

Additive only. The founder task manager continues to use the existing `tasks` table and the
calendar continues to use `calendar_events`; this migration adds the missing durable fields,
owner-scoped history/dependency tables, decision versioning and calendar links. It does not
touch the Security Kernel or the R15 lane.

Revision ID: 0095
Revises: 0094
Create Date: 2026-11-22
"""

from alembic import op

revision = "0095"
down_revision = "0094"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'blocked';
        ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'cancelled';

        CREATE TABLE workspace_milestones (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title varchar(256) NOT NULL,
            description text,
            due_at timestamp,
            status varchar(16) NOT NULL DEFAULT 'open',
            completed_at timestamp,
            version integer NOT NULL DEFAULT 1,
            created_at timestamp NOT NULL DEFAULT now(),
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT ck_workspace_milestones_status
                CHECK (status IN ('open','in_progress','completed','cancelled'))
        );
        CREATE INDEX ix_workspace_milestones_project_due
            ON workspace_milestones(project_id, due_at, id);
        CREATE INDEX ix_workspace_milestones_owner_status
            ON workspace_milestones(owner_id, status, due_at);

        ALTER TABLE tasks
            ADD COLUMN due_at timestamp,
            ADD COLUMN completed_at timestamp,
            ADD COLUMN blocked_reason varchar(512),
            ADD COLUMN milestone_id uuid REFERENCES workspace_milestones(id) ON DELETE SET NULL,
            ADD COLUMN version integer NOT NULL DEFAULT 1,
            ADD COLUMN deleted_at timestamp;
        CREATE INDEX ix_tasks_owner_project_status
            ON tasks(created_by, project_id, status, due_at);
        CREATE INDEX ix_tasks_owner_due
            ON tasks(created_by, due_at);
        CREATE INDEX ix_tasks_milestone
            ON tasks(milestone_id);

        CREATE TABLE task_dependencies (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            depends_on_task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_task_dependencies_edge UNIQUE (task_id, depends_on_task_id),
            CONSTRAINT ck_task_dependencies_no_self CHECK (task_id <> depends_on_task_id)
        );
        CREATE INDEX ix_task_dependencies_owner_task
            ON task_dependencies(owner_id, task_id);
        CREATE INDEX ix_task_dependencies_owner_dep
            ON task_dependencies(owner_id, depends_on_task_id);

        CREATE TABLE task_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            event_type varchar(32) NOT NULL,
            from_status varchar(32),
            to_status varchar(32),
            detail jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT ck_task_events_detail CHECK (jsonb_typeof(detail) = 'object')
        );
        CREATE INDEX ix_task_events_task_created
            ON task_events(task_id, created_at DESC, id);
        CREATE INDEX ix_task_events_owner_created
            ON task_events(owner_id, created_at DESC);

        ALTER TABLE workspace_decisions
            ADD COLUMN version integer NOT NULL DEFAULT 1,
            ADD COLUMN supersedes_id uuid REFERENCES workspace_decisions(id) ON DELETE SET NULL,
            ADD COLUMN epistemic_status varchar(16) NOT NULL DEFAULT 'confirmed',
            ADD COLUMN origin varchar(16) NOT NULL DEFAULT 'founder',
            ADD COLUMN superseded_at timestamp;
        ALTER TABLE workspace_decisions
            ADD CONSTRAINT ck_workspace_decisions_epistemic
                CHECK (epistemic_status IN ('confirmed','uncertain','proposed')),
            ADD CONSTRAINT ck_workspace_decisions_origin
                CHECK (origin IN ('founder','ai'));
        CREATE INDEX ix_workspace_decisions_project_version
            ON workspace_decisions(project_id, version DESC);

        ALTER TABLE calendar_events
            ADD COLUMN project_id uuid REFERENCES projects(id) ON DELETE SET NULL,
            ADD COLUMN task_id uuid REFERENCES tasks(id) ON DELETE SET NULL,
            ADD COLUMN milestone_id uuid REFERENCES workspace_milestones(id) ON DELETE SET NULL,
            ADD COLUMN category varchar(16) NOT NULL DEFAULT 'personal',
            ADD COLUMN timezone varchar(64) NOT NULL DEFAULT 'UTC',
            ADD COLUMN recurrence_rule varchar(16) NOT NULL DEFAULT 'none',
            ADD COLUMN recurrence_until timestamp,
            ADD COLUMN series_id uuid,
            ADD COLUMN recurrence_index integer NOT NULL DEFAULT 0,
            ADD COLUMN version integer NOT NULL DEFAULT 1,
            ADD COLUMN deleted_at timestamp;
        ALTER TABLE calendar_events
            ADD CONSTRAINT ck_calendar_events_category
                CHECK (category IN ('personal','work','school')),
            ADD CONSTRAINT ck_calendar_events_recurrence
                CHECK (recurrence_rule IN ('none','daily','weekly','monthly'));
        CREATE INDEX ix_calendar_events_owner_start
            ON calendar_events(owner_id, starts_at, id);
        CREATE INDEX ix_calendar_events_project
            ON calendar_events(project_id, starts_at);
        CREATE INDEX ix_calendar_events_series
            ON calendar_events(series_id, starts_at);

        ALTER TABLE workspace_milestones ENABLE ROW LEVEL SECURITY;
        ALTER TABLE workspace_milestones FORCE ROW LEVEL SECURITY;
        CREATE POLICY workspace_milestones_isolation ON workspace_milestones
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        ALTER TABLE task_dependencies ENABLE ROW LEVEL SECURITY;
        ALTER TABLE task_dependencies FORCE ROW LEVEL SECURITY;
        CREATE POLICY task_dependencies_isolation ON task_dependencies
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        ALTER TABLE task_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE task_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY task_events_isolation ON task_events
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        GRANT SELECT, INSERT, UPDATE, DELETE ON workspace_milestones TO mainai_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON task_dependencies TO mainai_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON task_events TO mainai_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS task_events_isolation ON task_events;
        DROP POLICY IF EXISTS task_dependencies_isolation ON task_dependencies;
        DROP POLICY IF EXISTS workspace_milestones_isolation ON workspace_milestones;

        DROP INDEX IF EXISTS ix_calendar_events_series;
        DROP INDEX IF EXISTS ix_calendar_events_project;
        DROP INDEX IF EXISTS ix_calendar_events_owner_start;
        ALTER TABLE calendar_events
            DROP CONSTRAINT IF EXISTS ck_calendar_events_recurrence,
            DROP CONSTRAINT IF EXISTS ck_calendar_events_category,
            DROP COLUMN IF EXISTS deleted_at,
            DROP COLUMN IF EXISTS version,
            DROP COLUMN IF EXISTS recurrence_index,
            DROP COLUMN IF EXISTS series_id,
            DROP COLUMN IF EXISTS recurrence_until,
            DROP COLUMN IF EXISTS recurrence_rule,
            DROP COLUMN IF EXISTS timezone,
            DROP COLUMN IF EXISTS category,
            DROP COLUMN IF EXISTS milestone_id,
            DROP COLUMN IF EXISTS task_id,
            DROP COLUMN IF EXISTS project_id;

        DROP INDEX IF EXISTS ix_workspace_decisions_project_version;
        ALTER TABLE workspace_decisions
            DROP CONSTRAINT IF EXISTS ck_workspace_decisions_origin,
            DROP CONSTRAINT IF EXISTS ck_workspace_decisions_epistemic,
            DROP COLUMN IF EXISTS superseded_at,
            DROP COLUMN IF EXISTS origin,
            DROP COLUMN IF EXISTS epistemic_status,
            DROP COLUMN IF EXISTS supersedes_id,
            DROP COLUMN IF EXISTS version;

        DROP TABLE IF EXISTS task_events;
        DROP TABLE IF EXISTS task_dependencies;
        DROP INDEX IF EXISTS ix_tasks_milestone;
        DROP INDEX IF EXISTS ix_tasks_owner_due;
        DROP INDEX IF EXISTS ix_tasks_owner_project_status;
        ALTER TABLE tasks
            DROP COLUMN IF EXISTS deleted_at,
            DROP COLUMN IF EXISTS version,
            DROP COLUMN IF EXISTS milestone_id,
            DROP COLUMN IF EXISTS blocked_reason,
            DROP COLUMN IF EXISTS completed_at,
            DROP COLUMN IF EXISTS due_at;

        DROP TABLE IF EXISTS workspace_milestones;
        """
    )
