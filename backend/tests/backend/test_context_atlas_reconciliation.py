"""Context Atlas acceptance — reconciliation decisions (Workstream G, cases 6-12).

Also asserts the authorization-negative invariant: no outcome ever authorizes execution.
"""

import pytest

from app.context_atlas.reconciliation import ContextObservation, Outcome, reconcile
from app.context_atlas.relationships import CONCEPT_MAPPINGS, propose_relationship


def test_old_requirement_belonging_to_another_project_is_linked_not_implemented():
    decision = reconcile(ContextObservation(project_scope="other_project"))

    assert decision.outcome is Outcome.link
    assert decision.execution_authorized is False
    assert "another project" in decision.reasons[0]


def test_already_implemented_capability_is_reused():
    decision = reconcile(
        ContextObservation(
            capability_exists=True,
            capability_evidence=("backend/app/routers/library.py",),
        )
    )

    assert decision.outcome is Outcome.reuse
    assert "backend/app/routers/library.py" in decision.reasons
    assert decision.execution_authorized is False


def test_active_assignment_makes_lifeai_wait():
    decision = reconcile(
        ContextObservation(active_assignment="claude/founder-alpha-ws-b-knowledge")
    )

    assert decision.outcome is Outcome.wait
    assert "claude/founder-alpha-ws-b-knowledge" in decision.reasons[0]
    assert decision.execution_authorized is False


def test_later_superseding_record_wins():
    decision = reconcile(ContextObservation(superseded_by="docs/DECISION-2026-11-24.md"))

    assert decision.outcome is Outcome.link
    assert "supersedes" in decision.reasons[0]


def test_rejected_prior_decision_is_rejected_again():
    decision = reconcile(ContextObservation(prior_decision="rejected"))

    assert decision.outcome is Outcome.reject
    assert decision.execution_authorized is False


def test_postponed_prior_decision_makes_lifeai_wait():
    assert reconcile(ContextObservation(prior_decision="postponed")).outcome is Outcome.wait


def test_ambiguous_historical_context_refuses_to_start_work():
    decision = reconcile(
        ContextObservation(ambiguous=True, missing_evidence=("which project owns it",))
    )

    assert decision.outcome is Outcome.ask
    assert decision.required_evidence == ("which project owns it",)
    assert "do not start work" in decision.next_step
    assert decision.execution_authorized is False


def test_missing_evidence_names_what_is_missing_instead_of_inventing_conclusion():
    decision = reconcile(ContextObservation(evidence_sufficient=False))

    assert decision.outcome is Outcome.ask
    assert decision.required_evidence
    assert all(isinstance(item, str) and item for item in decision.required_evidence)


def test_similarity_alone_leads_to_comparison_not_identity():
    decision = reconcile(ContextObservation(similarity_to_existing=0.82))

    assert decision.outcome is Outcome.compare
    assert "not proven identical" in decision.reasons[0]


def test_founder_decision_requirement_is_escalated():
    decision = reconcile(ContextObservation(founder_decision_required=True))

    assert decision.outcome is Outcome.ask
    assert "founder" in decision.next_step


def test_knowledge_only_observation_is_documented_not_assigned():
    decision = reconcile(ContextObservation(action_requested=False))

    assert decision.outcome is Outcome.document
    assert "do not create an assignment" in decision.next_step


def test_uncovered_gap_is_proposed_without_authorization():
    decision = reconcile(ContextObservation())

    assert decision.outcome is Outcome.propose
    assert "needs normal authorization" in decision.next_step
    assert decision.execution_authorized is False


@pytest.mark.parametrize(
    "observation",
    [
        ContextObservation(ambiguous=True),
        ContextObservation(project_scope="other_project"),
        ContextObservation(active_assignment="x"),
        ContextObservation(capability_exists=True),
        ContextObservation(prior_decision="rejected"),
        ContextObservation(prior_decision="postponed"),
        ContextObservation(superseded_by="y"),
        ContextObservation(founder_decision_required=True),
        ContextObservation(similarity_to_existing=0.9),
        ContextObservation(action_requested=False),
        ContextObservation(),
    ],
)
def test_no_outcome_grants_execution_authority(observation):
    decision = reconcile(observation)
    assert decision.execution_authorized is False
    assert decision.reasons


def test_relationship_vocabulary_maps_onto_existing_schema_and_names_the_gaps():
    assert CONCEPT_MAPPINGS["DUPLICATES"].existing_type == "duplicates"
    assert CONCEPT_MAPPINGS["SUPERSEDES"].existing_type == "supersedes"
    assert CONCEPT_MAPPINGS["BELONGS_TO_PROJECT"].existing_type == "belongs_to"

    # These have no existing edge type; the mapping must say so rather than pick a
    # near-sounding value.
    for concept in ("SIMILAR_TO", "DEPENDS_ON", "IMPLEMENTS_REQUIREMENT", "VERIFIED_BY"):
        assert CONCEPT_MAPPINGS[concept].existing_type is None
        proposal = propose_relationship(concept)
        assert proposal.recordable is False
        assert proposal.requires_review is True


def test_unknown_relationship_concept_stays_unresolved():
    proposal = propose_relationship("TOTALLY_NEW_RELATION")

    assert proposal.existing_type is None
    assert proposal.recordable is False
    assert proposal.requires_review is True
    assert "unresolved" in proposal.reason
