"""S1A deterministic backfill (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8, phase 2 of the
six-phase `KnowledgeClaim.source_id`/`version_id`/`chunk_id` cutover plan): assigns
`memory_source_id` to existing `knowledge_claims` rows that predate S1A (`memory_source_id IS
NULL`). One `memory_source_units`+`document_source_units` row is created per DISTINCT
chunk/version/document among matching claims, never one per claim — `get_or_create_memory_
source_unit`'s identity-key-based find-or-create (app/rag/memory_source.py) already provides
that de-duplication for free. No AI/provider/network call anywhere in this module.

Resolution order per §4.8 (never fall through past a PRESENT-but-invalid identifier — that
would be exactly the "guess or attach to the nearest source" the plan explicitly forbids):
  1. `claim.chunk_id` is set AND structurally valid (belongs to `claim.owner_id`/
     `claim.source_id`) -> `document_chunk` + `exact`, `content_text` read from the real
     `DocumentChunk.text`, never from the claim or any other input.
  2. `claim.chunk_id` is NULL, `claim.version_id` is set AND structurally valid -> `document_
     version` + `degraded` (a real `KnowledgeVersion` row is still genuine evidence, even
     without the exact chunk text).
  3. `claim.chunk_id` and `claim.version_id` are both NULL, `claim.source_id` resolves to a
     real, owner-matching `Document` -> `document_record` + `missing` (nothing but the
     document's existence can be vouched for at this tier).
  4. Anything else — a `chunk_id`/`version_id` that is PRESENT but doesn't structurally match
     `claim.owner_id`/`claim.source_id`, or a `source_id` that doesn't resolve to a
     `Document` owned by `claim.owner_id` — fails closed: the claim is skipped and logged,
     never silently attached to a lower tier.

The "degraded vs. missing" split above is this module's canonical policy: `degraded` means
some real, independently-verifiable evidence still exists beyond the document's bare
existence (a `KnowledgeVersion` row); `missing` means only the `Document` row itself remains.

These application-level checks deliberately duplicate migration 0019's
`trg_dsu_validate_fields` structural checks (document/version/chunk ownership). That trigger
is a `DEFERRABLE INITIALLY DEFERRED` constraint trigger, so a genuinely invalid locator would
only fail at COMMIT — aborting the whole transaction, including every other claim already
processed within it. Pre-validating here, before ever calling `get_or_create_memory_source_
unit`, is what lets one bad claim be skipped without losing any other claim's progress.

Concurrency/restart-safety: each claim is claimed with `SELECT ... FOR UPDATE SKIP LOCKED`
and resolved+linked+committed as its own transaction — a second worker's concurrent query
simply skips whatever the first worker already has locked and picks up the next row instead,
and a crash between two claims loses at most the in-flight one (retried next run, still a
valid NULL-memory_source_id candidate). `get_or_create_memory_source_unit` itself is the
proven-safe primitive for the source-unit half of that atomicity (see its own module
docstring and tests/backend/test_memory_source_units.py's real concurrent-lock-wait test).

Production execution — durable run tracking now exists (app/rag/backfill/memory_source_run.py,
migration 0025), closing the gap this docstring used to describe. This module's own
`MemorySourceBackfillResult` and `logger.warning`/`logger.exception` calls were never a durable
enough progress/failure record for a real production backfill across a whole owner base on
their own — a crashed process loses whatever was only ever in memory or in ordinary logs, an
operator can't ask "how far did run X get" without grepping logs, and a specific claim's
specific failure reason isn't queryable. `memory_source_backfill_run.py`'s
`MemorySourceBackfillRun`/`MemorySourceBackfillFailure` durably persist exactly what was
missing: a run id, owner_id, started_at/completed_at, status, the per-source-kind/skipped/
failed/already_done counters this module already computes, a resumable `(created_at, id)`
cursor (see this module's own `after` parameter below), and a claim-level failure log (claim id
+ reason + retry count, never `claim_text`) — built as its own owner-scoped table pair rather
than folded into the unrelated `knowledge_import_jobs`/worker-lease machinery
`trigger_claim_type_backfill`'s docstring once speculated about, since a backfill run has no
uploaded blob, no ZIP extraction, and no worker-container lease semantics to share with that
table. A genuinely REAL production backfill EXECUTION (as opposed to this module and its
durable-tracking wrapper simply existing) is still a separate, explicit, later decision — see
`memory_source_backfill_run.py`'s own module docstring for exactly what remains gated on that.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_claim import KnowledgeClaim
from app.models.knowledge_version import KnowledgeVersion
from app.models.memory_source_unit import SnapshotStatus, SourceKind
from app.rag.memory_source import DocumentSourceLocator, MemorySourceIdentityConflict, get_or_create_memory_source_unit

logger = logging.getLogger("mainai.memory_source_backfill")

# A "batch" here bounds how much work one backfill_memory_source_units() call does (matching
# app/rag/claims.py's backfill_claim_types max_batches convention), not a single SQL round
# trip — each candidate claim is still its own SELECT ... FOR UPDATE SKIP LOCKED + commit, by
# design (see module docstring's concurrency/restart-safety section).
BACKFILL_BATCH_SIZE = 50

# A "bounded" function should not default to "process everything" -- that default is exactly
# what let the pre-Pass-19 infinite-loop bug run for hours undetected instead of surfacing
# after one bounded call. Matches app/routers/admin.py's MAX_BACKFILL_BATCHES_PER_REQUEST
# convention for the sibling claim_type backfill. A caller that genuinely wants to run to
# completion in one call (an offline/maintenance script, never an HTTP request) must pass
# max_batches=None explicitly -- that is an opt-in, not the default.
DEFAULT_MAX_BATCHES = 10


@dataclass
class MemorySourceBackfillResult:
    candidates_total: int = 0
    chunk_backed: int = 0
    version_backed: int = 0
    document_backed: int = 0
    skipped_mismatch: int = 0
    failed: int = 0
    # Snapshot taken once at the start of the call: how many of this owner's claims already
    # had memory_source_id set BEFORE this run (from an earlier backfill run or dual-write) —
    # not affected by candidates_total/chunk_backed/etc., which only describe THIS call's work.
    already_done: int = 0
    # The newest (created_at, id) pair actually considered (in any outcome) by this call —
    # None if candidates_total is 0. app/rag/backfill/memory_source_run.py's durable run
    # tracking persists this as a resumable checkpoint (see its own module docstring for why
    # both dry_run and real mode need it, not just dry_run).
    last_seen_created_at: datetime | None = None
    last_seen_id: uuid.UUID | None = None
    # One (claim_id, reason) entry per skipped/failed claim in THIS call — `reason` is always
    # the exact structural failure string `_resolve_locator()`/`_apply()` already log, NEVER
    # `KnowledgeClaim.claim_text`. app/rag/backfill/memory_source_run.py's durable run
    # tracking persists these into `memory_source_backfill_failures`.
    failures: list[tuple[uuid.UUID, str]] = field(default_factory=list)


@dataclass
class _ResolutionFailure:
    reason: str


@dataclass
class ClaimOutcomeDelta:
    """Founder review round 3 (HIGH — transaction atomicity): one claim's contribution to a
    durable run's reporting state, handed to an optional `on_claim_outcome` callback so the
    caller (app/rag/backfill/memory_source_run.py's `advance_backfill_run()`) can apply it to
    `MemorySourceBackfillRun` and flush/upsert `MemorySourceBackfillFailure` WITHIN the exact
    same transaction `_apply()`/`_dry_run()` commits for that claim — see `backfill_memory_
    source_units()`'s own docstring for why this must never be split into a separate, later
    commit. Exactly one of chunk_backed/version_backed/document_backed/skipped_mismatch/failed
    is ever 1 for a given claim (mutually exclusive outcomes); `cursor` is `None` when the
    persisted checkpoint must not advance for this claim (BLOCKER 2's lock-gap freeze)."""

    claim_id: uuid.UUID
    chunk_backed: int = 0
    version_backed: int = 0
    document_backed: int = 0
    skipped_mismatch: int = 0
    failed: int = 0
    # Set only when a memory_source_backfill_failures row must be written/upserted for this
    # claim — never KnowledgeClaim.claim_text, only the same structural strings already used
    # elsewhere in this module.
    failure_reason: str | None = None
    cursor: tuple[datetime, uuid.UUID] | None = None


OnClaimOutcome = Callable[[Session, ClaimOutcomeDelta], None]


def _resolve_locator(db: Session, claim: KnowledgeClaim) -> DocumentSourceLocator | _ResolutionFailure:
    document = db.get(Document, claim.source_id)
    if document is None or document.uploaded_by != claim.owner_id:
        return _ResolutionFailure(
            f"document {claim.source_id} not found or not owned by owner {claim.owner_id}"
        )

    observed_at = datetime.now(timezone.utc)

    if claim.chunk_id is not None:
        chunk = db.get(DocumentChunk, claim.chunk_id)
        if chunk is None or chunk.document_id != claim.source_id or chunk.owner_id != claim.owner_id:
            return _ResolutionFailure(
                f"chunk_id {claim.chunk_id} does not structurally belong to document "
                f"{claim.source_id}/owner {claim.owner_id}"
            )
        return DocumentSourceLocator(
            owner_id=claim.owner_id,
            document_id=claim.source_id,
            version_id=None,
            chunk_id=chunk.id,
            observed_at=observed_at,
            content_text=chunk.text,
            snapshot_status=SnapshotStatus.exact,
        )

    if claim.version_id is not None:
        version = db.get(KnowledgeVersion, claim.version_id)
        if version is None or version.source_id != claim.source_id or version.owner_id != claim.owner_id:
            return _ResolutionFailure(
                f"version_id {claim.version_id} does not structurally belong to document "
                f"{claim.source_id}/owner {claim.owner_id}"
            )
        return DocumentSourceLocator(
            owner_id=claim.owner_id,
            document_id=claim.source_id,
            version_id=version.id,
            chunk_id=None,
            observed_at=observed_at,
            content_text=None,
            snapshot_status=SnapshotStatus.degraded,
        )

    return DocumentSourceLocator(
        owner_id=claim.owner_id,
        document_id=claim.source_id,
        version_id=None,
        chunk_id=None,
        observed_at=observed_at,
        content_text=None,
        snapshot_status=SnapshotStatus.missing,
    )


def _apply_cursor(query, after: tuple[datetime, uuid.UUID] | None):
    """Keyset-pagination lower bound matching the `.order_by(created_at.asc(), id.asc())`
    every query below already uses — excludes everything at or before `after`, never a claim
    at exactly `after` itself (that one was already considered by the call that produced this
    cursor)."""
    if after is None:
        return query
    after_created_at, after_id = after
    return query.filter(
        or_(
            KnowledgeClaim.created_at > after_created_at,
            and_(KnowledgeClaim.created_at == after_created_at, KnowledgeClaim.id > after_id),
        )
    )


def _advance_cursor(result: MemorySourceBackfillResult, claim: KnowledgeClaim) -> None:
    result.last_seen_created_at = claim.created_at
    result.last_seen_id = claim.id


def _before_position(query, created_at: datetime, id_: uuid.UUID):
    """The mirror image of `_apply_cursor` — matches anything strictly BEFORE `(created_at,
    id_)` in the same `(created_at.asc(), id.asc())` ordering. Used by `_apply()`'s
    lock-gap check below, never by the plain forward-scan queries."""
    return query.filter(
        or_(
            KnowledgeClaim.created_at < created_at,
            and_(KnowledgeClaim.created_at == created_at, KnowledgeClaim.id < id_),
        )
    )


def _tally_outcome(result: MemorySourceBackfillResult, outcome: DocumentSourceLocator | _ResolutionFailure) -> None:
    if isinstance(outcome, _ResolutionFailure):
        result.skipped_mismatch += 1
        return
    if outcome.source_kind == SourceKind.document_chunk:
        result.chunk_backed += 1
    elif outcome.source_kind == SourceKind.document_version:
        result.version_backed += 1
    else:
        result.document_backed += 1


def _outcome_bucket_kwargs(outcome: DocumentSourceLocator | _ResolutionFailure) -> dict[str, int]:
    """The same classification `_tally_outcome` applies to `result`, expressed as
    `ClaimOutcomeDelta` kwargs — kept as one shared mapping so the two can never silently
    disagree about which bucket a given outcome belongs to."""
    if isinstance(outcome, _ResolutionFailure):
        return {"skipped_mismatch": 1}
    if outcome.source_kind == SourceKind.document_chunk:
        return {"chunk_backed": 1}
    if outcome.source_kind == SourceKind.document_version:
        return {"version_backed": 1}
    return {"document_backed": 1}


def backfill_memory_source_units(
    db: Session,
    owner_id: uuid.UUID,
    *,
    batch_size: int = BACKFILL_BATCH_SIZE,
    max_batches: int | None = DEFAULT_MAX_BATCHES,
    dry_run: bool = False,
    after: tuple[datetime, uuid.UUID] | None = None,
    on_claim_outcome: OnClaimOutcome | None = None,
) -> MemorySourceBackfillResult:
    """Owner-scoped, bounded, idempotent, restart-safe backfill of `KnowledgeClaim.
    memory_source_id` for `owner_id`'s claims. The caller's session must already have
    `app.current_user_id` set to `owner_id` (same precondition as app/rag/claims.py's
    extraction/backfill functions) — SQLAlchemy re-applies `SET LOCAL app.current_user_id` on
    every new transaction automatically (see app/db.py's `after_begin` listener), so the
    repeated per-claim commits below don't lose RLS context.

    `dry_run=True` resolves and tallies every candidate exactly as a real run would, but never
    calls `get_or_create_memory_source_unit` and never writes `memory_source_id` — safe to run
    against production data to see the expected outcome first. No FOR UPDATE lock is taken in
    dry-run mode (nothing here needs exclusive access to report on it), so already-seen
    candidates are excluded by id instead of by re-querying a mutated NULL-filter.

    `max_batches` defaults to `DEFAULT_MAX_BATCHES` (bounded, not "process everything") — a
    caller that genuinely wants to run every remaining candidate to completion in one call must
    pass `max_batches=None` explicitly, an opt-in reserved for an offline/maintenance runner,
    never an HTTP request. `batch_size` must be a positive integer and `max_batches` must be
    `None` or a positive integer — either bound as `<= 0` would make `_apply`'s per-batch
    `range(batch_size)` loop body never run, leaving `exhausted` `False` forever and spinning
    the outer `while` loop indefinitely (the same failure class the Pass 19 review caught: a
    bounded-sounding function must actually be unable to loop forever, not just unlikely to).

    `after` is an optional `(created_at, id)` keyset-pagination lower bound, excluding
    everything at or before it — the durable-run-tracking layer (app/rag/
    memory_source_backfill_run.py) passes each call's own previous `last_seen_created_at`/
    `last_seen_id` back in here so a resumed DRY-RUN doesn't re-scan (and double-count)
    claims a prior call already tallied. Real-mode calls don't strictly need this for
    correctness (`memory_source_id IS NULL` already excludes anything this function itself
    already wrote), but accept the same parameter for one consistent checkpoint mechanism
    across both modes.

    `on_claim_outcome` (founder review round 3 — HIGH, transaction atomicity): an optional
    callback invoked, per claim, with a `ClaimOutcomeDelta` describing that claim's outcome —
    called and flushed WITHIN the exact same (still-uncommitted) transaction this function is
    about to commit for that claim, so the caller can make a durable run's reporting state
    (`MemorySourceBackfillRun` counters/cursor, `MemorySourceBackfillFailure` rows) commit
    ATOMICALLY with the claim itself. `None` (the default) preserves this function's original,
    standalone behavior exactly — used by tests/backend/test_memory_source_backfill.py, which
    calls this function directly with no durable run involved at all. Only
    app/rag/backfill/memory_source_run.py's `advance_backfill_run()` passes a real callback.
    """
    if batch_size <= 0:
        raise ValueError(f"batch_size must be a positive integer, got {batch_size!r}")
    if max_batches is not None and max_batches <= 0:
        raise ValueError(f"max_batches must be None or a positive integer, got {max_batches!r}")

    result = MemorySourceBackfillResult()
    result.already_done = (
        db.query(KnowledgeClaim)
        .filter(KnowledgeClaim.owner_id == owner_id, KnowledgeClaim.memory_source_id.isnot(None))
        .count()
    )

    if dry_run:
        return _dry_run(db, owner_id, batch_size, max_batches, result, after, on_claim_outcome)
    return _apply(db, owner_id, batch_size, max_batches, result, after, on_claim_outcome)


def _dry_run(
    db: Session,
    owner_id: uuid.UUID,
    batch_size: int,
    max_batches: int | None,
    result: MemorySourceBackfillResult,
    after: tuple[datetime, uuid.UUID] | None,
    on_claim_outcome: OnClaimOutcome | None,
) -> MemorySourceBackfillResult:
    seen_ids: set[uuid.UUID] = set()
    batches_done = 0
    while max_batches is None or batches_done < max_batches:
        query = db.query(KnowledgeClaim).filter(
            KnowledgeClaim.owner_id == owner_id, KnowledgeClaim.memory_source_id.is_(None)
        )
        query = _apply_cursor(query, after)
        if seen_ids:
            query = query.filter(KnowledgeClaim.id.notin_(seen_ids))
        candidates = query.order_by(KnowledgeClaim.created_at.asc(), KnowledgeClaim.id.asc()).limit(batch_size).all()
        if not candidates:
            break

        for claim in candidates:
            seen_ids.add(claim.id)
            result.candidates_total += 1
            _advance_cursor(result, claim)
            outcome = _resolve_locator(db, claim)
            failure_reason: str | None = None
            if isinstance(outcome, _ResolutionFailure):
                logger.warning("memory_source_backfill dry-run: claim %s: %s", claim.id, outcome.reason)
                result.failures.append((claim.id, outcome.reason))
                failure_reason = outcome.reason
            _tally_outcome(result, outcome)

            # Founder review round 3 (HIGH): dry-run writes nothing to the claim/source-unit
            # tables (by design — see this function's own docstring), so there is no per-claim
            # data write to entangle a commit with here. But the durable run's OWN counters/
            # cursor still need the same crash-safety guarantee as real mode: without a commit
            # per claim, a crash mid-batch would silently lose this call's progress the exact
            # same way real mode's did before this round's fix (see module docstring's
            # ClaimOutcomeDelta / advance_backfill_run's commit-map comment).
            if on_claim_outcome is not None:
                delta = ClaimOutcomeDelta(
                    claim_id=claim.id, failure_reason=failure_reason, cursor=(claim.created_at, claim.id), **_outcome_bucket_kwargs(outcome)
                )
                on_claim_outcome(db, delta)
                db.commit()
        batches_done += 1

    return result


def _apply(
    db: Session,
    owner_id: uuid.UUID,
    batch_size: int,
    max_batches: int | None,
    result: MemorySourceBackfillResult,
    after: tuple[datetime, uuid.UUID] | None,
    on_claim_outcome: OnClaimOutcome | None,
) -> MemorySourceBackfillResult:
    # A claim that fails resolution or hits a genuine conflict is excluded from re-selection
    # for the REST OF THIS CALL (same pattern as app/rag/claims.py's backfill_claim_types
    # failed_ids) — nothing about the claim changes between one iteration and the next, so
    # without this exclusion a claim that never resolves would be re-selected forever,
    # spinning the outer while loop indefinitely whenever max_batches=None (the default). The
    # claim's memory_source_id is never written for these, so it's still a real candidate on
    # the NEXT separate backfill_memory_source_units call, in case the underlying data (a
    # since-fixed chunk_id, a since-created Document) changed in between.
    excluded_ids: set[uuid.UUID] = set()

    # Founder review, round 2 (BLOCKER 2): `SELECT ... FOR UPDATE SKIP LOCKED` below returns
    # the smallest UNLOCKED matching row — it cannot tell us whether a SMALLER, still-eligible
    # row exists but is momentarily locked by some unrelated concurrent transaction. If we kept
    # advancing `result.last_seen_*` (and therefore the durable persisted cursor) to whatever
    # SKIP LOCKED happened to return, that smaller locked claim would be permanently excluded
    # by every future call's cursor filter — even though it was never actually resolved, never
    # recorded as a failure, and its memory_source_id is still NULL. Once the per-claim gap
    # check below finds such a gap, this freezes for the REST of this call: real work below
    # still proceeds normally (claims still get resolved/committed), but `result.last_seen_*`
    # simply stops moving forward, so the persisted cursor stays at (or before) the locked
    # claim's position and it remains reachable by a later call once the lock clears.
    cursor_frozen = False

    batches_done = 0
    while max_batches is None or batches_done < max_batches:
        exhausted = False
        for _ in range(batch_size):
            query = db.query(KnowledgeClaim).filter(
                KnowledgeClaim.owner_id == owner_id, KnowledgeClaim.memory_source_id.is_(None)
            )
            query = _apply_cursor(query, after)
            if excluded_ids:
                query = query.filter(KnowledgeClaim.id.notin_(excluded_ids))
            claim = (
                query.order_by(KnowledgeClaim.created_at.asc(), KnowledgeClaim.id.asc())
                .with_for_update(skip_locked=True)
                .first()
            )
            if claim is None:
                exhausted = True
                break
            result.candidates_total += 1
            # Captured as local values (not left as live ORM-attribute access) BEFORE any
            # resolve/commit/rollback below, which could otherwise expire this object's
            # attributes — _advance_cursor must still see the real claim identity even after
            # a rollback on the FAILURE path a few lines down.
            claim_created_at, claim_id = claim.created_at, claim.id

            if not cursor_frozen:
                gap_query = db.query(KnowledgeClaim.id).filter(
                    KnowledgeClaim.owner_id == owner_id, KnowledgeClaim.memory_source_id.is_(None)
                )
                gap_query = _apply_cursor(gap_query, after)
                gap_query = _before_position(gap_query, claim_created_at, claim_id)
                if excluded_ids:
                    gap_query = gap_query.filter(KnowledgeClaim.id.notin_(excluded_ids))
                # A plain (non-locking) read sees a row regardless of another transaction's row
                # lock (Postgres MVCC) — if this finds anything, SKIP LOCKED above must have
                # jumped over it, since our own query is ordered ascending and would otherwise
                # have returned it instead of `claim`.
                if gap_query.first() is not None:
                    cursor_frozen = True
                    logger.warning(
                        "memory_source_backfill: an eligible claim earlier than %s is locked by a "
                        "concurrent transaction — freezing this run's cursor so it stays reachable "
                        "instead of skipping it permanently",
                        claim_id,
                    )

            if not cursor_frozen:
                result.last_seen_created_at, result.last_seen_id = claim_created_at, claim_id
            # Founder review round 3 (HIGH — transaction atomicity): this is the SAME cursor
            # value handed to `on_claim_outcome` below, so the durable run's persisted cursor
            # can never disagree with `result.last_seen_*` — both are set from this one local,
            # exactly once, per claim.
            cursor_for_commit = (claim_created_at, claim_id) if not cursor_frozen else None

            outcome = _resolve_locator(db, claim)
            if isinstance(outcome, _ResolutionFailure):
                logger.warning("memory_source_backfill: claim %s: %s — leaving untouched", claim.id, outcome.reason)
                result.failures.append((claim_id, outcome.reason))
                _tally_outcome(result, outcome)
                excluded_ids.add(claim.id)
                # Nothing has been written for this claim yet (_resolve_locator only reads) —
                # no db.rollback() needed before durably recording the failure + run-state
                # delta as ONE atomic commit (see this function's docstring / ClaimOutcomeDelta).
                if on_claim_outcome is not None:
                    on_claim_outcome(
                        db, ClaimOutcomeDelta(claim_id=claim_id, failure_reason=outcome.reason, cursor=cursor_for_commit, skipped_mismatch=1)
                    )
                    db.commit()
                else:
                    db.rollback()
                continue

            try:
                # Same transaction as the claim.memory_source_id write below — nothing here
                # commits until the single db.commit() a few lines down, so a source unit is
                # never created and left dangling without its claim link (see module
                # docstring's concurrency/restart-safety section).
                memory_source_id = get_or_create_memory_source_unit(db, outcome)
                claim.memory_source_id = memory_source_id
                db.add(claim)
            except MemorySourceIdentityConflict as exc:
                db.rollback()
                result.failed += 1
                reason = f"identity conflict: {exc}"
                result.failures.append((claim_id, reason))
                excluded_ids.add(claim.id)
                logger.error("memory_source_backfill: claim %s: identity conflict: %s", claim.id, exc)
                if on_claim_outcome is not None:
                    on_claim_outcome(db, ClaimOutcomeDelta(claim_id=claim_id, failure_reason=reason, cursor=cursor_for_commit, failed=1))
                    db.commit()
                continue
            except Exception as exc:
                db.rollback()
                result.failed += 1
                reason = f"unexpected error: {type(exc).__name__}"
                result.failures.append((claim_id, reason))
                excluded_ids.add(claim.id)
                logger.exception("memory_source_backfill: claim %s: unexpected error", claim.id)
                if on_claim_outcome is not None:
                    on_claim_outcome(db, ClaimOutcomeDelta(claim_id=claim_id, failure_reason=reason, cursor=cursor_for_commit, failed=1))
                    db.commit()
                continue
            else:
                # get_or_create_memory_source_unit + claim assignment succeeded with no
                # exception — only NOW is it safe to record the outcome and commit.
                # Deliberately a SEPARATE try/except from the block above, not folded into
                # the same `except Exception` used for get_or_create_memory_source_unit: a
                # failure here (a bug in the callback itself, or the commit itself failing —
                # DB error, constraint violation, connection loss) must be classified for
                # what it is and still isolated to this one claim, exactly like every other
                # failure path in this function (see test_backfill_crash_before_commit_
                # leaves_no_half_source), without being misattributed to identity resolution
                # or risking the callback being invoked a second time from inside its own
                # failure handling.
                try:
                    if on_claim_outcome is not None:
                        on_claim_outcome(
                            db, ClaimOutcomeDelta(claim_id=claim_id, cursor=cursor_for_commit, **_outcome_bucket_kwargs(outcome))
                        )
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    result.failed += 1
                    reason = f"unexpected error: {type(exc).__name__}"
                    result.failures.append((claim_id, reason))
                    excluded_ids.add(claim.id)
                    logger.exception("memory_source_backfill: claim %s: unexpected error", claim.id)
                    if on_claim_outcome is not None:
                        on_claim_outcome(db, ClaimOutcomeDelta(claim_id=claim_id, failure_reason=reason, cursor=cursor_for_commit, failed=1))
                        db.commit()
                    continue
                _tally_outcome(result, outcome)

        batches_done += 1
        if exhausted:
            break

    return result
