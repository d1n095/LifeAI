"""MainAI Runtime Truthfulness and Durable Job Foundation — correction round: close gaps an
independent founder review found in `0026_mainai_jobs.py`, PLUS a second review round that
found this migration's own first draft had a critical cross-owner deletion bug and a
portability bug. Both rounds are folded into this single migration's final shape (no separate
follow-up migration for what is really still "make this migration correct before anything
depends on it" — this branch had not been opened as a PR yet at the time, so there was no
reviewed history to preserve by leaving the bug in place and patching it in a later revision).

1. Composite owner FK (child rows can no longer point at a different owner's job). Before
   this migration, `mainai_job_events`/`mainai_job_proposals` had two *independent* FKs
   (`job_id -> mainai_jobs.id`, `owner_id -> users.id`) with nothing tying them together.
   RLS only checks a row's own `owner_id`, so owner A — knowing owner B's `job_id` — could in
   principle insert `mainai_job_events(job_id=<B's job>, owner_id=A)`: a row visible to A,
   but logically attached to B's job. `mainai_jobs` gets a `UNIQUE(id, owner_id)` so the
   child tables can carry a real composite `FOREIGN KEY (job_id, owner_id) REFERENCES
   mainai_jobs (id, owner_id)` — a job/owner pair that doesn't actually match a real job row
   is now a constraint violation, not just an RLS-invisible-but-still-inserted row.

2. Real DB-level append-only enforcement. `0026_mainai_jobs.py`'s own docstring admitted the
   event log was "append-only by convention and tests, not a DB-level immutability trigger" — not
   good enough for something meant to be independent evidence of what MainAI actually did.
   `mainai_job_events` gets a BEFORE UPDATE/DELETE trigger that always denies UPDATE (no
   exceptions, ever) and denies DELETE unless the deleting transaction has explicitly set
   `app.mainai_job_erasure_in_progress = 'on'` — the only thing that ever sets that flag is
   `erase_own_mainai_job_children()` below. `mainai_job_proposals` gets the same DELETE gate
   plus a narrower UPDATE guard: the only mutation ever permitted is the single
   `status: 'proposed' -> 'dismissed'` transition with every other column unchanged — never
   `dismissed -> proposed`, never `proposal_text`/`source_document_id`/`job_id`/`owner_id`
   edited after the fact.

3. `erase_own_mainai_job_children()` — the ONLY legitimate deletion path — takes NO
   caller-supplied owner argument. The first draft of this migration defined
   `erase_mainai_job_children_for_owner(target_owner_id uuid)`, SECURITY DEFINER, granted
   EXECUTE to `mainai_app`, and deleted `WHERE owner_id = target_owner_id` — without ever
   checking that `target_owner_id` matched the calling session's own
   `app.current_user_id`. Because the function is SECURITY DEFINER, its DELETEs run with the
   function owner's privileges, not the caller's — RLS on the tables is therefore not a
   sufficient owner boundary on its own, and ANY authenticated session could have called
   `SELECT erase_mainai_job_children_for_owner('<some other user's uuid>')` and erased that
   other user's event/proposal history. Fixed by deriving the owner from
   `current_setting('app.current_user_id', true)` INSIDE the function (the same session GUC
   RLS itself trusts — see app/rls.py) instead of trusting a parameter, and denying outright
   if that setting is unset. No admin/cross-owner variant of this function exists: nothing in
   this codebase today has a legitimate reason to erase another owner's mainai job data
   outside that owner's own account-deletion flow, and building one with no real caller would
   be exactly the kind of speculative, unverified privileged surface this review round is
   about closing, not adding. If a real cross-owner maintenance need appears later, it gets
   its own function, its own review, and its own EXECUTE grant — never `mainai_app`.

4. This migration does NOT reference the `mainai_app` role at all (no GRANT/REVOKE naming it
   directly) — a second, independent portability bug in the first draft. `REVOKE ... FROM
   mainai_app` requires the role to already exist; a bare migration database (one that runs
   `alembic upgrade head` without first running `scripts/ensure_app_role.py` — exactly the
   scenario PR #31's own migrations are already careful about) would fail this migration with
   `role "mainai_app" does not exist`. What this migration DOES do, which needs no runtime
   role to exist: `REVOKE ALL ... FROM PUBLIC` on every function it creates (`PUBLIC` is a
   pseudo-role that always exists) and `ALTER FUNCTION ... OWNER TO CURRENT_USER` is
   unnecessary (the migration role already owns what it creates). The actual `mainai_app`
   grants (`SELECT`/`INSERT` on the two child tables, `EXECUTE` on
   `erase_own_mainai_job_children()`, and nothing else) live entirely in
   `app/rls.py`'s `apply_mainai_job_runtime_privileges()`, which now also VERIFIES the exact
   final state (signature, owner, `SECURITY DEFINER`, `search_path`, `PUBLIC` has no
   `EXECUTE`, `mainai_app`'s exact table grants) rather than just issuing REVOKE/GRANT and
   hoping — called from `app/main.py`'s startup, after Alembic has already run and after
   `ensure_app_role.py` has already provisioned the role, so `require_complete=True` (hard
   fail on any drift) is the correct default there.

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-04

INTEGRATION NOTE: originally authored as `revision = "0026"`, `down_revision = "0025"`
against `claude/mainai-job-runtime-foundation`'s own base numbering. Renumbered during
integration (`claude/mainai-job-runtime-integration`) to `0027`, `down_revision = "0026"`, to
follow this migration's sibling after ITS renumbering (`0025_mainai_jobs.py` ->
`0026_mainai_jobs.py`) — see that file's own integration note for the full chain. No SQL below
was changed.
"""

