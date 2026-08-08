"""Owner-scoped Row-Level Security for `messages` (docs/AUTH_THREAT_MODEL.md, app/rls.py).

WHY THIS EXISTS. `messages` is the last table holding directly personal, owner-specific
content that has never had an RLS policy of its own. It was flagged, not fixed, during S1B
(docs/BRANCH_REGISTRY.md's Pass 42, "Känd, kvarstående risk"), with the explicit note that
giving it RLS is a trust-boundary change deserving its own branch and its own review rather
than being folded into an unrelated diff. This migration is that change and nothing else.

Until now, isolation of a founder's message history rested entirely on a convention: every
router looks up the RLS-protected `conversations` row FIRST and only then touches `messages`
keyed by that conversation's id. That convention is, as far as this session could verify,
correctly followed by all five DB paths that exist today (app/routers/chat.py,
app/routers/conversations.py, app/rag/account_export.py, app/rag/account_erasure.py,
app/rag/message_sequence_backfill.py). The problem is not that it is currently broken — it is
that it is a property of five call sites rather than a property of the table, so it is only as
good as every FUTURE writer remembering it. That is precisely the argument migration 0030 made
for making sequence-number assignment a trigger instead of a line in `chat.py`, and migration
0027 made for the append-only job tables. The same argument applies here, to the one table
where getting it wrong leaks one person's private conversation to another account.

A single forgotten `JOIN conversations` in a future query — S1C's `message_source_units`
backfill and S2's segmentation passes are both coming, and both scan `messages` in bulk — is
today a silent cross-owner read. After this migration it returns zero rows instead. Failing
closed and empty is a bug; failing open and leaking is an incident.

DERIVED OWNERSHIP, NOT A DENORMALIZED `owner_id`. Every other policy in app/rls.py compares a
literal `owner_id`/`user_id` column on the table itself. `messages` has no such column, and
this migration deliberately does NOT add one. Two reasons, in order of importance:

  1. A denormalized `messages.owner_id` would be a SECOND source of truth for a fact that
     `conversations.user_id` already states exactly once. Two copies can drift; one cannot.
     Keeping ownership derived means there is no state in which a message's owner column
     disagrees with its conversation's owner — not after a bug, not after a partial backfill,
     not after a restore. The message's owner is not merely equal to the conversation's owner,
     it IS the conversation's owner; a column would encode a derivation as data.
  2. Adding the column would itself require the full EXPAND/BACKFILL/CONTRACT cycle (nullable
     column, dual-write trigger, a durable historical backfill job, then NOT NULL) — a second
     migration chain of exactly the shape S1B is still in the middle of, and one that could
     not be completed without a production backfill run. A derived policy is correct the
     instant it is created, on every row that already exists, with nothing to backfill and
     nothing gated on production access.

The cost is a subquery in the policy instead of a column comparison. That cost was measured
rather than assumed — see the next section.

THE POLICY EXPRESSION, AND WHY THIS FORM. Two semantically identical formulations were
benchmarked on a local Postgres 16 with 240 000 messages across 4 000 conversations and 20
owners, warm cache, four repetitions each:

    A. correlated:   EXISTS (SELECT 1 FROM conversations c
                             WHERE c.id = messages.conversation_id AND c.user_id = <uid>)
    B. uncorrelated: conversation_id IN (SELECT c.id FROM conversations c
                                         WHERE c.user_id = <uid>)

                                    no RLS      A (EXISTS)    B (IN)
    single-conversation transcript   ~0.44 ms     ~0.65 ms    ~0.65 ms
    owner-wide scan (12 000 rows)     ~22 ms       ~46 ms      ~26 ms

Postgres compiles both into a hashed SubPlan, so neither is evaluated per row in the naive
sense — but form B plans consistently at the no-RLS baseline on the bulk owner-wide scans,
while form A costs roughly double. Bulk owner-wide scans are exactly the shape
app/rag/message_sequence_backfill.py and app/rag/account_export.py use, and exactly the shape
S1C and S2 will add more of. Form B is therefore what this migration installs. On the
single-conversation read the two are indistinguishable.

FAIL-CLOSED IN EVERY DIRECTION.
  * No `app.current_user_id` set (a raw admin connection, or a pooled connection whose
    SET LOCAL was reverted by a mid-request commit): `current_setting(..., true)` yields NULL,
    NULLIF turns the '' quirk into NULL too, the subquery selects nothing, and the policy
    matches no row. Default-deny, identical to every other policy in app/rls.py — the NULLIF
    guard is copied from there deliberately rather than reinvented.
  * `conversation_id` NULL: it is NOT NULL in the schema (migration 0001) so this cannot
    arise, but `NULL IN (...)` evaluates to NULL, which is not TRUE, so the row would be
    denied rather than allowed even if that ever changed.
  * Writing a message into someone else's conversation: WITH CHECK rejects the INSERT
    outright. This is the direction that had NO database-level defense at all before today.
  * `conversations` carries FORCE RLS itself, so the subquery is additionally filtered by
    `conversations_isolation`. Both predicates say the same thing, so this is redundancy, not
    a second rule that could disagree. It does mean that if `conversations_isolation` were
    ever dropped, `messages` would go dark rather than open — an availability failure, not a
    security one, and app/rls.py's boot-time self-heal loop recreates any missing policy.
  * The policy's subquery requires the runtime role to hold SELECT on `conversations`, which
    it already does and needs anyway to read a conversation at all.

WHY THIS POLICY DOES NOT PIN `search_path`, unlike every function migrations 0019/0027/0030
create. A reviewer used to that discipline should expect the question. The reason those
functions must pin it is that a plpgsql function BODY is stored as text and its relation names
are resolved at CALL time, so an attacker-controlled schema earlier in `search_path` (Postgres
always checks the session's temp schema first) could shadow `public.messages`. A policy
expression is not text: `CREATE POLICY` parses it once and stores a node tree in which
`conversations` is already resolved to that table's OID. It is therefore immune to
search_path shadowing by construction, and there is nothing to pin. Verified rather than
assumed — renaming `conversations` makes `pg_policies.qual` deparse to the NEW name, which is
only possible if the stored reference is the OID and not the original text.

INTERACTION WITH MIGRATION 0030'S ASSIGNMENT TRIGGER — the one genuinely subtle consequence,
and the reason this migration is not a two-line change.

`messages_assign_sequence_number()` computes `GREATEST(COALESCE(max(sequence_number), 0),
count(*)) + 1` by aggregating over `public.messages` for the conversation being written to.
It is NOT SECURITY DEFINER, so it runs with the invoking role's privileges — which means that
as of this migration that aggregate becomes RLS-filtered, where before it was not. Migration
0030's own comment says so in as many words ("`messages` carries no RLS policy of its own ...
so the aggregate below is not RLS-filtered and the count is always the true one"); that
sentence is corrected in place by this migration's companion edit, because it stops being true
here.

The count is nonetheless still the true one, for a new and stronger reason. The policy's unit
of visibility is the CONVERSATION: for any given conversation, either every one of its message
rows satisfies the policy or none of them do, because they all share one `conversation_id` and
therefore one owner. The trigger only ever aggregates within the single conversation
`NEW.conversation_id`, and the INSERT that fired it must itself have passed WITH CHECK on that
same conversation — so the inserting session provably has visibility of that conversation, and
therefore of ALL of its existing messages. An insert by a session that does NOT own the
conversation never reaches the aggregate at all: the row is rejected. A superuser/admin
connection bypasses RLS entirely and sees everything, as before.

So the aggregate is complete in every case where it runs, and S1B's collision-freedom proof is
preserved exactly. This is not left as reasoning: it is asserted directly by
tests/backend/test_messages_rls.py, which numbers messages in one owner's conversation while a
second owner holds a larger conversation of their own and verifies the ordinal is neither
restarted at 1 nor inflated by the other owner's rows.

WHY `ix_conversations_user_id` IS PART OF THIS MIGRATION. The policy subquery filters
`conversations` by `user_id` on every statement that touches `messages`, and `conversations`
has had NO index on `user_id` since the baseline schema (0001) — only the primary key on `id`.
This index is a direct requirement of the predicate being introduced here, in exactly the sense
migration 0030 argued for `ix_messages_conversation_id`, not an opportunistic "while I was
here" addition. (It also happens to help `conversations_isolation` itself, which has filtered
on the same unindexed column since 0001 — a genuine pre-existing inefficiency that this
migration is not otherwise touching.)

WHAT THIS MIGRATION DOES NOT DO.
  * It does not add `messages.owner_id`, and no backfill of any kind is involved.
  * It does not touch `sequence_number`, its nullability, its triggers, or the pending S1B
    CONTRACT migration. It is correct with the column fully NULL, fully populated, or anywhere
    in between, because owner isolation and message ordering are independent concerns.
  * It does not change any router, query, or API response. Every existing path already scoped
    itself through `conversations`; this migration makes the database enforce what they were
    already doing rather than asking them to do anything new.
  * It does not narrow `mainai_app`'s table privileges on `messages` (the pattern migration
    0027 applies to the append-only job tables). Message rows are legitimately updatable and
    deletable by the runtime role — the backfill numbers them, `delete_conversation` and
    account erasure remove them — so there is no excess privilege to revoke here.

DOWNGRADE. Dropping the policy returns `messages` to the pre-migration state exactly: the
table keeps every row, and access falls back to the router-level convention that governs it
today. Unlike migration 0030's downgrade (which destroys assigned ordinals), nothing is lost
here — this migration creates no data.

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-08
"""

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

