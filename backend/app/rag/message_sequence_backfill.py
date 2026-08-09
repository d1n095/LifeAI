"""S1B historical backfill (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.9, §8's S1B line):
numbers the `messages` rows that predate migration 0030 (`sequence_number IS NULL`).

Deterministic, exactly as §4.9 specifies: within one conversation, the still-unnumbered rows
are ordered by `(created_at, id)` and numbered `1..N`. `id` is the tiebreaker precisely
because `created_at` alone is not a total order — that is the whole problem S1B exists to fix,
so the backfill must not reintroduce it. `id` is a UUID: for rows sharing a `created_at` the
resulting order is arbitrary but STABLE and REPRODUCIBLE (re-running against the same data
yields the identical numbering), which is the strongest guarantee available for history where
no better ordering signal was ever recorded. That limitation is real and documented rather
than papered over — for same-timestamp historical pairs the ordinal is a canonical order, not
a recovered one.

No AI, no provider, no network. This module is pure SQL over rows that already exist — the
"the system must keep working without AI" rule applied literally: numbering a founder's own
message history must never depend on a model being configured or reachable.

ATOMICITY AND CONCURRENCY. One conversation is one unit of work, numbered in a single
`UPDATE ... FROM (SELECT row_number() ...)` statement and committed on its own. That means:
  * A crash mid-run loses at most the in-flight conversation, and re-running simply finds it
    again (`sequence_number IS NULL` is itself the restart marker — no cursor is needed, and
    unlike app/rag/memory_source_backfill_run.py's dry-run mode there is nothing here that
    writes nothing and could therefore double-count on resume).
  * A conversation is never left half-numbered: the whole `1..N` assignment is one statement
    inside one transaction, so either every historical row in it gets an ordinal or none does.
  * The SAME `pg_advisory_xact_lock` key migration 0030's insert trigger takes is acquired
    first, so a live message arriving mid-backfill cannot interleave with the numbering of the
    conversation it belongs to. Whichever side wins the lock, the other sees a consistent
    state: an insert that goes first is numbered above the historical range (see migration
    0030's formula proof), and one that goes second sees the now-numbered rows.

FAIL-CLOSED CONFLICT HANDLING. Before writing, `_conversation_state()` re-derives the two
numbers the migration's invariant proof rests on (`unnumbered_count`, and the smallest
already-assigned ordinal) and refuses to number a conversation where an existing ordinal would
fall inside the `1..N` range this run is about to hand out. That state is unreachable if
migration 0030's trigger has been in place for every insert — it is checked anyway, because
the alternative to an explicit, counted, reported `conflict` outcome is a raw unique-violation
that aborts the run, and because a database that somehow reached that state is exactly the
case where guessing would be worst. A conflicted conversation is left completely untouched and
reported; it is never partially numbered and never renumbered.

OWNER SCOPING. `messages` has no `owner_id` column — a message belongs to whoever owns its
conversation. As of migration 0031 the database enforces that directly: `messages_isolation`
is an RLS policy whose predicate is "this row's `conversation_id` is one of the current user's
conversations". This module was written before that policy existed and needs no change because
of it, since it already respected the same boundary by hand: the candidate list is a query
over `conversations` filtered by `user_id == owner_id` on an already RLS-scoped session, and
every message-level statement is keyed by a `conversation_id` that came out of that list.

What changed is how many independent things have to fail before another owner's history could
be touched. There are now three: the explicit `user_id == owner_id` filter, the
`conversations_isolation` policy, and — new — `messages_isolation` on the message rows
themselves. Before 0031 the third did not exist, so a future statement here that forgot to key
itself by an owner-verified conversation would have silently reached across accounts; now it
returns zero rows instead. That is the difference between a bug and an incident, and it is
why the policy exists rather than this docstring being considered sufficient.
"""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.conversation import Conversation, Message

logger = logging.getLogger("mainai.message_sequence_backfill")

# Must stay identical to migration 0030's `messages_assign_sequence_number` — the two are the
# same mutual-exclusion domain, and a drift here would silently remove the only thing
# preventing a live insert from interleaving with a conversation's numbering. Guarded by
# tests/backend/test_message_sequence_backfill.py::test_advisory_lock_key_matches_migration.
MESSAGE_SEQUENCE_ADVISORY_LOCK_NAMESPACE = 72197002

# How many CONVERSATIONS one backfill_message_sequence_numbers() call will process. Bounded by
# default for the same reason app/rag/memory_source_backfill.py's BACKFILL_BATCH_SIZE is: an
# unbounded "process everything" default is what lets a runaway loop stay invisible instead of
# surfacing after one bounded call.
BACKFILL_BATCH_SIZE = 25


