"""S1B, EXPAND phase — `messages.sequence_number` (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md
§4.9 "Meddelandeordning — riktig ordinal, inte bara `created_at`" and §8's S1B line).

WHY THIS EXISTS. `messages` has only ever had `created_at` (a timezone-less `timestamp`, set
client-side by SQLAlchemy's `datetime.utcnow` default — see app/models/conversation.py). Two
messages written inside the same microsecond, or across a clock that isn't monotonic, cannot
be ordered against each other at all: `ORDER BY created_at` is not a total order, and every
consumer in the codebase today (app/routers/conversations.py's transcript, app/routers/chat.py's
`history` window feeding both the provider prompt AND app/context/resolver.py) silently depends
on it being one. S1C (`message_source_units`) and S2 (`conversation_segments`, whose
`start_message_id`/`end_message_id` boundaries are only meaningful against a total order)
cannot be built on that — which is exactly why §8 puts S1B before them and marks it independent
of S1A.

EXPAND / DUAL-WRITE / BACKFILL / VERIFY / CONTRACT — this migration is the EXPAND step only.
§4.8's "Fasad migrationsplan" is explicit that `SET NOT NULL` cannot live in the same migration
that adds the column (the historical backfill has to run first, and a real backfill does not fit
inside a migration's transaction). So:

  * THIS migration (0030): nullable column, positive-value CHECK, the per-conversation
    uniqueness index, the assignment trigger (so every NEW message is numbered from this
    moment on — the "dual-write" half), and the immutability trigger.
  * app/rag/message_sequence_backfill.py + the `message_sequence_backfill` MainAI job type:
    the durable historical backfill, run as a real `mainai_jobs` row on the existing worker
    (§6.12's "no ad-hoc parallel mechanism" — this reuses the job runtime built in PR #36
    rather than inventing a second one).
  * A LATER, separate migration (not this one): `SET NOT NULL` + promoting the partial unique
    index to a real `UNIQUE (conversation_id, sequence_number)` constraint — only after
    `count_unsequenced_messages()` reports 0 against real production data.

WHY A TRIGGER AND NOT APPLICATION CODE. Assignment is a database trigger, not a line in
app/routers/chat.py, for the same reason migration 0029 chose a trigger over "application code
only ever increments": a numbering rule that lives in one writer is only as good as every
FUTURE writer remembering it. There are already three distinct INSERT paths into `messages`
(the user message, the assistant success row, the assistant failure row) and S1C/S2 will add
more. A `BEFORE INSERT` trigger makes "every message in a conversation carries a unique,
gapless-from-1, monotonically-assigned ordinal" a property of the TABLE, which no future
writer — or backfill, or test fixture — can accidentally opt out of.

THE ASSIGNMENT FORMULA, and why it is not simply `max + 1`.
`GREATEST(COALESCE(max(sequence_number), 0), count(*)) + 1`, computed over the whole
conversation. The `max` term alone is wrong during the window this migration deliberately
opens: between deploy and the completion of the historical backfill, a conversation can hold
old rows with `sequence_number IS NULL` alongside newly-inserted numbered ones. With `max`
alone the first new message in an untouched 12-message conversation would be numbered 1, and
the backfill would then have nowhere to put those 12 historical messages without either
colliding or rewriting an already-issued ordinal (which the immutability trigger below
forbids, on purpose).

The `count(*)` term closes that exactly. Let `N` be the number of still-unnumbered rows in a
conversation at the moment the trigger fires. The formula yields at least `numbered + N + 1`,
which is strictly greater than `N` — so every trigger-assigned number is strictly above the
range `1..N` the backfill will later hand out. `N` never grows (a new row is always numbered
by this trigger; a row only leaves the unnumbered set by being numbered or deleted), so the
`N` the backfill actually sees is <= every earlier assignment's `N`. Hence no trigger-assigned
ordinal can ever collide with a backfill-assigned one — proven by the invariant, and ALSO
enforced independently by `uq_messages_conversation_sequence_number` below and re-checked
defensively by the backfill itself before it writes (see that module's `_conversation_state`).
The `max` term remains necessary for the ordinary post-backfill case where a message has been
deleted (app/routers/conversations.py's `delete_conversation`, app/rag/account_erasure.py):
gaps are allowed, reuse of a retired ordinal is not.

WHY THE ADVISORY LOCK. Read-then-write under READ COMMITTED is a classic TOCTOU: two
concurrent inserts into the SAME conversation would both read the same `max`/`count` and both
compute the same next value. `pg_advisory_xact_lock` serializes ONLY same-conversation inserts
(different conversations hash to different keys and never wait on each other), is released
automatically at transaction end (no leak on rollback or crash), and is held for the duration
of a single INSERT flush — app/routers/chat.py's message inserts flush at commit time, so the
hold is microseconds, not the length of a provider call. The same key is taken by the backfill
(app/rag/message_sequence_backfill.py) so a backfill and a live insert into the same
conversation can never interleave. Key namespace `72197002` is deliberately adjacent to, and
distinct from, `72197001` (backend/scripts/s1a_privilege_policy.py's
`PRIVILEGE_BOOT_ADVISORY_LOCK_KEY` / app/rls.py's copy of it) so the two never collide.
A hash collision between two different conversation ids costs a little needless serialization
and is never a correctness problem. Deadlock is not reachable from any code path in this
codebase (no transaction inserts messages into two different conversations), and if a future
one did, Postgres's own deadlock detector aborts one side — fail-closed, never corruption.

WHY IMMUTABILITY IS ENFORCED TOO. `messages_deny_sequence_number_rewrite` rejects any UPDATE
that changes an already-assigned `sequence_number` (including back to NULL) or that moves a
message to a different `conversation_id`. This is the founder's standing "derivatives/versions/
audit metadata must never be destroyed" rule applied at the only place it can actually be
guaranteed: an ordinal that S1C's `message_source_units` and S2's segment boundaries will
reference must not be silently renumbered afterwards, and an ordinal that means "position
within conversation X" stops meaning anything if the row can be moved to conversation Y.
NULL -> value is explicitly allowed — that is the backfill's one legitimate transition.

WHAT `downgrade()` COSTS, stated rather than discovered later. Dropping the column discards
every ordinal assigned so far. That is acceptable TODAY and only today: nothing yet references
a `sequence_number` (S1C's `message_source_units` and S2's segment boundaries are the first
things that will), and the numbering is fully re-derivable — the backfill is deterministic on
`(created_at, id)` and re-running it reproduces the identical assignment, which
tests/backend/test_message_sequence.py proves directly. Once S1C ships, a downgrade past this
migration stops being a reversible operation and becomes a data-loss event; whoever writes the
CONTRACT migration should say so there too.

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-07
"""

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE messages ADD COLUMN sequence_number integer;

        ALTER TABLE messages
            ADD CONSTRAINT ck_messages_sequence_number_positive
            CHECK (sequence_number IS NULL OR sequence_number >= 1);

        -- `messages` has had NO index on conversation_id since the baseline schema (0001) --
        -- every read path has been a sequential scan filtered by conversation. The trigger
        -- below and the backfill both aggregate per conversation on every insert, so this
        -- index is a direct requirement of this migration, not an opportunistic addition.
        CREATE INDEX ix_messages_conversation_id ON messages (conversation_id);

        -- Partial (WHERE sequence_number IS NOT NULL) so it can be created NOW, while every
        -- existing row is still NULL, and still give the full uniqueness guarantee for every
        -- row that actually has an ordinal. The CONTRACT migration later replaces this with a
        -- plain UNIQUE constraint once NOT NULL holds.
        CREATE UNIQUE INDEX uq_messages_conversation_sequence_number
            ON messages (conversation_id, sequence_number)
            WHERE sequence_number IS NOT NULL;
    """)

    # search_path=pg_catalog (matching migration 0027's functions) means every application
    # object must be schema-qualified explicitly -- `public.messages` below, not `messages`.
    # Neither function is SECURITY DEFINER: both only ever touch rows in the very conversation
    # the caller is already writing to, so they need no privilege the writer doesn't already
    # hold. (`messages` carries no RLS policy of its own -- access is gated through the
    # RLS-protected `conversations` row by every router, see app/routers/conversations.py --
    # so the aggregate below is not RLS-filtered and the count is always the true one.)
    op.execute("""
        CREATE FUNCTION messages_assign_sequence_number() RETURNS trigger AS $$
        DECLARE
            next_sequence integer;
        BEGIN
            IF NEW.sequence_number IS NOT NULL THEN
                RETURN NEW;
            END IF;

            PERFORM pg_catalog.pg_advisory_xact_lock(72197002, pg_catalog.hashtext(NEW.conversation_id::text));

            SELECT (GREATEST(COALESCE(max(m.sequence_number), 0), count(*)) + 1)::integer
              INTO next_sequence
              FROM public.messages m
             WHERE m.conversation_id = NEW.conversation_id;

            NEW.sequence_number := next_sequence;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql SET search_path = pg_catalog;

        -- Same rule migrations 0019 and 0027 apply to every function they create: PUBLIC gets
        -- nothing. Revoking EXECUTE does NOT stop the trigger from firing — Postgres invokes a
        -- trigger function as part of the DML statement itself and never checks the writer's
        -- EXECUTE privilege for it (see app/rls.py's _MAINAI_JOB_FUNCTION_SPECS comment) — it
        -- only removes the ability to CALL these directly, which nothing legitimately does.
        -- Deliberately no GRANT/REVOKE naming `mainai_app`: that role may not exist yet on a
        -- bare database at migration time (migration 0019's module docstring documents the
        -- portability bug that caused), and it needs no grant here anyway.
        REVOKE ALL ON FUNCTION messages_assign_sequence_number() FROM PUBLIC;

        CREATE TRIGGER messages_assign_sequence_number_trigger
            BEFORE INSERT ON messages
            FOR EACH ROW
            EXECUTE FUNCTION messages_assign_sequence_number();

        CREATE FUNCTION messages_deny_sequence_number_rewrite() RETURNS trigger AS $$
        BEGIN
            IF OLD.sequence_number IS NOT NULL
               AND NEW.sequence_number IS DISTINCT FROM OLD.sequence_number THEN
                RAISE EXCEPTION
                    'messages.sequence_number is immutable once assigned (message %, % -> %)',
                    OLD.id, OLD.sequence_number, NEW.sequence_number;
            END IF;
            IF NEW.conversation_id IS DISTINCT FROM OLD.conversation_id THEN
                RAISE EXCEPTION
                    'messages.conversation_id is immutable (message %, % -> %) -- sequence_number is only meaningful within one conversation',
                    OLD.id, OLD.conversation_id, NEW.conversation_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql SET search_path = pg_catalog;

        REVOKE ALL ON FUNCTION messages_deny_sequence_number_rewrite() FROM PUBLIC;

        CREATE TRIGGER messages_deny_sequence_number_rewrite_trigger
            BEFORE UPDATE ON messages
            FOR EACH ROW
            EXECUTE FUNCTION messages_deny_sequence_number_rewrite();
    """)


def downgrade() -> None:
    op.execute("""
        DROP TRIGGER IF EXISTS messages_deny_sequence_number_rewrite_trigger ON messages;
        DROP TRIGGER IF EXISTS messages_assign_sequence_number_trigger ON messages;
        DROP FUNCTION IF EXISTS messages_deny_sequence_number_rewrite();
        DROP FUNCTION IF EXISTS messages_assign_sequence_number();

        DROP INDEX IF EXISTS uq_messages_conversation_sequence_number;
        DROP INDEX IF EXISTS ix_messages_conversation_id;

        ALTER TABLE messages DROP CONSTRAINT IF EXISTS ck_messages_sequence_number_positive;
        ALTER TABLE messages DROP COLUMN IF EXISTS sequence_number;
    """)
