"""MainAI Core: agent orchestration — the first vertical chain from a project note through a
dispatched code/review agent to a proposed (or, gated, real) GitHub PR (see CLAUDE.md's
2026-07-26 "MainAI Core" direction). Two additive tables, no new architecture:

  - agent_tasks: one scoped work order (title, description, target files, constraints,
    acceptance criteria, required tests) MainAI created — always derived from a real
    ProjectNote, never a bare instruction.
  - agent_task_events: an append-only history of everything that happened to a task
    (dispatch, recorded result, recorded test run, review verdict, GitHub operations) — same
    never-delete pattern as project_notes/project_branch_pr_status.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-26
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE agent_tasks (
            id uuid PRIMARY KEY,
            title varchar(256) NOT NULL,
            description text NOT NULL,
            target_files jsonb NOT NULL DEFAULT '[]',
            constraints text,
            acceptance_criteria text NOT NULL,
            required_tests text,
            status varchar(32) NOT NULL DEFAULT 'created',
            source_note_id uuid REFERENCES project_notes(id),
            created_by varchar(64) NOT NULL,
            created_at timestamp without time zone NOT NULL DEFAULT now(),
            updated_at timestamp without time zone NOT NULL DEFAULT now()
        );

        CREATE INDEX ix_agent_tasks_created_at ON agent_tasks (created_at);
    """)

    op.execute("""
        CREATE TABLE agent_task_events (
            id uuid PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES agent_tasks(id),
            event_type varchar(32) NOT NULL,
            role varchar(16),
            provider varchar(32),
            model varchar(64),
            payload jsonb NOT NULL DEFAULT '{}',
            created_by varchar(64) NOT NULL,
            created_at timestamp without time zone NOT NULL DEFAULT now()
        );

        CREATE INDEX ix_agent_task_events_task_id ON agent_task_events (task_id);
        CREATE INDEX ix_agent_task_events_created_at ON agent_task_events (created_at);
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS agent_task_events;
        DROP TABLE IF EXISTS agent_tasks;
    """)
