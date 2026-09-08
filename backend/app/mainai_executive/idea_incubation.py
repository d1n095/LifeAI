"""Idea incubation lifecycle -- widens `IntelligenceIdea.disposition` beyond
`IdeaDisposition`'s flat accepted/rejected/deferred/unknown vocabulary (migration 0069) with
incubating/planned/ready/later, plus explicit, fail-closed transition functions mirroring
`app.life_intents.service`'s `LIFE_INTENT_TRANSITIONS` convention (explicit transition table,
typed errors, `expected_current_state` optimistic-concurrency param).

CRITICAL SCHEMA FACT, confirmed by direct testing against real Postgres (not assumed from the
model file alone): `intelligence_ideas` -- like every other `app.intelligence_governance`
table -- is enforced APPEND-ONLY at the database level by migration 0038's own
`trg_intelligence_ideas_deny_mutation` trigger (`intelligence_governance_deny_mutation()`,
`BEFORE UPDATE OR DELETE`). `UPDATE intelligence_ideas SET disposition = ...` is REJECTED by
Postgres itself, unconditionally, outside of account erasure. LifeIntent.state is a genuinely
mutable column (`app.life_intents.service.transition_intent()` does `row.state = state`);
`IntelligenceIdea.disposition` is NOT -- these two "transition" functions therefore cannot
share an implementation strategy despite sharing a transition-table SHAPE.

`transition_idea_disposition()` below instead mirrors `IntelligenceInterpretation`'s own real,
already-established append-only-ledger pattern (that model's own docstring: "recalculation
inserts a new row", via its `supersedes_id` column): it INSERTS a new `IntelligenceIdea` row
carrying the new disposition -- via the REAL, existing
`app.intelligence_governance.service.record_idea()`, never a raw `IntelligenceIdea(...)`
construction -- and records a durable
`app.intelligence_governance.service.record_idea_link()` edge (`relation=
'disposition_transition'`) from the new row back to the one it succeeds. `IntelligenceIdea`
has no `supersedes_id` column of its own; `IntelligenceIdeaLink` is the real, already-existing
mechanism this module reuses instead of adding one (staying inside the reconciliation doc's
"one small, additive migration, no new table" scope).

GOOD IDEA != ACTIVE JOB, structurally enforced, not just documented: this module owns
disposition transitions ONLY. The one function here that touches `app.work_candidates`,
`promote_ready_idea_to_work_candidate()`, requires the idea to already be `ready` and calls
ONLY the real, existing `app.work_candidates.service.record_work_candidate()` -- the STAGING
function, never `authorize_work_candidate()` (the ONLY function anywhere in this codebase that
can create a `MainAIGoal`). This module has no import of, and no code path that reaches,
`authorize_work_candidate()` -- an idea, no matter how `ready`, can only ever become an
unreviewed `WorkCandidate` here, exactly like every other `WorkCandidate` source in this
codebase (see `app.mainai_executive.lookaround`, which follows the identical staging-only
pattern for its own executive-scan candidates). Actually authorizing that `WorkCandidate` into
real, governed work remains entirely the separate, explicit, founder-attributed responsibility
of a caller invoking `authorize_work_candidate()` itself, exactly as `work_candidates/
service.py`'s own module docstring already requires for every `WorkCandidate` regardless of
origin."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.intelligence_governance.service import record_idea, record_idea_link
from app.models.intelligence_governance import IntelligenceIdea, IntelligenceIdeaLink
from app.models.work_candidate import WorkCandidate
from app.work_candidates.service import record_work_candidate

# The one relation value this module uses on IntelligenceIdeaLink -- `relation` is a plain,
# unconstrained String(32) column (no CHECK, unlike `disposition`), so no migration is needed
# to introduce it.
DISPOSITION_TRANSITION_RELATION = "disposition_transition"


class IdeaIncubationError(ValueError):
    pass


class InvalidIdeaTransitionError(IdeaIncubationError):
    """The requested FROM/TO disposition pair is not a legitimate transition per
    IDEA_DISPOSITION_TRANSITIONS -- raised instead of silently applying it."""


class TerminalIdeaStateError(InvalidIdeaTransitionError):
    """The idea's current disposition has no outbound transitions at all (accepted/rejected/
    deferred -- the pre-existing migration-0038 outcomes). STALE CALLER != CURRENT AUTHORITY:
    no generic call to transition_idea_disposition() may reopen a terminal idea -- this module
    defines no separate reopen operation, mirroring LifeIntent's own TerminalStateError
    doctrine (app.life_intents.service); if one is ever needed it must be its own, explicitly
    named, explicitly authorized function, never a side effect of this generic gate."""


class StaleIdeaTransitionError(IdeaIncubationError):
    """The caller's `expected_current_state` no longer matches the row's real current
    disposition -- another actor already recorded a newer transition since the caller last
    read it."""


class IdeaNotReadyError(IdeaIncubationError):
    """Raised by promote_ready_idea_to_work_candidate() when the idea's disposition is not
    'ready' -- the ONLY disposition this module allows to become a (staged, unauthorized)
    WorkCandidate. GOOD IDEA != ACTIVE JOB: an incubating/planned/later/unknown idea has no
    path through this function at all."""


# Real, widened IntelligenceIdea.disposition vocabulary (migration 0069, additive to the
# original accepted/rejected/deferred/unknown CHECK from migration 0038's
# ck_intelligence_idea_disposition). `unknown` is the column default (pre-triage).
# incubating/planned/ready are the new pre-authorization richness this module adds
# (reconciliation doc §8), sitting strictly BEFORE any authorized-work path. `later` is a
# deliberate "not now, but still promising" parking state, distinct from the pre-existing
# `deferred` (migration 0038's own ck_intelligence_idea_reason CHECK already requires a
# disposition_reason for accepted/rejected -- `later`/`deferred` both remain optional-reason,
# same as `incubating`/`planned`/`ready`).
IDEA_DISPOSITION_TRANSITIONS: dict[str, set[str]] = {
    "unknown": {"incubating", "planned", "later", "accepted", "rejected", "deferred"},
    "incubating": {"planned", "later", "rejected", "deferred"},
    "planned": {"incubating", "ready", "later", "rejected", "deferred"},
    "ready": {"planned", "later", "rejected", "deferred", "accepted"},
    "later": {"incubating", "planned", "rejected", "deferred"},
    # accepted/rejected/deferred are the pre-existing migration-0038 outcomes -- this module
    # does not reopen them; see TerminalIdeaStateError above.
    "accepted": set(),
    "rejected": set(),
    "deferred": set(),
}

TERMINAL_IDEA_DISPOSITIONS = frozenset({"accepted", "rejected", "deferred"})

# disposition values requiring a non-empty disposition_reason -- mirrors migration 0038's real
# `ck_intelligence_idea_reason` CHECK verbatim (disposition NOT IN ('accepted','rejected') OR
# disposition_reason IS NOT NULL). None of the four NEW values require one.
REASON_REQUIRED_DISPOSITIONS = frozenset({"accepted", "rejected"})


def _idea(db: Session, owner_id: uuid.UUID, idea_id: uuid.UUID) -> IntelligenceIdea:
    row = db.execute(
        select(IntelligenceIdea).where(IntelligenceIdea.id == idea_id, IntelligenceIdea.owner_id == owner_id)
    ).scalar_one_or_none()
    if row is None:
        raise IdeaIncubationError(f"idea {idea_id} is missing or belongs to another owner")
    return row


def resolve_current_idea(db: Session, *, owner_id: uuid.UUID, idea_id: uuid.UUID) -> IntelligenceIdea:
    """Follows the disposition_transition link chain forward from `idea_id` to the newest row
    in its lineage (a newer row's own link points FROM itself TO the row it supersedes -- see
    transition_idea_disposition()). Read-only, bounded by the chain's real length -- there is
    no cycle risk since each new row is a genuinely new INSERT, never a re-point of an
    existing one."""
    current = _idea(db, owner_id, idea_id)
    seen = {current.id}
    while True:
        newer_id = db.execute(
            select(IntelligenceIdeaLink.from_idea_id).where(
                IntelligenceIdeaLink.owner_id == owner_id,
                IntelligenceIdeaLink.to_idea_id == current.id,
                IntelligenceIdeaLink.relation == DISPOSITION_TRANSITION_RELATION,
            )
        ).scalars().first()
        if newer_id is None or newer_id in seen:
            return current
        current = _idea(db, owner_id, newer_id)
        seen.add(current.id)


def transition_idea_disposition(
    db: Session,
    *,
    owner_id: uuid.UUID,
    idea_id: uuid.UUID,
    disposition: str,
    idempotency_key: str,
    reason: str | None = None,
    expected_current_state: str | None = None,
) -> IntelligenceIdea:
    """Fail-closed IntelligenceIdea.disposition state machine (see IDEA_DISPOSITION_
    TRANSITIONS above) -- appends a NEW IntelligenceIdea row via record_idea(), never an
    UPDATE (see module docstring: intelligence_ideas is DB-enforced append-only).

    `idea_id` may be ANY row in the idea's lineage (the original row or any later transition
    row) -- this function always resolves it to the CURRENT (latest) row via
    resolve_current_idea() first, since staleness is a property of the LOGICAL idea (the whole
    chain), not of whichever single immutable row happens to be passed. A caller holding an
    id that has since been superseded by someone else's transition is exactly the case
    `expected_current_state` exists to catch.

    `expected_current_state` is an optional optimistic-concurrency check: if given, and the
    idea's ACTUAL current disposition (after resolving to the latest row in the chain) no
    longer matches it, raises StaleIdeaTransitionError immediately -- before even considering
    whether the requested transition would otherwise be legal.

    A same-disposition call (old == disposition) is a harmless no-op that returns the existing
    current row unchanged -- mirrors transition_intent()'s own no-op convention for same-state
    calls, and (since a new row would otherwise be indistinguishable append-only noise) never
    mints one."""
    if disposition not in IDEA_DISPOSITION_TRANSITIONS:
        raise InvalidIdeaTransitionError(f"{disposition!r} is not a recognized idea disposition")
    if disposition in REASON_REQUIRED_DISPOSITIONS and not (reason or "").strip():
        raise IdeaIncubationError(
            f"disposition {disposition!r} requires a non-empty reason (ck_intelligence_idea_reason)"
        )
    old = resolve_current_idea(db, owner_id=owner_id, idea_id=idea_id)
    if expected_current_state is not None and old.disposition != expected_current_state:
        raise StaleIdeaTransitionError(
            f"caller expected idea {idea_id} to currently be {expected_current_state!r}, but it is "
            f"{old.disposition!r} -- another actor already recorded a newer transition since this "
            "caller last read it"
        )
    if old.disposition == disposition:
        return old
    if old.disposition in TERMINAL_IDEA_DISPOSITIONS:
        raise TerminalIdeaStateError(
            f"idea {idea_id} is already terminal ({old.disposition!r}); cannot transition to {disposition!r}"
        )
    if disposition not in IDEA_DISPOSITION_TRANSITIONS.get(old.disposition, set()):
        raise InvalidIdeaTransitionError(
            f"cannot transition idea {idea_id} from {old.disposition!r} to {disposition!r}"
        )
    new_row = record_idea(
        db,
        owner_id=owner_id,
        execution_id=old.execution_id,
        idea_kind=old.idea_kind,
        content=old.content,
        evidence_id=old.evidence_id,
        disposition=disposition,
        disposition_reason=reason,
        classification_basis=old.classification_basis,
        confidence=old.confidence,
        idempotency_key=idempotency_key,
    )
    record_idea_link(
        db, owner_id=owner_id, from_idea_id=new_row.id, to_idea_id=old.id,
        relation=DISPOSITION_TRANSITION_RELATION,
    )
    return new_row


def promote_ready_idea_to_work_candidate(
    db: Session,
    *,
    owner_id: uuid.UUID,
    idea_id: uuid.UUID,
    source_entity_id: uuid.UUID,
    idempotency_key: str,
    title: str | None = None,
    rationale: str | None = None,
    priority: str = "medium",
    classifier_strategy: str = "idea_incubation_v1",
) -> WorkCandidate:
    """The ONLY bridge from an incubated idea into app.work_candidates -- and it stops at
    record_work_candidate() (staging only, status='unreviewed'). Structurally CANNOT skip
    straight into authorized work: see module docstring. Requires the idea's disposition to
    ALREADY be 'ready' (raises IdeaNotReadyError otherwise) -- no shortcut from
    incubating/planned/later/unknown. `idea_id` must be the CURRENT row in its lineage (see
    resolve_current_idea()) -- a caller holding a stale, since-superseded idea_id gets the
    same IdeaNotReadyError an idea that was genuinely never 'ready' would, since a superseded
    row's own disposition is whatever it was at the moment it stopped being current."""
    idea = _idea(db, owner_id, idea_id)
    if idea.disposition != "ready":
        raise IdeaNotReadyError(
            f"idea {idea_id} is {idea.disposition!r}, not 'ready' -- only a 'ready' idea may be "
            "staged as a work candidate (must pass through transition_idea_disposition() first)"
        )
    return record_work_candidate(
        db,
        owner_id=owner_id,
        source_entity_id=source_entity_id,
        title=(title or idea.content)[:200],
        idempotency_key=idempotency_key,
        rationale=rationale or idea.content,
        priority=priority,
        classifier_strategy=classifier_strategy,
        classifier_confidence=float(idea.confidence) if idea.confidence is not None else None,
        provenance={
            "idea_id": str(idea.id),
            "idea_disposition": idea.disposition,
            "authorized": False,
            "future_plan_is_not_authority": True,
        },
    )