# The single source of truth for the predicate, used for both USING and WITH CHECK below.
# Must stay character-identical to app/rls.py's POLICY_DEFINITIONS entry for `messages` --
# that module's boot-time self-heal loop recreates this policy by NAME if it ever goes
# missing, and would silently install a different rule if the two ever drifted. Guarded
# directly by tests/backend/test_rls_policy_registry.py, which compares every policy actually
# present in pg_policies against POLICY_DEFINITIONS after a real migration run.
_MESSAGES_ISOLATION_EXPR = (
    "conversation_id IN ("
    "SELECT c.id FROM conversations c "
    "WHERE c.user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid"
    ")"
)


def upgrade() -> None:
    # See the "WHY ix_conversations_user_id IS PART OF THIS MIGRATION" section above.
    op.execute("CREATE INDEX ix_conversations_user_id ON conversations (user_id);")

    # FORCE, not just ENABLE: the app connects as the role that owns these tables, and
    # Postgres exempts a table's owner from its own RLS unless FORCE is set. Same reasoning,
    # and same pairing, as every other table in app/rls.py's RLS_STATEMENTS.
    op.execute(
        f"""
        ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
        ALTER TABLE messages FORCE ROW LEVEL SECURITY;
        CREATE POLICY messages_isolation ON messages
            USING ({_MESSAGES_ISOLATION_EXPR})
            WITH CHECK ({_MESSAGES_ISOLATION_EXPR});
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS messages_isolation ON messages;
        ALTER TABLE messages NO FORCE ROW LEVEL SECURITY;
        ALTER TABLE messages DISABLE ROW LEVEL SECURITY;

        DROP INDEX IF EXISTS ix_conversations_user_id;
        """
    )
