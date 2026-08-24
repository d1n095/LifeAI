"""Proves app/work_candidates/service.py's authorize_work_candidate() -> execution_scope_
proposals live wiring: authorizing a work candidate derived from a task_reference/decision
ProjectEntity records a proposed execution scope (never an authorization envelope directly);
one derived from a vision_statement/open_question... never reaches work_candidates at all
(app.project_entities.service._ACTIONABLE_ENTITY_TYPES already excludes them), so this proves
the boundary the other direction: an idea-typed candidate proposes read-only capabilities
only. Mirrors tests/backend/mainai/test_project_entity_work_candidate_capture.py's own
established pattern for proving a live signal-producer -> staging-layer wiring, one level up
the chain."""

import uuid

from app.execution_envelopes import list_unreviewed_execution_scope_proposals
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import KnowledgeClaim
from app.models.user import User
from app.project_entities import promote_interpretation_proposal, record_interpretation_proposal
from app.work_candidates import authorize_work_candidate, list_unreviewed_work_candidates


def _owner_with_claim(db, claim_text="Vi bör byta databas till Postgres."):
    user = User(email=f"wcesc-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    document = Document(title="Källa", source=DocumentSource.upload, uploaded_by=user.id, active_truth_status=ActiveTruthStatus.active)
    db.add(document)
    db.flush()
    claim = KnowledgeClaim(owner_id=user.id, source_id=document.id, claim_text=claim_text, extraction_version="v1")
    db.add(claim)
    db.flush()
    return user, claim


def _authorize_a_work_candidate_for(superuser_db, owner, claim, entity_type):
    proposal = record_interpretation_proposal(superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type=entity_type, idempotency_key=f"esc-wire-prop-{uuid.uuid4()}")
    superuser_db.commit()
    _, entity = promote_interpretation_proposal(
        superuser_db, owner_id=owner.id, proposal_id=proposal.id, entity_type=entity_type, title="x",
        authority="founder", basis="manual", entity_idempotency_key=f"esc-wire-entity-{uuid.uuid4()}",
    )
    superuser_db.commit()
    candidate = next(c for c in list_unreviewed_work_candidates(superuser_db, owner_id=owner.id) if c.source_entity_id == entity.id)
    candidate_row, goal = authorize_work_candidate(superuser_db, owner_id=owner.id, candidate_id=candidate.id, authorized_by="founder")
    superuser_db.commit()
    return candidate_row, goal


def test_authorizing_a_decision_derived_work_candidate_proposes_repo_edit_scope(superuser_db):
    owner, claim = _owner_with_claim(superuser_db)
    superuser_db.commit()
    _, goal = _authorize_a_work_candidate_for(superuser_db, owner, claim, "decision")

    proposals = list_unreviewed_execution_scope_proposals(superuser_db, owner_id=owner.id)
    matching = [p for p in proposals if p.goal_id == goal.id]
    assert len(matching) == 1
    assert matching[0].proposed_capabilities == ["repo_read", "repo_edit", "run_tests"]
    assert matching[0].proposed_paths == []  # honest: no file-level signal exists yet
    assert matching[0].status == "unreviewed"


def test_authorizing_an_idea_derived_work_candidate_proposes_read_only_scope(superuser_db):
    owner, claim = _owner_with_claim(superuser_db, "Kanske borde vi bygga en snabbare cache.")
    superuser_db.commit()
    _, goal = _authorize_a_work_candidate_for(superuser_db, owner, claim, "idea")

    proposals = list_unreviewed_execution_scope_proposals(superuser_db, owner_id=owner.id)
    matching = [p for p in proposals if p.goal_id == goal.id]
    assert len(matching) == 1
    assert matching[0].proposed_capabilities == ["repo_read"]


def test_a_failure_proposing_the_execution_scope_never_breaks_the_work_candidate_authorization(superuser_db, monkeypatch):
    """The non-fatal, SAVEPOINT-isolated guarantee: a bug in the observational side-effect can
    never take down the caller's own main result (the goal authorization itself)."""

    import app.execution_envelopes as execution_envelopes_package

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated execution_envelopes failure")

    monkeypatch.setattr(execution_envelopes_package, "propose_execution_scope", _boom)

    owner, claim = _owner_with_claim(superuser_db)
    superuser_db.commit()
    candidate_row, goal = _authorize_a_work_candidate_for(superuser_db, owner, claim, "decision")

    assert candidate_row.status == "authorized"  # the authorization itself still succeeded
    assert goal.id is not None
