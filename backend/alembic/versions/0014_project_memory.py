"""MainAI Project Memory & Coordination Loop — first working slice.

Minimal, deliberately narrow first version (see CLAUDE.md's "Malet" section and the
2026-07-26 direction to reuse P1/P2 infrastructure rather than build a parallel memory
system): three additive tables, no new architecture, no RLS (founder-wide project state,
same rationale as provider_config/provider_verification_checks in migrations 0001/0013).

  - project_notes: decisions/blockers/next-steps, always source-cited, never deleted (a
    resolved/superseded row stays as history — see NoteStatus).
  - project_checkpoints: a point-in-time snapshot pointing at a full resumption-brief
    markdown stored via the existing content-addressed storage backend (app/storage) — this
    table only indexes it, it does not duplicate the storage layer.
  - project_checkpoint_notes: which notes were open at checkpoint time, so a checkpoint's
    "current state" claim is verifiable against real rows, not just trusted free text.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-26
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE project_notes (
            id uuid PRIMARY KEY,
            kind varchar(16) NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'open',
            content text NOT NULL,
            source_type varchar(32) NOT NULL,
            source_ref varchar(256) NOT NULL,
            created_by varchar(64) NOT NULL,
            created_at timestamp without time zone NOT NULL DEFAULT now(),
            resolved_at timestamp without time zone,
            resolved_by varchar(64),
            resolution_note text
        );

        CREATE INDEX ix_project_notes_created_at ON project_notes (created_at);
        CREATE INDEX ix_project_notes_status_kind ON project_notes (status, kind);
    """)

    op.execute("""
        CREATE TABLE project_checkpoints (
            id uuid PRIMARY KEY,
            summary text NOT NULL,
            branch_name varchar(256) NOT NULL,
            open_pr_refs varchar(512) NOT NULL,
            brief_storage_key varchar(80) NOT NULL,
            brief_sha256 varchar(64) NOT NULL,
            created_by varchar(64) NOT NULL,
            created_at timestamp without time zone NOT NULL DEFAULT now()
        );

        CREATE INDEX ix_project_checkpoints_created_at ON project_checkpoints (created_at);
    """)

    op.execute("""
        CREATE TABLE project_checkpoint_notes (
            id uuid PRIMARY KEY,
            checkpoint_id uuid NOT NULL REFERENCES project_checkpoints(id),
            note_id uuid NOT NULL REFERENCES project_notes(id)
        );

        CREATE INDEX ix_project_checkpoint_notes_checkpoint_id ON project_checkpoint_notes (checkpoint_id);
        CREATE INDEX ix_project_checkpoint_notes_note_id ON project_checkpoint_notes (note_id);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE project_checkpoint_notes;")
    op.execute("DROP TABLE project_checkpoints;")
    op.execute("DROP TABLE project_notes;")
