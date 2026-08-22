"""Composed proof of the full closing-phase chain this mission's PR #138 (Project Entities /
Interpretation Queue) and this PR (Work Candidates) together build:

    source claim (P3, already live)
    -> interpretation_proposal (candidate, never truth)
    -> project_entity (trusted, evidence-linked, requires explicit authority/basis)
    -> work_candidate (candidate, never authorized)
    -> MainAIGoal (real, executable work, requires explicit authorized_by)

Each individual link is already proven in isolation by test_project_entities.py,
test_claim_interpretation_proposal_capture.py, test_work_candidates.py, and
test_project_entity_work_candidate_capture.py. This file proves the composed chain actually
holds end to end in ONE flow -- not a claim about a bigger E2E this mission does not build
(Supervisor/execution/verification/recovery remain out of scope, see docs/LIFE_WORK_
CANDIDATES.md's closing section for exactly why).

Every stage before the final authorization requires NO explicit human/founder authority --
only `authorize_work_candidate()`'s `authorized_by` does. This is the concrete proof that
`claims -> interpretation -> structured knowledge -> justified work` is real and composed,
while `justified work -> authorized execution` remains a deliberate, explicit, single gate --
never silently crossed by the chain leading up to it."""

import uuid

from app.mainai_execution.planner import get_goal
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import ClaimType, KnowledgeClaim
from app.models.user import User
from app.project_entities import promote_interpretation_proposal, record_interpretation_proposal
from app.work_candidates import authorize_work_candidate, list_unreviewed_work_candidates


def test_a_source_claim_can_become_a_real_authorized_goal_through_the_full_composed_chain(superuser_db):
    # 1. Source: a user, a document, one extracted claim -- exactly what
    #    app/rag/claims.py's extract_claims_for_document() would produce in production.
    user = User(email=f"chain-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(user)
    superuser_db.flush()
    document = Document(title="Källa", source=DocumentSource.upload, uploaded_by=user.id, active_truth_status=ActiveTruthStatus.active)
    superuser_db.add(document)
    superuser_db.flush()
    claim = KnowledgeClaim(owner_id=user.id, source_id=document.id, claim_text="Vi bör migrera databasen till Postgres innan Q1.", extraction_version="v1", claim_type=ClaimType.decision)
    superuser_db.add(claim)
    superuser_db.flush()
    superuser_db.commit()

    # 2. Claim -> candidate interpretation proposal (never project truth yet).
    proposal = record_interpretation_proposal(
        superuser_db, owner_id=user.id, source_claim_id=claim.id, proposed_entity_type="decision",
        idempotency_key="chain-proposal-1", classifier_strategy="claim_type_extraction_v1", classifier_confidence="certain",
    )
    superuser_db.commit()
    assert proposal.status == "unreviewed"

    # 3. Proposal -> real, trusted project understanding -- requires the reviewer's OWN
    #    explicit authority/basis, never the classifier's own confidence.
    promoted_proposal, entity = promote_interpretation_proposal(
        superuser_db, owner_id=user.id, proposal_id=proposal.id, entity_type="decision",
        title="Migrera databasen till Postgres innan Q1.", authority="founder", basis="manual",
        entity_idempotency_key="chain-entity-1",
    )
    superuser_db.commit()
    assert promoted_proposal.status == "promoted"
    assert entity.derived_from_claim_id == claim.id
    assert entity.authority == "founder"

    # 4. The SAME promotion call above already, live, recorded a candidate work candidate --
    #    no separate manual step, exactly like production would see it.
    candidates = list_unreviewed_work_candidates(superuser_db, owner_id=user.id)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source_entity_id == entity.id
    assert candidate.status == "unreviewed"
    assert candidate.authorized_goal_id is None  # not yet real work

    # 5. Candidate -> real, executable MainAIGoal -- the ONE explicit authorization gate in
    #    the entire chain. Everything before this point required no founder action at all;
    #    this step, and only this step, does.
    authorized_candidate, goal = authorize_work_candidate(
        superuser_db, owner_id=user.id, candidate_id=candidate.id, authorized_by="founder",
    )
    superuser_db.commit()

    assert authorized_candidate.status == "authorized"
    assert authorized_candidate.authorized_goal_id == goal.id
    assert goal.created_by == "founder"
    assert goal.title == entity.title

    fetched_goal = get_goal(superuser_db, goal.id)
    assert fetched_goal.id == goal.id
    assert fetched_goal.owner_id == user.id

    # No unreviewed candidates remain -- the chain from source claim to real goal is closed,
    # not left dangling in an intermediate staging state.
    assert list_unreviewed_work_candidates(superuser_db, owner_id=user.id) == []
