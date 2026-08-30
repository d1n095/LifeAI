"""Add entity-resolution columns to candidate_learning_signals -- the personal-language
resolution layer for the founder-defined "Personal Intent & Executive Reasoning" workstream
(docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md §1).

WHY EXTEND, NOT A NEW TABLE (design correction from that document's own §1.2 sketch, which
proposed a separate `conversational_interpretation_proposals` table mirroring `app.
project_entities.InterpretationProposal`): re-reading the actual schema before building
anything (per this codebase's own "verify before recommending" discipline) showed
`candidate_learning_signals` (migration 0053) already carries nearly every column that sketch
needed -- `source_message_id` (bare FK to `messages.id`, since `messages` itself has no
`owner_id` column -- ownership flows through `conversation_id -> conversations.user_id`,
matching this migration's own established precedent, not a new one), `classifier_strategy`/
`classifier_confidence`/`classifier_reasoning`, `status`, `provenance`. A parallel table would
have duplicated all of that. The only genuinely missing piece is WHICH existing entity a
signal's message is about -- so this migration adds exactly that, to the existing table,
rather than inventing a second "signal producer != truth writer" staging mechanism alongside
the one already built and already live-wired into app/routers/chat.py.

Deliberately loose typing for `resolved_entity_type` (a plain varchar, no CHECK constraint
against app.active_context.service.SUPPORTED_TYPES' closed registry): tightening it to that
shared vocabulary is a reasonable follow-up once real resolver usage exists to validate
against, not before. Resolving an entity reference is the SAME epistemic status as
`classifier_confidence` -- a fact about the resolver's own guess, never a truth claim until
`promote_candidate_signal()` (unchanged by this migration) turns it into one."""

from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE candidate_learning_signals
            ADD COLUMN resolved_entity_type varchar(32),
            ADD COLUMN resolved_entity_id uuid,
            ADD COLUMN resolution_reasoning text;
        CREATE INDEX ix_candidate_learning_signals_resolved_entity
            ON candidate_learning_signals(resolved_entity_type, resolved_entity_id);
    """)


def downgrade() -> None:
    op.execute("""
        DROP INDEX ix_candidate_learning_signals_resolved_entity;
        ALTER TABLE candidate_learning_signals
            DROP COLUMN resolved_entity_type,
            DROP COLUMN resolved_entity_id,
            DROP COLUMN resolution_reasoning;
    """)
