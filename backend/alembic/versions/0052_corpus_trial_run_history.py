"""Life Corpus Trial Run History.

`docs/LIFE_CORPUS_TRIAL_HARNESS.md`'s own "Explicitly deferred" section named this as the
natural, small follow-up once a real trial gets closer: "a durable record of trial runs
(reusing the same authority/basis/provenance discipline as every other foundation here)."
`app.corpus_trial.harness.run_trial()` (PR #102) returns an in-memory `TrialReport` only --
nothing about a trial run survives past the Python process that ran it. This migration adds
exactly the durable record, nothing else: `run_trial()` itself is untouched, still pure.

Not a provenance CLAIM about the world (no `authority`/`basis` columns -- there is no founder
statement or inference being recorded here, just a fact about what a scoring run measured),
so this table does NOT reuse migration 0042's authority/basis vocabulary the way `capability_
records`/`founder_memory_notes`/`diagnosis_records` do. It IS an append-only execution/evidence
record, structurally, so it reuses THAT pattern instead:
`capability_observation_events`/`agent_work_assignment_events`'s deny-mutation trigger, not
`founder_memory_notes`/`diagnosis_records`'s mutable-with-narrowed-privileges pattern -- a
trial run, once scored, is a historical fact that never gets edited in place; a corrected or
re-run trial is simply a NEW row, same discipline as everything else in this mission."""

from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE corpus_trial_runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            corpus_label varchar(64) NOT NULL DEFAULT 'bootstrap',
            record_count integer NOT NULL,
            passed boolean NOT NULL,
            dimension_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
            violation_counts jsonb NOT NULL DEFAULT '{}'::jsonb,
            idempotency_key varchar(128) NOT NULL,
            run_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_corpus_trial_runs_idem UNIQUE (owner_id, idempotency_key),
            CONSTRAINT ck_corpus_trial_runs_record_count CHECK (record_count >= 0),
            CONSTRAINT ck_corpus_trial_runs_dimension_summary CHECK (jsonb_typeof(dimension_summary) = 'object'),
            CONSTRAINT ck_corpus_trial_runs_violation_counts CHECK (jsonb_typeof(violation_counts) = 'object'),
            CONSTRAINT ck_corpus_trial_runs_label CHECK (length(btrim(corpus_label)) > 0)
        );
        CREATE INDEX ix_corpus_trial_runs_owner_run_at ON corpus_trial_runs(owner_id, run_at);
    """)

    op.execute("""
        ALTER TABLE corpus_trial_runs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE corpus_trial_runs FORCE ROW LEVEL SECURITY;
        CREATE POLICY corpus_trial_runs_isolation ON corpus_trial_runs
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    op.execute("""
        CREATE FUNCTION corpus_trial_runs_deny_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND current_setting('app.corpus_trial_run_erasure_in_progress', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION '% is append-only: % is not permitted', TG_TABLE_NAME, TG_OP;
        END;
        $$;
        REVOKE ALL ON FUNCTION corpus_trial_runs_deny_mutation() FROM PUBLIC;
        CREATE TRIGGER trg_corpus_trial_runs_deny_mutation
            BEFORE UPDATE OR DELETE ON corpus_trial_runs
            FOR EACH ROW EXECUTE FUNCTION corpus_trial_runs_deny_mutation();
    """)

    op.execute("""
        CREATE FUNCTION erase_own_corpus_trial_run_children() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE v_owner_id uuid;
        BEGIN
            v_owner_id := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner_id IS NULL THEN
                RAISE EXCEPTION 'erase_own_corpus_trial_run_children requires an authenticated app.current_user_id session context.';
            END IF;
            PERFORM set_config('app.corpus_trial_run_erasure_in_progress', 'on', true);
            DELETE FROM public.corpus_trial_runs WHERE owner_id = v_owner_id;
        END;
        $$;
        REVOKE ALL ON FUNCTION erase_own_corpus_trial_run_children() FROM PUBLIC;
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION erase_own_corpus_trial_run_children();")
    op.execute("DROP TRIGGER trg_corpus_trial_runs_deny_mutation ON corpus_trial_runs;")
    op.execute("DROP FUNCTION corpus_trial_runs_deny_mutation();")
    op.execute("DROP TABLE corpus_trial_runs;")