@dataclass
class ConversationOutcome:
    """One conversation's result. `assigned` is the number of rows that actually received an
    ordinal in this run — 0 for every status other than `numbered`."""

    conversation_id: uuid.UUID
    status: str  # "numbered" | "conflict"
    assigned: int = 0
    reason: str | None = None


@dataclass
class MessageSequenceBackfillResult:
    conversations_numbered: int = 0
    conversations_conflicted: int = 0
    messages_assigned: int = 0
    # Conversations that still held unnumbered messages when this call returned. Measured,
    # never inferred from "the batch cap was hit" — the same truthfulness rule migration
    # 0025's `status` docstring states for memory_source_backfill_runs, so a caller can never
    # report "done" for a run that merely ran out of batch. Deliberately INCLUDES conversations
    # this run reported as `conflict`: they genuinely still have unnumbered messages, and
    # hiding them here would turn an unresolved conflict into an invisible one.
    remaining_conversations: int = 0
    outcomes: list[ConversationOutcome] = field(default_factory=list)


def count_unsequenced_messages(db: Session, owner_id: uuid.UUID) -> int:
    """The VERIFY step of §4.8's expand/dual-write/backfill/verify/contract plan: how many of
    this owner's messages still have no ordinal. The CONTRACT migration (`SET NOT NULL` +
    a real UNIQUE constraint) must not be written until this reports 0 against real production
    data — that is the whole reason the column ships nullable."""
    return int(
        db.execute(
            text(
                "SELECT count(*) FROM messages m "
                "JOIN conversations c ON c.id = m.conversation_id "
                "WHERE c.user_id = :owner_id AND m.sequence_number IS NULL"
            ),
            {"owner_id": str(owner_id)},
        ).scalar_one()
    )


def count_conversations_with_unsequenced_messages(db: Session, owner_id: uuid.UUID) -> int:
    """Progress denominator for the durable job (app/jobs/handlers/message_sequence_backfill.py).
    A point-in-time count, not a promise: a conversation created after this is read is simply
    not part of this run's snapshot total — new messages are numbered by migration 0030's
    trigger at insert time and never need backfilling at all, so the snapshot can only ever
    shrink, never grow."""
    return int(
        db.execute(
            text(
                "SELECT count(DISTINCT m.conversation_id) FROM messages m "
                "JOIN conversations c ON c.id = m.conversation_id "
                "WHERE c.user_id = :owner_id AND m.sequence_number IS NULL"
            ),
            {"owner_id": str(owner_id)},
        ).scalar_one()
    )


def candidate_conversation_ids(
    db: Session, owner_id: uuid.UUID, limit: int, exclude: set[uuid.UUID] | None = None
) -> list[uuid.UUID]:
    """Conversations owned by `owner_id` that still contain at least one unnumbered message.

    Ordered by `(created_at, id)` — a total order, for the same reason the message numbering
    below uses one: a batched scan that re-queries between batches needs a deterministic order,
    or a conversation could be repeated or reordered across calls. (One cannot actually be
    skipped here regardless, since a conversation only leaves this result set by being fully
    numbered, but the deterministic order makes a partial run's progress reproducible and its
    logs comparable.)

    `exclude` is how a caller carries forward the conversations it has ALREADY seen conflict:
    a conflicted conversation is deliberately left unnumbered, so it would otherwise reappear
    in this list on every subsequent call forever. Filtering it out in SQL (not just skipping
    it in Python) is what lets a bounded batch keep making real progress instead of spending
    its whole budget re-discovering the same unfixable rows."""
    excluded = list(exclude or ())
    rows = db.execute(
        select(Conversation.id)
        .where(
            Conversation.user_id == owner_id,
            Conversation.id.in_(select(Message.conversation_id).where(Message.sequence_number.is_(None))),
            *([Conversation.id.notin_(excluded)] if excluded else []),
        )
        .order_by(Conversation.created_at.asc(), Conversation.id.asc())
        .limit(limit)
    ).scalars()
    return list(rows)


def _conversation_state(db: Session, conversation_id: uuid.UUID) -> tuple[int, int | None]:
    """`(unnumbered_count, smallest_already_assigned_sequence_number)` for one conversation.
    Read INSIDE the advisory lock by the caller, so it cannot be invalidated by a concurrent
    insert between the check and the write."""
    row = db.execute(
        text(
            "SELECT count(*) FILTER (WHERE sequence_number IS NULL) AS unnumbered, "
            "       min(sequence_number) AS min_assigned "
            "FROM messages WHERE conversation_id = :cid"
        ),
        {"cid": str(conversation_id)},
    ).one()
    return int(row.unnumbered), (int(row.min_assigned) if row.min_assigned is not None else None)


