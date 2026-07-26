"""MainAI Project Memory & Coordination Loop — Fas 2 (source ingestion + branch/PR status).

Adds the pieces needed to make the loop actually operative, not just a storage table:

  - project_sources: one row per ingested source (a governing doc's content, a git HEAD
    snapshot, or an agent-supplied GitHub branch/PR snapshot — there is no in-process GitHub
    client in this codebase, so GitHub state is ingested as structured input the caller
    already fetched, not re-fetched here). Doc content is stored via the existing
    content-addressed storage backend (app/storage) — this table only indexes it, exactly
    like project_checkpoints already does for the resumption brief.
  - project_branch_pr_status: structured, queryable branch/PR state, distinct from freeform
    ProjectNote — "which branch/PR applies" needs real fields (base/head/mergeable/CI), not
    prose. Never overwritten in place: a new snapshot for the same (kind, ref) supersedes the
    previous one (is_current flips to false), preserving history.
  - project_notes gains: an optional source_id citation (stronger provenance than the
    existing free-text source_type/source_ref alone), and an optional classification column
    for side-issue triage (blocking/directly_resolvable/registered_for_later/
    needs_founder_decision) — MainAI stores the classification, it does not compute it
    autonomously (see app/project_memory.py).
  - project_checkpoints gains a git_commit_sha column, the basis for staleness detection
    (has the project moved on since this checkpoint was taken?).

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-26
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE project_sources (
            id uuid PRIMARY KEY,
            source_type varchar(32) NOT NULL,
            source_ref varchar(512) NOT NULL,
            content_sha256 varchar(64),
            storage_key varchar(80),
            commit_sha varchar(40),
            raw_data jsonb,
            ingested_by varchar(64) NOT NULL,
            ingested_at timestamp without time zone NOT NULL DEFAULT now()
        );

        CREATE INDEX ix_project_sources_ingested_at ON project_sources (ingested_at);
        CREATE INDEX ix_project_sources_type_ref ON project_sources (source_type, source_ref);
    """)

    op.execute("""
        CREATE TABLE project_branch_pr_status (
            id uuid PRIMARY KEY,
            kind varchar(16) NOT NULL,
            ref varchar(256) NOT NULL,
            title text,
            status varchar(32) NOT NULL,
            base_ref varchar(256),
            head_ref varchar(256),
            mergeable boolean,
            ci_status varchar(32),
            summary text,
            source_id uuid REFERENCES project_sources(id),
            is_current boolean NOT NULL DEFAULT true,
            recorded_by varchar(64) NOT NULL,
            recorded_at timestamp without time zone NOT NULL DEFAULT now(),
            superseded_at timestamp without time zone
        );

        CREATE INDEX ix_project_branch_pr_status_kind_ref ON project_branch_pr_status (kind, ref);
        CREATE INDEX ix_project_branch_pr_status_current ON project_branch_pr_status (is_current);
    """)

    op.execute("""
        ALTER TABLE project_notes
            ADD COLUMN source_id uuid REFERENCES project_sources(id),
            ADD COLUMN classification varchar(32);
    """)

    op.execute("""
        ALTER TABLE project_checkpoints
            ADD COLUMN git_commit_sha varchar(40);
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE project_checkpoints DROP COLUMN git_commit_sha;
    """)
    op.execute("""
        ALTER TABLE project_notes DROP COLUMN source_id, DROP COLUMN classification;
    """)
    op.execute("DROP TABLE project_branch_pr_status;")
    op.execute("DROP TABLE project_sources;")