from alembic import op

revision = "0027"
down_revision = "0026"
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

        REVOKE ALL ON FUNCTION mainai_job_events_deny_mutation() FROM PUBLIC;

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

        REVOKE ALL ON FUNCTION mainai_job_proposals_guard_mutation() FROM PUBLIC;

        CREATE TRIGGER trg_mainai_job_proposals_guard_mutation
            BEFORE UPDATE OR DELETE ON mainai_job_proposals
            FOR EACH ROW EXECUTE FUNCTION mainai_job_proposals_guard_mutation();
    """)

    # The only path that may ever remove mainai_job_events/mainai_job_proposals rows —
    # account erasure (see app/routers/account.py). Takes NO owner argument: the owner is
    # derived from app.current_user_id (the same session GUC RLS itself trusts), never from a
    # caller-supplied value, so there is no parameter a compromised or buggy caller could set
    # to another owner's id. Denies outright if that GUC is unset (no ambient/anonymous
    # erasure). Deletes both child tables for the CALLING session's own owner inside the same
    # transaction as the rest of account deletion; mainai_jobs itself is deleted separately by
    # the caller afterward (it isn't append-only, mainai_app already has ordinary DELETE on
    # it — no function needed there).
    op.execute("""
        CREATE FUNCTION erase_own_mainai_job_children() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            v_owner_id uuid;
        BEGIN
            v_owner_id := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner_id IS NULL THEN
                RAISE EXCEPTION 'erase_own_mainai_job_children requires an authenticated app.current_user_id session context.';
            END IF;
            PERFORM set_config('app.mainai_job_erasure_in_progress', 'on', true);
            DELETE FROM public.mainai_job_proposals WHERE owner_id = v_owner_id;
            DELETE FROM public.mainai_job_events WHERE owner_id = v_owner_id;
        END;
        $$;

        REVOKE ALL ON FUNCTION erase_own_mainai_job_children() FROM PUBLIC;
    """)

    # Deliberately NOT here: no `GRANT`/`REVOKE ... TO/FROM mainai_app`. This migration must
    # apply cleanly on a bare database where that role doesn't exist yet — see this file's own
    # docstring. The runtime role's exact grants are applied and VERIFIED by
    # app/rls.py's apply_mainai_job_runtime_privileges(), called from app/main.py's startup
    # after both Alembic and scripts/ensure_app_role.py have already run.


def downgrade() -> None:
    op.execute("""
        DROP FUNCTION erase_own_mainai_job_children();

        DROP TRIGGER trg_mainai_job_proposals_guard_mutation ON mainai_job_proposals;
        DROP FUNCTION mainai_job_proposals_guard_mutation();

        DROP TRIGGER trg_mainai_job_events_deny_mutation ON mainai_job_events;
        DROP FUNCTION mainai_job_events_deny_mutation();

        ALTER TABLE mainai_job_proposals DROP CONSTRAINT fk_mainai_job_proposals_job_owner;
        ALTER TABLE mainai_job_events DROP CONSTRAINT fk_mainai_job_events_job_owner;
        ALTER TABLE mainai_jobs DROP CONSTRAINT uq_mainai_jobs_id_owner_id;
    """)
