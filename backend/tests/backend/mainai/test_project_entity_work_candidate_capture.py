"""Proves app/project_entities/service.py's promote_interpretation_proposal() ->
work_candidates live wiring: promoting a decision/idea/task_reference entity records a
candidate work candidate (never a MainAIGoal directly); promoting a vision_statement/
open_question entity does not. Mirrors tests/backend/rag/test_claim_interpretation_proposal_
capture.py's own established pattern for proving a live signal-producer -> staging-layer
wiring, one level up the chain."""

import uuid

from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import KnowledgeClaim
from app.models.user import User
from app.models.work_candidate import WorkCandidate
from app.project_entities import promote_interpretation_proposal, record_interpretation_proposal


def _owner_with_claim(db, claim_text="Vi bör byta databas till Postgres."):
    user = User(email=f"pewc-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    document = Document(title="Källa", source=DocumentSource.upload, uploaded_by=user.id, active_truth_status=ActiveTruthStatus.active)
    db.add(document)
    db.flush()
    claim = KnowledgeClaim(owner_id=user.id, source_id=document.id, claim_text=claim_text, extraction_version="v1")
    db.add(claim)
    db.flush()
    return user, claim


def test_promoting_a_decision_entity_records_a_candidate_work_candidate_never_a_goal_directly(superuser_db):
    owner, claim = _owner_with_claim(superuser_db)
    superuser_db.commit()
    proposal = record_interpretation_proposal(superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="decision", idempotency_key="wc-wire-1")
    superuser_db.commit()

    _, entity = promote_interpretation_proposal(
        superuser_db, owner_id=owner.id, proposal_id=proposal.id, entity_type="decision", title="Byt databas till Postgres.",
        authority="founder", basis="manual", entity_idempotency_key="wc-wire-entity-1",
    )
    superuser_db.commit()

    candidates = superuser_db.query(WorkCandidate).filter_by(source_entity_id=entity.id).all()
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.status == "unreviewed"
    assert candidate.authorized_goal_id is None
    assert candidate.title == "Byt databas till Postgres."


def test_promoting_a_vision_statement_entity_records_no_work_candidate(superuser_db):
    owner, claim = _owner_with_claim(superuser_db, "Vi vill bygga en produkt som hjälper folk spara tid.")
    superuser_db.commit()
    proposal = record_interpretation_proposal(superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="vision_statement", idempotency_key="wc-wire-2")
    superuser_db.commit()

    _, entity = promote_interpretation_proposal(
        superuser_db, owner_id=owner.id, proposal_id=proposal.id, entity_type="vision_statement", title="Save people time.",
        authority="founder", basis="manual", entity_idempotency_key="wc-wire-entity-2",
    )
    superuser_db.commit()

    candidates = superuser_db.query(WorkCandidate).filter_by(source_entity_id=entity.id).all()
    assert candidates == []


def test_a_failure_recording_the_work_candidate_never_breaks_the_promotion_itself(superuser_db, monkeypatch):
    """The non-fatal, SAVEPOINT-isolated guarantee: a bug in the observational side-effect
    can never take down the caller's own main result (the promotion), nor get silently rolled
    back together with it."""

    import app.work_candidates as work_candidates_package

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated work_candidates failure")

    # _record_work_candidate_if_actionable does `from app.work_candidates import
    # record_work_candidate` INSIDE its own function body, resolved fresh on every call --
    # patching the package-level name (not app.work_candidates.service's own) is what that
    # import statement actually looks up at call time.
    monkeypatch.setattr(work_candidates_package, "record_work_candidate", _boom)

    owner, claim = _owner_with_claim(superuser_db)
    superuser_db.commit()
    proposal = record_interpretation_proposal(superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="decision", idempotency_key="wc-wire-3")
    superuser_db.commit()

    promoted_proposal, entity = promote_interpretation_proposal(
        superuser_db, owner_id=owner.id, proposal_id=proposal.id, entity_type="decision", title="x",
        authority="founder", basis="manual", entity_idempotency_key="wc-wire-entity-3",
    )
    superuser_db.commit()

    assert promoted_proposal.status == "promoted"  # the promotion itself still succeeded
    fetched_entity_still_exists = superuser_db.query(type(entity)).filter_by(id=entity.id).one_or_none()
    assert fetched_entity_still_exists is not None
