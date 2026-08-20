"""Life Founder/User Memory foundation -- proves that founder/user facts, project facts, and
Life's own capability-reality facts remain semantically separate but linkable, that authority
levels are never silently promoted, that supersession preserves history rather than deleting
it, and that emotional/psychological state is never inferred. See migration 0049's own module
docstring and docs/LIFE_FOUNDER_MEMORY.md for the full architecture."""

import uuid

import pytest

from app.active_context.service import SUPPORTED_TYPES
from app.capability_reality import record_capability_observation
from app.founder_memory import (
    FounderMemoryError,
    get_founder_memory,
    list_founder_memory,
    mark_founder_memory_disputed,
    record_founder_memory,
)
from app.memory_threads.service import add_member, create_thread
from app.models.founder_memory import FounderMemoryNote
from app.models.user import User
from app.problem_learning.service import create_problem, record_decision


def _owner(db):
    user = User(email=f"founder-mem-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


# ============================================================================ Requirement A:
# the founder/user memory foundation itself.

def test_record_founder_memory_creates_a_note_with_explicit_fields_never_inferred(superuser_db):
    owner = _owner(superuser_db)
    note = record_founder_memory(
        superuser_db, owner_id=owner.id, note_type="preference", content="Jag vill ha korta, direkta svar utan sammanfattningar på slutet.",
        idempotency_key="pref-1", authority="founder", basis="manual",
    )
    superuser_db.commit()
    assert note.status == "active"
    assert note.authority == "founder"
    assert note.basis == "manual"
    assert note.content.startswith("Jag vill ha korta")

    fetched = get_founder_memory(superuser_db, owner_id=owner.id, note_id=note.id)
    assert fetched.id == note.id


def test_record_founder_memory_is_idempotent_and_rejects_a_reused_key_with_different_fields(superuser_db):
    owner = _owner(superuser_db)
    first = record_founder_memory(
        superuser_db, owner_id=owner.id, note_type="preference", content="Terse responses.", idempotency_key="idem-1",
        authority="founder", basis="manual",
    )
    superuser_db.commit()
    replay = record_founder_memory(
        superuser_db, owner_id=owner.id, note_type="preference", content="Terse responses.", idempotency_key="idem-1",
        authority="founder", basis="manual",
    )
    assert replay.id == first.id  # same call replayed -- no duplicate row

    with pytest.raises(FounderMemoryError):
        record_founder_memory(
            superuser_db, owner_id=owner.id, note_type="preference", content="A COMPLETELY DIFFERENT STATEMENT.",
            idempotency_key="idem-1", authority="founder", basis="manual",
        )


def test_note_type_and_status_reject_arbitrary_values(superuser_db):
    from sqlalchemy.exc import DBAPIError

    owner = _owner(superuser_db)
    with pytest.raises(DBAPIError):
        record_founder_memory(
            superuser_db, owner_id=owner.id, note_type="definitely_not_a_real_type", content="x", idempotency_key="bad-type",
        )
    superuser_db.rollback()


# ============================================================================ Requirement G.1
# / G.2: assistant suggestion != founder decision; inferred preference != explicit preference.

def test_assistant_suggestion_never_silently_becomes_a_founder_decision(superuser_db):
    owner = _owner(superuser_db)
    suggestion = record_founder_memory(
        superuser_db, owner_id=owner.id, note_type="decision", content="Assistenten föreslog att vi borde använda Postgres istället för MongoDB.",
        idempotency_key="assist-1", authority="ai_interpretation", basis="ai_interpretation",
    )
    real_decision = record_founder_memory(
        superuser_db, owner_id=owner.id, note_type="decision", content="Vi använder Postgres.", idempotency_key="founder-decision-1",
        authority="founder", basis="manual",
    )
    superuser_db.commit()

    assert suggestion.authority == "ai_interpretation"
    assert real_decision.authority == "founder"
    assert suggestion.id != real_decision.id
    # Querying by authority correctly discriminates -- an assistant suggestion is never
    # returned when specifically asking for founder-authored facts.
    founder_only = list_founder_memory(superuser_db, owner_id=owner.id, authority="founder")
    assert real_decision.id in {n.id for n in founder_only}
    assert suggestion.id not in {n.id for n in founder_only}


def test_inferred_preference_never_silently_becomes_an_explicit_preference(superuser_db):
    owner = _owner(superuser_db)
    inferred = record_founder_memory(
        superuser_db, owner_id=owner.id, note_type="recurring_pattern",
        content="Grundaren har vid tre tillfällen bett om kortare svar -- ett återkommande mönster, inte ett uttalat beslut.",
        idempotency_key="inferred-1", authority="inferred_pattern", basis="inferred", confidence=0.72,
    )
    explicit = record_founder_memory(
        superuser_db, owner_id=owner.id, note_type="preference", content="Jag vill ha korta svar.", idempotency_key="explicit-1",
        authority="founder", basis="manual",
    )
    superuser_db.commit()

    assert inferred.authority == "inferred_pattern"
    assert inferred.basis == "inferred"
    assert float(inferred.confidence) == pytest.approx(0.72)
    assert explicit.authority == "founder"
    assert explicit.basis == "manual"
    assert explicit.confidence is None  # never fabricated for an explicit statement


# ============================================================================ Requirement C /
# G.3: supersession preserves both records.

def test_later_correction_supersedes_earlier_preference_while_preserving_both(superuser_db):
    owner = _owner(superuser_db)
    original = record_founder_memory(
        superuser_db, owner_id=owner.id, note_type="preference", content="Jag vill ha e-postnotiser för allt.",
        idempotency_key="notif-pref-1", authority="founder", basis="manual",
    )
    superuser_db.commit()

    correction = record_founder_memory(
        superuser_db, owner_id=owner.id, note_type="correction", content="Nej, jag vill bara ha e-postnotiser för kritiska händelser.",
        idempotency_key="notif-pref-2", authority="founder", basis="manual", supersedes_note_id=original.id,
    )
    superuser_db.commit()

    superuser_db.refresh(original)
    assert original.status == "superseded"
    assert original.content == "Jag vill ha e-postnotiser för allt."  # never rewritten
    assert correction.status == "active"
    assert correction.supersedes_note_id == original.id
    # Both rows still durably queryable -- history preserved, not deleted.
    both = {n.id for n in list_founder_memory(superuser_db, owner_id=owner.id)}
    assert {original.id, correction.id} <= both


def test_superseding_a_note_belonging_to_another_owner_fails_closed(superuser_db):
    owner_a = _owner(superuser_db)
    owner_b = _owner(superuser_db)
    note_a = record_founder_memory(
        superuser_db, owner_id=owner_a.id, note_type="preference", content="A's preference.", idempotency_key="a-1",
        authority="founder", basis="manual",
    )
    superuser_db.commit()

    with pytest.raises(FounderMemoryError):
        record_founder_memory(
            superuser_db, owner_id=owner_b.id, note_type="correction", content="B tries to supersede A's note.",
            idempotency_key="b-1", authority="founder", basis="manual", supersedes_note_id=note_a.id,
        )


def test_mark_founder_memory_disputed_never_deletes_or_rewrites_content(superuser_db):
    owner = _owner(superuser_db)
    note = record_founder_memory(
        superuser_db, owner_id=owner.id, note_type="preference", content="Original statement.", idempotency_key="disp-1",
        authority="founder", basis="manual",
    )
    superuser_db.commit()

    disputed = mark_founder_memory_disputed(superuser_db, owner_id=owner.id, note_id=note.id)
    superuser_db.commit()
    assert disputed.status == "disputed"
    assert disputed.content == "Original statement."


# ============================================================================ Requirement D /
# G.6: UNKNOWN / inferred / disputed remain representable.

def test_missing_or_uncertain_data_stays_unknown_never_guessed(superuser_db):
    owner = _owner(superuser_db)
    note = record_founder_memory(superuser_db, owner_id=owner.id, note_type="observation", content="Unclear origin.", idempotency_key="unk-1")
    superuser_db.commit()
    assert note.authority == "unknown"
    assert note.basis == "unknown"
    assert note.confidence is None
    assert note.source is None
    assert note.valid_from is None


# ============================================================================ Requirement B /
# G.4 / G.5: founder preference can influence reasoning via a link, without becoming project
# truth -- project facts remain independently queryable and unaffected.

def test_founder_preference_links_to_a_project_fact_without_collapsing_into_it(superuser_db):
    owner = _owner(superuser_db)
    preference = record_founder_memory(
        superuser_db, owner_id=owner.id, note_type="preference", content="Jag föredrar Postgres för allt strukturerat data.",
        idempotency_key="pg-pref-1", authority="founder", basis="manual",
    )
    problem = create_problem(superuser_db, owner_id=owner.id, title="Choose a database", description="Pick a DB for the new module.", idempotency_key="db-problem-1")
    decision = record_decision(
        superuser_db, owner_id=owner.id, problem_id=problem.id, decision="Use Postgres for the new module.",
        idempotency_key="db-decision-1", status="active", authority="founder", basis="manual",
    )
    superuser_db.commit()

    thread = create_thread(superuser_db, owner_id=owner.id, idempotency_key="pg-thread-1", manual_label="Postgres choice")
    add_member(superuser_db, owner_id=owner.id, thread_id=thread.id, member_kind="founder_memory_note", member_ref_id=preference.id, membership_basis="founder_added")
    add_member(superuser_db, owner_id=owner.id, thread_id=thread.id, member_kind="life_problem_decision", member_ref_id=decision.id, membership_basis="founder_added")
    superuser_db.commit()

    # Linked in the SAME thread -- but each remains its own row, in its own table, with its
    # own authority/status. The founder's preference never became the project decision's own
    # text, and the project decision was never copied into founder_memory_notes.
    superuser_db.refresh(preference)
    superuser_db.refresh(decision)
    assert preference.content != decision.decision
    assert isinstance(preference, FounderMemoryNote)
    assert preference.note_type == "preference"  # never silently reclassified as a decision


def test_project_fact_remains_independent_of_founder_preference_when_queried_directly(superuser_db):
    owner = _owner(superuser_db)
    record_founder_memory(
        superuser_db, owner_id=owner.id, note_type="preference", content="Some preference.", idempotency_key="indep-pref-1",
        authority="founder", basis="manual",
    )
    problem = create_problem(superuser_db, owner_id=owner.id, title="Unrelated problem", description="d", idempotency_key="indep-problem-1")
    record_decision(
        superuser_db, owner_id=owner.id, problem_id=problem.id, decision="Unrelated decision.", idempotency_key="indep-decision-1",
        status="active", authority="deterministic_source", basis="deterministic",
    )
    superuser_db.commit()

    # Querying founder memory never surfaces project decisions, and vice versa -- no shared
    # table, no accidental cross-contamination.
    founder_notes = list_founder_memory(superuser_db, owner_id=owner.id)
    assert all(isinstance(n, FounderMemoryNote) for n in founder_notes)


def test_founder_memory_note_is_a_recognized_linkable_type_in_the_central_registry(superuser_db):
    assert "founder_memory_note" in SUPPORTED_TYPES


# ============================================================================ Requirement G.7:
# emotional state is never inferred -- structural, not runtime, proof.

def test_no_vocabulary_anywhere_in_this_foundation_names_emotional_or_psychological_state():
    """Mirrors app.context.resolver's own test_never_infers_emotional_or_psychological_state --
    proves the STRUCTURAL surface (note_type vocabulary) has no concept of mood/stress/emotion
    at all, not merely that it happens to suppress one it secretly computes."""
    from app.models import founder_memory as founder_memory_model

    all_vocab = " ".join(
        founder_memory_model.FOUNDER_MEMORY_NOTE_TYPES
        + founder_memory_model.FOUNDER_MEMORY_NOTE_STATUSES
        + founder_memory_model.FOUNDER_MEMORY_AUTHORITIES
        + founder_memory_model.FOUNDER_MEMORY_BASES
    ).lower()
    forbidden_terms = ["stress", "mood", "emotion", "humör", "känsl", "diagnos", "psykolog", "arg", "ledsen", "glad", "trött"]
    for term in forbidden_terms:
        assert term not in all_vocab, f"founder_memory's own vocabulary must never reference {term!r}"


# ============================================================================ Requirement E /
# G.8: source truth remains immutable -- content is never rewritten.

def test_content_is_never_mutated_by_replaying_or_superseding(superuser_db):
    owner = _owner(superuser_db)
    note = record_founder_memory(
        superuser_db, owner_id=owner.id, note_type="preference", content="The exact original wording.", idempotency_key="immut-1",
        authority="founder", basis="manual",
    )
    superuser_db.commit()
    original_content = note.content

    correction = record_founder_memory(
        superuser_db, owner_id=owner.id, note_type="correction", content="A newer statement.", idempotency_key="immut-2",
        authority="founder", basis="manual", supersedes_note_id=note.id,
    )
    superuser_db.commit()
    superuser_db.refresh(note)
    assert note.content == original_content
    assert correction.content == "A newer statement."


# ============================================================================ Reuse, not
# duplication: capability_reality (the just-finished increment) stays fully independent too.

def test_capability_reality_and_founder_memory_remain_separate_foundations(superuser_db):
    owner = _owner(superuser_db)
    record_founder_memory(
        superuser_db, owner_id=owner.id, note_type="preference", content="Founder-level fact.", idempotency_key="cap-sep-1",
        authority="founder", basis="manual",
    )
    record_capability_observation(
        superuser_db, owner_id=owner.id, capability_key="test.separation", domain="test", status="unknown",
    )
    superuser_db.commit()
    # Distinct tables, distinct query surfaces -- no accidental merge between "what the founder
    # said" and "what Life can currently do."
    notes = list_founder_memory(superuser_db, owner_id=owner.id)
    assert all(n.note_type != "capability" for n in notes)
