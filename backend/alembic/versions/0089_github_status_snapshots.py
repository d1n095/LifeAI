"""Founder Alpha R14 (rebuilt marker: 2026-11-19): READ-ONLY GitHub development-status snapshots.

One durable, owner-scoped row per successful read of the project's GitHub state (branch tip,
recent commits, open PRs, open issues, check-runs). The table exists so a failed refresh never
has to invent anything: the last VERIFIED picture is served with its age, while the failure is
reported alongside it.

Same signal-producer/truth-writer separation as the rest of this codebase: a snapshot is an
observation of GitHub, never a claim that something was merged, deployed or authorized.

Owner-scoped FORCE RLS, identical policy shape to github_evidence_candidates (migration 0076).
No application DELETE path exists: history is superseded by newer snapshots, not removed.
"""

from alembic import op

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE github_status_snapshots (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            repo varchar(256) NOT NULL,
            payload jsonb NOT NULL,
            fetched_at timestamp NOT NULL,
            created_at timestamp NOT NULL DEFAULT now(),
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT ck_github_status_snapshots_payload CHECK (jsonb_typeof(payload) = 'object')
        );
        CREATE INDEX ix_github_status_snapshots_owner_id ON github_status_snapshots(owner_id);

        ALTER TABLE github_status_snapshots ENABLE ROW LEVEL SECURITY;
        ALTER TABLE github_status_snapshots FORCE ROW LEVEL SECURITY;
        CREATE POLICY github_status_snapshots_isolation ON github_status_snapshots
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        GRANT SELECT, INSERT, UPDATE, DELETE ON github_status_snapshots TO mainai_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS github_status_snapshots_isolation ON github_status_snapshots;
        DROP TABLE IF EXISTS github_status_snapshots;
        """
    )
