"""Life Candidate Learning Signals -- the missing stage in the founder-learning pipeline the
founder explicitly directed be built correctly rather than skipped: "conversation/source event
-> preserved source reference -> candidate learning signal -> evidence/classification stage ->
derived founder knowledge only when justified." See docs/LIFE_FOUNDER_MEMORY.md's own
"Candidate learning signals" section for the full architecture.

Standing principle this migration exists to enforce structurally, not just by convention:
SIGNAL PRODUCER != TRUTH WRITER. `app.context.resolver` (already live in `app/routers/chat.py`,
"purely observational" by its own code comment) is a signal producer -- a rule-based heuristic
over message text, with its own documented false-positive/negative trade-offs (its correction-
marker vocabulary includes very common short words like "nej "/"fel,"). Wiring its output
directly into `founder_memory_notes` -- a table other code will eventually treat as trusted
founder truth -- would flood that trust boundary with noise. `candidate_learning_signals` is
instead a NEW, explicitly untrusted staging table: nothing here carries `authority`/`basis`
columns at all, because a candidate signal is not yet a claim about the world, only a claim
that "something happened that might be worth a human or reviewed process's attention." The
ONLY path from a candidate signal to actual founder knowledge is
`app.founder_memory_signals.promote_candidate_signal()`, which ALWAYS requires an explicit,
caller-supplied `authority`/`basis` for the `founder_memory_notes` row it creates -- the
signal's own classifier confidence is never silently copied into that authority.

`source_message_id` references `messages(id)` directly, not a composite `(id, owner_id)` FK --
`messages` itself has no `owner_id` column (ownership flows through
`conversation_id -> conversations.user_id`), matching the existing precedent everywhere else in
this codebase that references a message without a composite ownership FK. The write path
(`app/routers/chat.py`, gated end-to-end by `Depends(require_founder)`) is the same
authenticated request context that already established ownership before calling
`resolve_context()`; this migration does not duplicate that verification with a new trigger."""

from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE candidate_learning_signals (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_type varchar(32) NOT NULL DEFAULT 'message',
            source_message_id uuid REFERENCES messages(id) ON DELETE CASCADE,
            signal_kind varchar(32) NOT NULL,
            classifier_strategy varchar(64) NOT NULL DEFAULT 'unknown',
            classifier_confidence varchar(16) NOT NULL DEFAULT 'unknown',
            classifier_reasoning text,
            status varchar(24) NOT NULL DEFAULT 'unreviewed',
            promoted_to_note_id uuid,
            dismissed_reason text,
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            idempotency_key varchar(128) NOT NULL,
            observed_at timestamp NOT NULL DEFAULT now(),
            created_at timestamp NOT NULL DEFAULT now(),
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_candidate_learning_signals_id_owner UNIQUE (id, owner_id),
            CONSTRAINT uq_candidate_learning_signals_idem UNIQUE (owner_id, idempotency_key),
            CONSTRAINT fk_candidate_learning_signals_promoted_note FOREIGN KEY (promoted_to_note_id, owner_id)
                REFERENCES founder_memory_notes (id, owner_id) ON DELETE SET NULL,
            CONSTRAINT ck_candidate_learning_signals_source_type CHECK (source_type IN ('message')),
            CONSTRAINT ck_candidate_learning_signals_signal_kind CHECK (signal_kind IN (
                'explicit_memory_candidate', 'correction_candidate', 'idea_candidate', 'unknown'
            )),
            CONSTRAINT ck_candidate_learning_signals_confidence CHECK (classifier_confidence IN (
                'high', 'medium', 'low', 'unknown'
            )),
            CONSTRAINT ck_candidate_learning_signals_status CHECK (status IN ('unreviewed', 'promoted', 'dismissed')),
            -- The structural half of "SIGNAL PRODUCER != TRUTH WRITER": a signal can only be
            -- marked promoted in the SAME transaction that actually points at the real
            -- founder_memory_notes row promote_candidate_signal() created -- never a status
            -- flip with no real, evidence-backed founder_memory_notes row behind it.
            CONSTRAINT ck_candidate_learning_signals_promoted_requires_note CHECK (
                status <> 'promoted' OR promoted_to_note_id IS NOT NULL
            ),
            CONSTRAINT ck_candidate_learning_signals_provenance CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE INDEX ix_candidate_learning_signals_owner_status ON candidate_learning_signals(owner_id, status);
        CREATE INDEX ix_candidate_learning_signals_message ON candidate_learning_signals(source_message_id);
    """)

    op.execute("""
        ALTER TABLE candidate_learning_signals ENABLE ROW LEVEL SECURITY;
        ALTER TABLE candidate_learning_signals FORCE ROW LEVEL SECURITY;
        CREATE POLICY candidate_learning_signals_isolation ON candidate_learning_signals
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    op.execute("""
        CREATE FUNCTION erase_own_candidate_learning_signal_children() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE v_owner_id uuid;
        BEGIN
            v_owner_id := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner_id IS NULL THEN
                RAISE EXCEPTION 'erase_own_candidate_learning_signal_children requires an authenticated app.current_user_id session context.';
            END IF;
            DELETE FROM public.candidate_learning_signals WHERE owner_id = v_owner_id;
        END;
        $$;
        REVOKE ALL ON FUNCTION erase_own_candidate_learning_signal_children() FROM PUBLIC;
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION erase_own_candidate_learning_signal_children();")
    op.execute("DROP TABLE candidate_learning_signals;")
