"""Widen work_candidates.priority to also accept the horizon-based executive-priority
vocabulary (NOW/NEAR/LATER/OPTIONAL/BLOCKED).

Part of the founder-defined "Personal Intent & Executive Reasoning" workstream
(docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md §2.3,
docs/MAINAI_LONG_HORIZON_PLANNING.md §4.1). A bounded executive scan will generate
`WorkCandidate` rows classified by which planning horizon they belong to -- NOW (required for
the triggering instruction), NEAR (likely needed soon), LATER (real but not urgent), OPTIONAL
(surfaced despite a scan bound or low confidence -- never silently dropped), BLOCKED (depends
on something unresolved; see the existing `dependencies` column). This is additive, not a
rewrite: the existing `low`/`medium`/`high`/`urgent` vocabulary (migration 0055) is kept
verbatim, since nothing in application code branches on `WorkCandidate.priority`'s specific
value today (confirmed by a whole-tree grep before writing this migration) -- existing rows
and any future caller of the pre-existing vocabulary are unaffected."""

from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE work_candidates DROP CONSTRAINT ck_work_candidates_priority;
        ALTER TABLE work_candidates ADD CONSTRAINT ck_work_candidates_priority CHECK (
            priority IN ('low', 'medium', 'high', 'urgent', 'NOW', 'NEAR', 'LATER', 'OPTIONAL', 'BLOCKED')
        );
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE work_candidates DROP CONSTRAINT ck_work_candidates_priority;
        ALTER TABLE work_candidates ADD CONSTRAINT ck_work_candidates_priority CHECK (
            priority IN ('low', 'medium', 'high', 'urgent')
        );
    """)
