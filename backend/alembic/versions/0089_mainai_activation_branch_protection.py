"""Protect the activation integration branch (and every `mainai/` branch) from task worktrees.

Widens migration 0033's ck_mainai_task_worktrees_branch_not_protected: a task worktree's
working branch must never be the Founder-supervised activation integration branch, nor any
other `mainai/` integration/release branch. Task branches are always
`claude/mainai-task-<task_id>`, so existing rows already satisfy the wider constraint.

Revision ID: 0089_activation_branch_protect
Revises: 0088_erasure_completion_phase
Create Date: 2026-10-17
"""

from alembic import op

revision = "0089_activation_branch_protect"
down_revision = "0088_erasure_completion_phase"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE mainai_task_worktrees DROP CONSTRAINT ck_mainai_task_worktrees_branch_not_protected;
        ALTER TABLE mainai_task_worktrees ADD CONSTRAINT ck_mainai_task_worktrees_branch_not_protected CHECK (
            branch NOT IN ('main', 'master', 'claude/det-kommer-mer-879lcm', 'mainai/integration-founder-supervised-v1')
            AND branch NOT LIKE 'mainai/%'
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE mainai_task_worktrees DROP CONSTRAINT ck_mainai_task_worktrees_branch_not_protected;
        ALTER TABLE mainai_task_worktrees ADD CONSTRAINT ck_mainai_task_worktrees_branch_not_protected CHECK (
            branch NOT IN ('main', 'master', 'claude/det-kommer-mer-879lcm')
        );
        """
    )
