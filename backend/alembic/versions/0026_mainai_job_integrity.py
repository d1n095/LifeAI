"""MainAI Runtime Truthfulness and Durable Job Foundation — correction round: close two gaps
an independent founder review found in migration 0025.

1. Composite owner FK (child rows can no longer point at a different owner's job). Before
   this migration, `mainai_job_events`/`mainai_job_proposals` had two *independent* FKs
   (`job_id -> mainai_jobs.id`, `owner_id -> users.id`) with nothing tying them together.
   RLS only checks a row's own `owner_id`, so owner A — knowing owner B's `job_id` — could in
   principle insert `mainai_job_events(job_id=<B's job>, owner_id=A)`: a row visible to A,
   but logically attached to B's job. `mainai_jobs` gets a `UNIQUE(id, owner_id)` so the
   child tables can carry a real composite `FOREIGN KEY (job_id, owner_id) REFERENCES
   mainai_jobs (id, owner_id)` — a job/owner pair that doesn't actually match a real job row
   is now a constraint violation, not just an RLS-invisible-but-still-inserted row.

2. Real DB-level append-only enforcement. Migration 0025's own docstring admitted the event
   log was "append-only by convention and tests, not a DB-level immutability trigger" — not
   good enough for something meant to be independent evidence of what MainAI actually did.
   `mainai_job_events` gets a BEFORE UPDATE/DELETE trigger that always denies UPDATE (no
   exceptions, ever) and denies DELETE unless the deleting transaction has explicitly set
   `app.mainai_job_erasure_in_progress = 'on'` — the only thing that ever sets that flag is
   `erase_mainai_job_children_for_owner()` below, a narrow SECURITY DEFINER function that is
   the *only* way these rows are ever removed (account erasure). `mainai_job_proposals` gets
   the same DELETE gate plus a narrower UPDATE guard: the only mutation ever permitted is the
   single `status: 'proposed' -> 'dismissed'` transition with every other column unchanged —
   never `dismissed -> proposed`, never `proposal_text`/`source_document_id`/`job_id`/
   `owner_id` edited after the fact.

`mainai_app`'s blanket `ALL PRIVILEGES` grant (from `scripts/ensure_app_role.py`, re-applied
unconditionally on *every* container boot, before Alembic runs — see that script's own
docstring and the Pass 12 incident in docs/BRANCH_REGISTRY.md for why this matters) would
silently re-grant UPDATE/DELETE/TRUNCATE back onto these tables after any restart if the
REVOKE below were only ever applied once, at migration time. `app/rls.py`'s new
`apply_mainai_job_runtime_privileges()` re-asserts it on every boot, after `apply_rls()`,
exactly like `apply_rls()` itself already does for RLS policies — see app/main.py.

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-04
"""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE mainai_jobs ADD CONSTRAINT uq_mainai_jobs_id_owner_id UNIQUE (id, owner_id);

        ALTER TABLE mainai_job_events
            ADD CONSTRAINT fk_mainai_job_events_job_owner
            FOREIGN KEY (job_id, owner_id) REFERENCES mainai_jobs (id, owner_id) ON DELETE CASCADE;

        ALTER TABLE mainai_job_proposals
            ADD CONSTRAINT fk_mainai_job_proposals_job_owner
            FOREIGN KEY (job_id, owner_id) REFERENCES mainai_jobs (id, owner_id) ON DELETE CASCADE;
    """)

    op.execute("""
        CREATE FUNCTION mainai_job_events_deny_mutation() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF current_setting('app.mainai_job_erasure_in_progress', true) = 'on' THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'mainai_job_events is append-only: DELETE is only permitted through an authorized owner erasure.';
            END IF;
            RAISE EXCEPTION 'mainai_job_events is append-only: UPDATE is never permitted.';
        END;
        $$;

        CREATE TRIGGER trg_mainai_job_events_deny_mutation
            BEFORE UPDATE OR DELETE ON mainai_job_events
            FOR EACH ROW EXECUTE FUNCTION mainai_job_events_deny_mutation();
    """)

    op.execute("""
        CREATE FUNCTION mainai_job_proposals_guard_mutation() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF current_setting('app.mainai_job_erasure_in_progress', true) = 'on' THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'mainai_job_proposals rows can only be deleted through an authorized owner erasure.';
            END IF;

            -- TG_OP = 'UPDATE': the ONLY permitted mutation, ever, is proposed -> dismissed
            -- with every other column byte-for-byte unchanged. No reversal, no edits.
            IF OLD.status = 'proposed' AND NEW.status = 'dismissed'
                AND NEW.id = OLD.id
                AND NEW.job_id = OLD.job_id
                AND NEW.owner_id = OLD.owner_id
                AND NEW.source_document_id IS NOT DISTINCT FROM OLD.source_document_id
                AND NEW.source_chunk_id IS NOT DISTINCT FROM OLD.source_chunk_id
                AND NEW.proposal_type = OLD.proposal_type
                AND NEW.proposal_text = OLD.proposal_text
                AND NEW.created_at = OLD.created_at
            THEN
                RETURN NEW;
            END IF;

            RAISE EXCEPTION 'mainai_job_proposals rows are immutable except the single proposed -> dismissed status transition.';
        END;
        $$;

        CREATE TRIGGER trg_mainai_job_proposals_guard_mutation
            BEFORE UPDATE OR DELETE ON mainai_job_proposals
            FOR EACH ROW EXECUTE FUNCTION mainai_job_proposals_guard_mutation();
    """)

    # The only path that may ever remove mainai_job_events/mainai_job_proposals rows —
    # account erasure (see app/routers/account.py). Deletes both child tables for one owner
    # inside the SAME transaction as the rest of account deletion; mainai_jobs itself is
    # deleted separately by the caller afterward (it isn't append-only, mainai_app already
    # has ordinary DELETE on it — no function needed there).
    op.execute("""
        CREATE FUNCTION erase_mainai_job_children_for_owner(target_owner_id uuid) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            PERFORM set_config('app.mainai_job_erasure_in_progress', 'on', true);
            DELETE FROM public.mainai_job_proposals WHERE owner_id = target_owner_id;
            DELETE FROM public.mainai_job_events WHERE owner_id = target_owner_id;
        END;
        $$;
    """)

    op.execute("""
        REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON mainai_job_events FROM mainai_app;
        REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON mainai_job_proposals FROM mainai_app;
        GRANT EXECUTE ON FUNCTION erase_mainai_job_children_for_owner(uuid) TO mainai_app;
    """)


def downgrade() -> None:
    op.execute("""
        REVOKE EXECUTE ON FUNCTION erase_mainai_job_children_for_owner(uuid) FROM mainai_app;
        GRANT ALL PRIVILEGES ON mainai_job_events TO mainai_app;
        GRANT ALL PRIVILEGES ON mainai_job_proposals TO mainai_app;

        DROP FUNCTION erase_mainai_job_children_for_owner(uuid);

        DROP TRIGGER trg_mainai_job_proposals_guard_mutation ON mainai_job_proposals;
        DROP FUNCTION mainai_job_proposals_guard_mutation();

        DROP TRIGGER trg_mainai_job_events_deny_mutation ON mainai_job_events;
        DROP FUNCTION mainai_job_events_deny_mutation();

        ALTER TABLE mainai_job_proposals DROP CONSTRAINT fk_mainai_job_proposals_job_owner;
        ALTER TABLE mainai_job_events DROP CONSTRAINT fk_mainai_job_events_job_owner;
        ALTER TABLE mainai_jobs DROP CONSTRAINT uq_mainai_jobs_id_owner_id;
    """)
