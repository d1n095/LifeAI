"""Life Workspaces: persistent project metadata, activity, decisions and conversation links.

Additive only. Existing projects/documents/tasks/conversations keep their current ids and
foreign keys; this migration adds the missing workspace persistence and owner-scoped
history. It intentionally does not touch the Security Kernel or R15 migration lane.

Revision ID: 0093
Revises: 0092
Create Date: 2026-11-21
"""

from alembic import op

revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE projects
            ADD COLUMN purpose text,
            ADD COLUMN goals jsonb NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN settings jsonb NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN archived_at timestamp,
            ADD COLUMN deleted_at timestamp,
            ADD COLUMN last_activity_at timestamp NOT NULL DEFAULT now();

        ALTER TABLE projects
            ADD CONSTRAINT ck_projects_goals_array CHECK (jsonb_typeof(goals) = 'array'),
            ADD CONSTRAINT ck_projects_settings_object CHECK (jsonb_typeof(settings) = 'object');

        CREATE INDEX ix_projects_owner_active
            ON projects(created_by, archived_at, deleted_at, updated_at DESC);

        ALTER TABLE conversations
            ADD COLUMN project_id uuid REFERENCES projects(id) ON DELETE SET NULL;
        CREATE INDEX ix_conversations_project_id ON conversations(project_id);

        CREATE TABLE workspace_members (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role varchar(32) NOT NULL DEFAULT 'owner',
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_workspace_members_project_user UNIQUE (project_id, user_id),
            CONSTRAINT ck_workspace_members_role CHECK (role IN ('owner','collaborator'))
        );
        CREATE INDEX ix_workspace_members_project ON workspace_members(project_id);
        CREATE INDEX ix_workspace_members_user ON workspace_members(user_id);

        CREATE TABLE workspace_activities (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind varchar(64) NOT NULL,
            summary varchar(512) NOT NULL,
            detail jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT ck_workspace_activities_detail CHECK (jsonb_typeof(detail) = 'object')
        );
        CREATE INDEX ix_workspace_activities_project_created
            ON workspace_activities(project_id, created_at DESC);
        CREATE INDEX ix_workspace_activities_owner_created
            ON workspace_activities(owner_id, created_at DESC);

        CREATE TABLE workspace_decisions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title varchar(256) NOT NULL,
            decision text NOT NULL,
            rationale text,
            status varchar(16) NOT NULL DEFAULT 'accepted',
            created_at timestamp NOT NULL DEFAULT now(),
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT ck_workspace_decisions_status CHECK (status IN ('proposed','accepted','superseded'))
        );
        CREATE INDEX ix_workspace_decisions_project_created
            ON workspace_decisions(project_id, created_at DESC);
        CREATE INDEX ix_workspace_decisions_owner_created
            ON workspace_decisions(owner_id, created_at DESC);

        ALTER TABLE workspace_members ENABLE ROW LEVEL SECURITY;
        ALTER TABLE workspace_members FORCE ROW LEVEL SECURITY;
        CREATE POLICY workspace_members_isolation ON workspace_members
            USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        ALTER TABLE workspace_activities ENABLE ROW LEVEL SECURITY;
        ALTER TABLE workspace_activities FORCE ROW LEVEL SECURITY;
        CREATE POLICY workspace_activities_isolation ON workspace_activities
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        ALTER TABLE workspace_decisions ENABLE ROW LEVEL SECURITY;
        ALTER TABLE workspace_decisions FORCE ROW LEVEL SECURITY;
        CREATE POLICY workspace_decisions_isolation ON workspace_decisions
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        GRANT SELECT, INSERT, UPDATE, DELETE ON workspace_members TO mainai_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON workspace_activities TO mainai_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON workspace_decisions TO mainai_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS workspace_decisions_isolation ON workspace_decisions;
        DROP POLICY IF EXISTS workspace_activities_isolation ON workspace_activities;
        DROP POLICY IF EXISTS workspace_members_isolation ON workspace_members;
        DROP TABLE IF EXISTS workspace_decisions;
        DROP TABLE IF EXISTS workspace_activities;
        DROP TABLE IF EXISTS workspace_members;

        DROP INDEX IF EXISTS ix_conversations_project_id;
        ALTER TABLE conversations DROP COLUMN IF EXISTS project_id;

        DROP INDEX IF EXISTS ix_projects_owner_active;
        ALTER TABLE projects
            DROP CONSTRAINT IF EXISTS ck_projects_settings_object,
            DROP CONSTRAINT IF EXISTS ck_projects_goals_array,
            DROP COLUMN IF EXISTS last_activity_at,
            DROP COLUMN IF EXISTS deleted_at,
            DROP COLUMN IF EXISTS archived_at,
            DROP COLUMN IF EXISTS settings,
            DROP COLUMN IF EXISTS goals,
            DROP COLUMN IF EXISTS purpose;
        """
    )