def backfill_conversation(
    db: Session,
    conversation_id: uuid.UUID,
    *,
    on_outcome: Callable[[ConversationOutcome], None] | None = None,
) -> ConversationOutcome:
    """Numbers one conversation's unnumbered messages and COMMITS. The caller is responsible
    for having established that `conversation_id` belongs to the owner it is acting for (see
    this module's owner-scoping note) — this function deliberately takes a bare id so the
    durable job can re-resolve and re-check ownership itself between batches rather than
    holding a stale ORM object across commits.

    Returns without writing anything (status `conflict`) if the conversation's current state
    would make the `1..N` assignment collide with an ordinal that already exists.

    `on_outcome` is invoked INSIDE this function's still-uncommitted transaction, immediately
    before the commit — the same shape Pass 37 imposed on app/rag/memory_source_backfill.py's
    `on_claim_outcome` after the founder rejected "the work committed but the run report did
    not" as a truthfulness failure, not an acceptable follow-up. Whatever the callback writes
    (the durable job's progress and its `progress_updated` event, in this module's one real
    caller) becomes durable in the SAME commit as the numbering it describes, so a hard crash
    can never leave messages numbered while the run report permanently under-counts them.
    A callback that raises — notably JobLeaseLostError, a worker that no longer owns the job —
    propagates with NOTHING committed, including the numbering itself, which is exactly right:
    a worker that lost its lease must not write at all. The next run re-finds the conversation
    unchanged (`sequence_number IS NULL` is the restart marker) and renumbers it identically."""
    db.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, hashtext(:cid))"),
        {"namespace": MESSAGE_SEQUENCE_ADVISORY_LOCK_NAMESPACE, "cid": str(conversation_id)},
    )

    unnumbered, min_assigned = _conversation_state(db, conversation_id)

    if unnumbered == 0:
        # Numbered by a concurrent run (or by nothing at all — the candidate query and this
        # read are separated by the lock acquisition). Not an error, and not counted as work.
        outcome = ConversationOutcome(conversation_id=conversation_id, status="numbered", assigned=0)
    elif min_assigned is not None and min_assigned <= unnumbered:
        reason = (
            f"conversation already holds sequence_number {min_assigned}, which falls inside the "
            f"1..{unnumbered} range this backfill would assign — refusing to renumber or collide"
        )
        logger.error("message_sequence_backfill: conversation %s conflict — %s", conversation_id, reason)
        outcome = ConversationOutcome(conversation_id=conversation_id, status="conflict", reason=reason)
    else:
        result = db.execute(
            text(
                "WITH ordered AS ("
                "    SELECT id, row_number() OVER (ORDER BY created_at ASC, id ASC) AS rn "
                "    FROM messages WHERE conversation_id = :cid AND sequence_number IS NULL"
                ") "
                "UPDATE messages m SET sequence_number = ordered.rn "
                "FROM ordered WHERE m.id = ordered.id"
            ),
            {"cid": str(conversation_id)},
        )
        outcome = ConversationOutcome(conversation_id=conversation_id, status="numbered", assigned=int(result.rowcount))

    if on_outcome is not None:
        on_outcome(outcome)
    db.commit()
    return outcome


def backfill_message_sequence_numbers(
    db: Session,
    owner_id: uuid.UUID,
    *,
    batch_size: int = BACKFILL_BATCH_SIZE,
    exclude_conversation_ids: set[uuid.UUID] | None = None,
    on_conversation_outcome: Callable[[ConversationOutcome], None] | None = None,
) -> MessageSequenceBackfillResult:
    """Numbers up to `batch_size` of this owner's conversations, one commit each.

    `on_conversation_outcome` is forwarded to `backfill_conversation`'s `on_outcome` — see that
    function for the atomicity guarantee it carries (the callback's writes commit together with
    the numbering they describe, never after it).

    Conflicts do NOT abort the run: the conflicted conversation is skipped, counted, reported,
    and the next one is processed — one impossible-state conversation must not block numbering
    the rest of a founder's history. Pass it back in via `exclude_conversation_ids` on the next
    call so a bounded batch is not spent re-discovering it."""
    if batch_size <= 0:
        # The exact class of bug Pass 19 found in app/rag/memory_source_backfill.py (a
        # non-positive batch size turning a bounded call into an unbounded/no-op loop) —
        # rejected explicitly rather than silently coerced.
        raise ValueError(f"batch_size must be positive, got {batch_size}")

    result = MessageSequenceBackfillResult()
    for conversation_id in candidate_conversation_ids(db, owner_id, batch_size, exclude_conversation_ids):
        outcome = backfill_conversation(db, conversation_id, on_outcome=on_conversation_outcome)
        result.outcomes.append(outcome)
        if outcome.status == "conflict":
            result.conversations_conflicted += 1
        else:
            result.conversations_numbered += 1
            result.messages_assigned += outcome.assigned

    result.remaining_conversations = count_conversations_with_unsequenced_messages(db, owner_id)
    return result
