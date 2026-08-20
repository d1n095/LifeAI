"""Life Project Entities / Interpretation Queue -- proves SIGNAL PRODUCER != TRUTH WRITER
structurally: record_interpretation_proposal() never touches project_entities; only
promote_interpretation_proposal() can, and only with the caller's own explicit authority/basis,
never the proposal's own classifier confidence silently copied in. See migration 0054's own
module docstring and docs/LIFE_PROJECT_ENTITIES_INTERPRETATION.md for the full architecture."""

import uuid

import pytest
from sqlalchemy.exc import DBAPIError

from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import KnowledgeClaim
from app.models.user import User
from app.project_entities import (
    ProjectEntityError,
    dismiss_interpretation_proposal,
    get_interpretation_proposal,
    get_project_entity,
    list_current_project_entities,
    list_entity_relationships,
    list_interpretation_proposals,
    list_unreviewed_interpretation_proposals,
    mark_project_entity_superseded,
    promote_interpretation_proposal,
    record_entity_relationship,
    record_interpretation_proposal,
)


def _owner_with_claim(db, claim_text="Vi bör byta databas till Postgres."):
    user = User(email=f"pe-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    document = Document(title="Källa", source=DocumentSource.upload, uploaded_by=user.id, active_truth_status=ActiveTruthStatus.active)
    db.add(document)
    db.flush()
    claim = KnowledgeClaim(owner_id=user.id, source_id=document.id, claim_text=claim_text, extraction_version="v1")
    db.add(claim)
    db.flush()
    return user, claim


def test_record_interpretation_proposal_never_writes_to_project_entities(superuser_db):
    owner, claim = _owner_with_claim(superuser_db)
    superuser_db.commit()

    proposal = record_interpretation_proposal(
        superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="decision",
        idempotency_key="prop-1", classifier_strategy="claim_type_extraction_v1", classifier_confidence="certain",
    )
    superuser_db.commit()

    assert proposal.status == "unreviewed"
    assert proposal.promoted_to_entity_id is None
    # No authority/basis vocabulary exists on this row at all -- structural, not just untested.
    assert not hasattr(proposal, "authority")
    assert not hasattr(proposal, "basis")


def test_record_interpretation_proposal_is_idempotent_and_rejects_a_reused_key_with_different_fields(superuser_db):
    owner, claim = _owner_with_claim(superuser_db)
    superuser_db.commit()
    first = record_interpretation_proposal(superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="decision", idempotency_key="idem-prop")
    superuser_db.commit()
    replay = record_interpretation_proposal(superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="decision", idempotency_key="idem-prop")
    assert replay.id == first.id

    with pytest.raises(ProjectEntityError):
        record_interpretation_proposal(superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="idea", idempotency_key="idem-prop")


def test_proposed_entity_type_rejects_arbitrary_values(superuser_db):
    owner, claim = _owner_with_claim(superuser_db)
    superuser_db.commit()
    with pytest.raises(DBAPIError):
        record_interpretation_proposal(superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="definitely_a_goal_trust_me", idempotency_key="bad-type")
    superuser_db.rollback()


def test_promoting_a_proposal_requires_the_callers_own_explicit_authority_never_the_classifiers_confidence(superuser_db):
    """The core structural proof: a classifier_confidence='certain' proposal does NOT imply
    authority='founder' on promotion -- the caller must assert it themselves."""

    owner, claim = _owner_with_claim(superuser_db)
    superuser_db.commit()
    proposal = record_interpretation_proposal(
        superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="decision",
        idempotency_key="promo-1", classifier_strategy="claim_type_extraction_v1", classifier_confidence="certain",
    )
    superuser_db.commit()

    promoted_proposal, entity = promote_interpretation_proposal(
        superuser_db, owner_id=owner.id, proposal_id=proposal.id, entity_type="decision",
        title="Byt databas till Postgres.", authority="founder", basis="manual",
        entity_idempotency_key="promo-entity-1",
    )
    superuser_db.commit()

    assert promoted_proposal.status == "promoted"
    assert promoted_proposal.promoted_to_entity_id == entity.id
    assert entity.authority == "founder"  # the REVIEWER's assertion, never proposal.classifier_confidence
    fetched_entity = get_project_entity(superuser_db, owner_id=owner.id, entity_id=entity.id)
    assert fetched_entity.provenance["promoted_from_interpretation_proposal_id"] == str(proposal.id)
    assert fetched_entity.derived_from_claim_id == claim.id


def test_a_low_confidence_proposal_can_still_be_promoted_with_high_authority_and_vice_versa(superuser_db):
    """Proves the two are genuinely decoupled, not just usually correlated."""

    owner, claim = _owner_with_claim(superuser_db)
    superuser_db.commit()
    proposal = record_interpretation_proposal(
        superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="idea",
        idempotency_key="decouple-1", classifier_confidence="uncertain",
    )
    superuser_db.commit()

    _, entity = promote_interpretation_proposal(
        superuser_db, owner_id=owner.id, proposal_id=proposal.id, entity_type="idea",
        title="Confirmed directly by founder despite low-confidence origin.",
        authority="founder", basis="manual", entity_idempotency_key="decouple-entity-1",
    )
    superuser_db.commit()
    assert entity.authority == "founder"


def test_cannot_promote_an_already_reviewed_proposal_twice(superuser_db):
    owner, claim = _owner_with_claim(superuser_db)
    superuser_db.commit()
    proposal = record_interpretation_proposal(superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="decision", idempotency_key="twice-1")
    superuser_db.commit()
    promote_interpretation_proposal(
        superuser_db, owner_id=owner.id, proposal_id=proposal.id, entity_type="decision", title="x",
        authority="founder", basis="manual", entity_idempotency_key="twice-entity-1",
    )
    superuser_db.commit()

    with pytest.raises(ProjectEntityError):
        promote_interpretation_proposal(
            superuser_db, owner_id=owner.id, proposal_id=proposal.id, entity_type="decision", title="y",
            authority="founder", basis="manual", entity_idempotency_key="twice-entity-2",
        )


def test_dismissing_a_proposal_never_deletes_it_and_records_a_reason(superuser_db):
    owner, claim = _owner_with_claim(superuser_db)
    superuser_db.commit()
    proposal = record_interpretation_proposal(superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="decision", idempotency_key="dismiss-1")
    superuser_db.commit()

    dismissed = dismiss_interpretation_proposal(superuser_db, owner_id=owner.id, proposal_id=proposal.id, reason="Actually just a historical note, not a real decision.")
    superuser_db.commit()
    assert dismissed.status == "dismissed"
    assert "historical" in dismissed.dismissed_reason

    fetched = get_interpretation_proposal(superuser_db, owner_id=owner.id, proposal_id=proposal.id)
    assert fetched is not None  # still durably queryable, not deleted


def test_list_unreviewed_interpretation_proposals_excludes_promoted_and_dismissed(superuser_db):
    owner, claim = _owner_with_claim(superuser_db)
    superuser_db.commit()
    unreviewed = record_interpretation_proposal(superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="idea", idempotency_key="list-1")
    promoted = record_interpretation_proposal(superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="decision", idempotency_key="list-2")
    dismissed = record_interpretation_proposal(superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="task_reference", idempotency_key="list-3")
    superuser_db.commit()
    promote_interpretation_proposal(superuser_db, owner_id=owner.id, proposal_id=promoted.id, entity_type="decision", title="x", authority="founder", basis="manual", entity_idempotency_key="list-entity-1")
    dismiss_interpretation_proposal(superuser_db, owner_id=owner.id, proposal_id=dismissed.id, reason="noise")
    superuser_db.commit()

    unreviewed_ids = {p.id for p in list_unreviewed_interpretation_proposals(superuser_db, owner_id=owner.id)}
    assert unreviewed_ids == {unreviewed.id}
    all_ids = {p.id for p in list_interpretation_proposals(superuser_db, owner_id=owner.id)}
    assert all_ids == {unreviewed.id, promoted.id, dismissed.id}


def test_promoting_or_dismissing_a_proposal_belonging_to_another_owner_fails_closed(superuser_db):
    owner_a, claim_a = _owner_with_claim(superuser_db)
    owner_b, _ = _owner_with_claim(superuser_db)
    superuser_db.commit()
    proposal = record_interpretation_proposal(superuser_db, owner_id=owner_a.id, source_claim_id=claim_a.id, proposed_entity_type="decision", idempotency_key="cross-1")
    superuser_db.commit()

    with pytest.raises(ProjectEntityError):
        dismiss_interpretation_proposal(superuser_db, owner_id=owner_b.id, proposal_id=proposal.id, reason="not mine")
    with pytest.raises(ProjectEntityError):
        promote_interpretation_proposal(
            superuser_db, owner_id=owner_b.id, proposal_id=proposal.id, entity_type="decision", title="stolen",
            authority="founder", basis="manual", entity_idempotency_key="cross-entity-1",
        )


def test_decided_fields_require_entity_type_decision(superuser_db):
    """DB-level CHECK constraint: decided_by/decided_at can only be set when entity_type is
    actually 'decision' -- promotion cannot silently attach decision metadata to an idea."""

    owner, claim = _owner_with_claim(superuser_db)
    superuser_db.commit()
    proposal = record_interpretation_proposal(superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="idea", idempotency_key="decision-check-1")
    superuser_db.commit()

    with pytest.raises(DBAPIError):
        promote_interpretation_proposal(
            superuser_db, owner_id=owner.id, proposal_id=proposal.id, entity_type="idea", title="x",
            authority="founder", basis="manual", entity_idempotency_key="decision-check-entity-1",
            decided_by="founder",
        )
    superuser_db.rollback()


def test_mark_project_entity_superseded_never_deletes_or_mutates_content(superuser_db):
    owner, claim = _owner_with_claim(superuser_db)
    superuser_db.commit()
    proposal = record_interpretation_proposal(superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="decision", idempotency_key="super-1")
    superuser_db.commit()
    _, old_entity = promote_interpretation_proposal(
        superuser_db, owner_id=owner.id, proposal_id=proposal.id, entity_type="decision", title="Use Postgres.",
        authority="founder", basis="manual", entity_idempotency_key="super-entity-1",
    )
    superuser_db.commit()

    proposal2 = record_interpretation_proposal(superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="decision", idempotency_key="super-2")
    superuser_db.commit()
    _, new_entity = promote_interpretation_proposal(
        superuser_db, owner_id=owner.id, proposal_id=proposal2.id, entity_type="decision",
        title="Reconsidered: use SQLite for local cache instead.",
        authority="founder", basis="manual", entity_idempotency_key="super-entity-2",
    )
    superuser_db.commit()

    mark_project_entity_superseded(superuser_db, owner_id=owner.id, entity_id=old_entity.id, superseded_by_entity_id=new_entity.id)
    superuser_db.commit()

    refetched_old = get_project_entity(superuser_db, owner_id=owner.id, entity_id=old_entity.id)
    assert refetched_old.status == "superseded"
    assert refetched_old.title == "Use Postgres."  # content untouched, never rewritten


def test_list_current_project_entities_excludes_superseded(superuser_db):
    owner, claim = _owner_with_claim(superuser_db)
    superuser_db.commit()
    proposal1 = record_interpretation_proposal(superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="decision", idempotency_key="current-old")
    superuser_db.commit()
    _, old_entity = promote_interpretation_proposal(
        superuser_db, owner_id=owner.id, proposal_id=proposal1.id, entity_type="decision", title="Old decision.",
        authority="founder", basis="manual", entity_idempotency_key="current-entity-old",
    )
    superuser_db.commit()

    proposal2 = record_interpretation_proposal(superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="decision", idempotency_key="current-new")
    superuser_db.commit()
    _, new_entity = promote_interpretation_proposal(
        superuser_db, owner_id=owner.id, proposal_id=proposal2.id, entity_type="decision", title="Still relevant.",
        authority="founder", basis="manual", entity_idempotency_key="current-entity-new",
    )
    superuser_db.commit()
    mark_project_entity_superseded(superuser_db, owner_id=owner.id, entity_id=old_entity.id, superseded_by_entity_id=new_entity.id)
    superuser_db.commit()

    current_ids = {e.id for e in list_current_project_entities(superuser_db, owner_id=owner.id)}
    assert new_entity.id in current_ids
    assert old_entity.id not in current_ids


def test_record_entity_relationship_rejects_self_relationship(superuser_db):
    owner, claim = _owner_with_claim(superuser_db)
    superuser_db.commit()
    proposal = record_interpretation_proposal(superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="idea", idempotency_key="rel-self-1")
    superuser_db.commit()
    _, entity = promote_interpretation_proposal(
        superuser_db, owner_id=owner.id, proposal_id=proposal.id, entity_type="idea", title="x",
        authority="founder", basis="manual", entity_idempotency_key="rel-self-entity-1",
    )
    superuser_db.commit()

    with pytest.raises(ProjectEntityError):
        record_entity_relationship(superuser_db, owner_id=owner.id, from_entity_id=entity.id, to_entity_id=entity.id, relationship_type="relates_to")


def test_record_and_list_entity_relationships(superuser_db):
    owner, claim = _owner_with_claim(superuser_db)
    superuser_db.commit()
    proposal1 = record_interpretation_proposal(superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="idea", idempotency_key="rel-1")
    proposal2 = record_interpretation_proposal(superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="decision", idempotency_key="rel-2")
    superuser_db.commit()
    _, idea = promote_interpretation_proposal(superuser_db, owner_id=owner.id, proposal_id=proposal1.id, entity_type="idea", title="idea", authority="founder", basis="manual", entity_idempotency_key="rel-entity-1")
    _, decision = promote_interpretation_proposal(superuser_db, owner_id=owner.id, proposal_id=proposal2.id, entity_type="decision", title="decision", authority="founder", basis="manual", entity_idempotency_key="rel-entity-2")
    superuser_db.commit()

    record_entity_relationship(superuser_db, owner_id=owner.id, from_entity_id=idea.id, to_entity_id=decision.id, relationship_type="derived_from")
    superuser_db.commit()

    rels = list_entity_relationships(superuser_db, owner_id=owner.id, entity_id=idea.id)
    assert len(rels) == 1
    assert rels[0].relationship_type == "derived_from"
    # visible from either side of the edge
    rels_from_decision = list_entity_relationships(superuser_db, owner_id=owner.id, entity_id=decision.id)
    assert len(rels_from_decision) == 1
