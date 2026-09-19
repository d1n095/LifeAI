"""Strategic compression -- proactively COMBINES several related `WorkCandidate`/
`IntelligenceIdea` rows into ONE coherent grouping rather than leaving them (or letting a caller
treat them) as N independently-recommended items (reconciliation doc §9 / real gap #6).

`compress_into_program()` composes with the real, already-existing `app.memory_threads`
mechanism (`create_thread()`/`add_member()`, read back via `thread_members()`) -- the SAME
mechanism the reconciliation doc names as the real MECHANISM for this decision -- rather than
inventing a parallel grouping table. Creating a `MemoryThread` carries NO execution/spend/merge/
deploy authority (see `app.memory_threads.service`'s own module docstring: "index durable
history without copying canonical content") -- it is a purely organizational grouping, exactly
like every other `MemoryThread` this codebase already creates. This module never imports
`app.work_candidates.service.authorize_work_candidate()` and never creates a new `WorkCandidate`
or `IntelligenceIdea` row itself -- it only groups EXISTING rows the caller already identified as
related (e.g. via `architectural_gravity.detect_architectural_gravity()`'s own tag clustering, or
a founder's own explicit selection). GOOD IDEA != ACTIVE JOB still holds: grouping is not
authorization."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.memory_threads.service import add_member, create_thread
from app.models.intelligence_governance import IntelligenceIdea
from app.models.work_candidate import WorkCandidate

MIN_ITEMS_TO_COMPRESS = 2


class StrategicCompressionError(ValueError):
    pass


def compress_into_program(
    db: Session,
    *,
    owner_id: uuid.UUID,
    idempotency_key: str,
    work_candidate_ids: list[uuid.UUID] | None = None,
    idea_ids: list[uuid.UUID] | None = None,
    program_label: str | None = None,
) -> dict[str, Any]:
    """Groups the given, already-related `WorkCandidate`/`IntelligenceIdea` rows into ONE
    `MemoryThread` and returns ONE proposal dict describing the combined program -- never one
    proposal per input row. `proposal_count` is always exactly `1` on a successful return,
    structurally proving N related items collapse to one grouped recommendation rather than N
    separately-recommended active items (reconciliation doc §9's own explicit bar: "8 related
    suggestions must not become 8 active jobs by default").

    Requires at least `MIN_ITEMS_TO_COMPRESS` total rows across both lists (compressing a single
    item into a "program of one" is a no-op this function refuses, not silently allows). Every
    referenced id must already exist and belong to `owner_id` -- raises otherwise; this function
    never creates the underlying rows it groups."""
    work_candidate_ids = list(work_candidate_ids or [])
    idea_ids = list(idea_ids or [])
    total = len(work_candidate_ids) + len(idea_ids)
    if total < MIN_ITEMS_TO_COMPRESS:
        raise StrategicCompressionError(
            f"compress_into_program requires at least {MIN_ITEMS_TO_COMPRESS} related rows to "
            f"compress, got {total}"
        )

    candidates = (
        list(
            db.execute(
                select(WorkCandidate).where(
                    WorkCandidate.owner_id == owner_id, WorkCandidate.id.in_(work_candidate_ids)
                )
            ).scalars()
        )
        if work_candidate_ids
        else []
    )
    if len(candidates) != len(set(work_candidate_ids)):
        raise StrategicCompressionError(
            "one or more work_candidate_ids are missing or belong to another owner"
        )

    ideas = (
        list(
            db.execute(
                select(IntelligenceIdea).where(
                    IntelligenceIdea.owner_id == owner_id, IntelligenceIdea.id.in_(idea_ids)
                )
            ).scalars()
        )
        if idea_ids
        else []
    )
    if len(ideas) != len(set(idea_ids)):
        raise StrategicCompressionError("one or more idea_ids are missing or belong to another owner")

    thread = create_thread(
        db,
        owner_id=owner_id,
        idempotency_key=idempotency_key,
        system_label=program_label or "strategic_compression_program",
        classification_basis="deterministic",
    )
    for candidate in candidates:
        add_member(
            db,
            owner_id=owner_id,
            thread_id=thread.id,
            member_kind="work_candidate",
            member_ref_id=candidate.id,
            # "deterministic_relationship" is one of migration 0040's real
            # ck_memory_thread_membership_basis CHECK values -- the same value
            # memory_threads.service.expand_thread() itself already uses for its own
            # deterministic membership additions; there is no dedicated "strategic_compression"
            # value in that CHECK, and this module does not widen it.
            membership_basis="deterministic_relationship",
            classification_basis="deterministic",
            actor_type="system",
            provenance={"program_thread_id": str(thread.id)},
        )
    for idea in ideas:
        add_member(
            db,
            owner_id=owner_id,
            thread_id=thread.id,
            member_kind="intelligence_idea",
            member_ref_id=idea.id,
            membership_basis="deterministic_relationship",
            classification_basis="deterministic",
            actor_type="system",
            provenance={"program_thread_id": str(thread.id)},
        )

    return {
        "owner_id": str(owner_id),
        "program_thread_id": str(thread.id),
        "member_count": total,
        "work_candidate_ids": [str(c.id) for c in candidates],
        "idea_ids": [str(i.id) for i in ideas],
        "proposal_count": 1,
        "authorized": False,
        "authority_impact": "NONE",
        "recommendation": (
            f"formalize as one coherent program -- {total} related item(s) grouped, not raised "
            "separately"
        ),
    }
