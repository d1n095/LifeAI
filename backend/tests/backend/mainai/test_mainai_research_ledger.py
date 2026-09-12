"""MainAI Research, Truth & Advisory Intelligence -- `app.mainai_research.research_ledger` --
proves the durable investigation/hypothesis/evidence/confidence-history/reopen-event ledger:
CONFIDENCE CHANGE MUST HAVE A REASON, REJECTED EVIDENCE != DELETED EVIDENCE, append-only history
(DB-enforced, real trigger), owner isolation, and SATURATED_FOR_NOW != PERMANENTLY CLOSED.

See docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the architecture this
module implements."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import InternalError

from app.mainai_research.research_ledger import (
    get_investigation,
    list_confidence_history,
    list_evidence_for_hypothesis,
    list_hypotheses_for_investigation,
    list_investigations,
    list_reopen_events,
    mark_investigation_saturated,
    record_evidence,
    record_hypothesis,
    record_investigation,
    reject_evidence,
    reopen_evidence,
    reopen_investigation,
    set_hypothesis_status,
    update_hypothesis_confidence,
)
from app.mainai_research.types import (
    EvidenceLifecycleStatus,
    EvidenceRole,
    EvidenceState,
    HypothesisStatus,
    InvestigationStatus,
    ResearchError,
)
from app.models.user import User


@pytest.fixture
def owner_id(superuser_db):
    user = User(email=f"rl-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(user)
    superuser_db.flush()
    superuser_db.commit()
    return user.id


def _investigation(db, owner_id, question="Why did X happen?"):
    return record_investigation(db, owner_id=owner_id, question=question, idempotency_key=f"inv-{uuid.uuid4()}")


def _hypothesis(db, owner_id, investigation_id, statement="A caused X"):
    return record_hypothesis(db, owner_id=owner_id, investigation_id=investigation_id, statement=statement, idempotency_key=f"hyp-{uuid.uuid4()}")


def test_record_investigation_is_idempotent(superuser_db, owner_id):
    first = _investigation(superuser_db, owner_id)
    superuser_db.commit()
    replay = record_investigation(superuser_db, owner_id=owner_id, question=first["question"], idempotency_key=first["idempotency_key"])
    assert replay["id"] == first["id"]


def test_record_investigation_rejects_key_reuse_with_different_question(superuser_db, owner_id):
    first = _investigation(superuser_db, owner_id)
    superuser_db.commit()
    with pytest.raises(ResearchError):
        record_investigation(superuser_db, owner_id=owner_id, question="different question", idempotency_key=first["idempotency_key"])


def test_confidence_change_requires_a_reason(superuser_db, owner_id):
    investigation = _investigation(superuser_db, owner_id)
    hypothesis = _hypothesis(superuser_db, owner_id, investigation["id"])
    superuser_db.commit()
    with pytest.raises(ResearchError):
        update_hypothesis_confidence(superuser_db, owner_id=owner_id, hypothesis_id=hypothesis["id"], new_confidence=0.8, reason="")


def test_confidence_history_is_append_only_and_durable(superuser_db, owner_id):
    investigation = _investigation(superuser_db, owner_id)
    hypothesis = _hypothesis(superuser_db, owner_id, investigation["id"])
    superuser_db.commit()
    update_hypothesis_confidence(superuser_db, owner_id=owner_id, hypothesis_id=hypothesis["id"], new_confidence=0.3, reason="one weak independent source")
    superuser_db.commit()
    update_hypothesis_confidence(superuser_db, owner_id=owner_id, hypothesis_id=hypothesis["id"], new_confidence=0.8, reason="second, genuinely independent source found")
    superuser_db.commit()

    history = list_confidence_history(superuser_db, owner_id=owner_id, hypothesis_id=hypothesis["id"])
    assert len(history) == 2
    assert float(history[0]["new_confidence"]) == pytest.approx(0.3)
    assert float(history[1]["previous_confidence"]) == pytest.approx(0.3)
    assert float(history[1]["new_confidence"]) == pytest.approx(0.8)
    assert all(h["reason"] for h in history)

    # Append-only, DB-enforced: a direct UPDATE against the ledger must be rejected.
    from sqlalchemy import text

    with pytest.raises(InternalError):
        superuser_db.execute(text("UPDATE mainai_research_confidence_history SET reason='tampered' WHERE hypothesis_id=:h"), {"h": hypothesis["id"]})
        superuser_db.flush()
    superuser_db.rollback()


def test_rejected_as_support_is_distinct_from_proven_false(superuser_db, owner_id):
    investigation = _investigation(superuser_db, owner_id)
    hypothesis = _hypothesis(superuser_db, owner_id, investigation["id"])
    superuser_db.commit()
    evidence = record_evidence(
        superuser_db, owner_id=owner_id, hypothesis_id=hypothesis["id"], role=EvidenceRole.SUPPORT,
        underlying_source_id="study-A", evidence_state=EvidenceState.PLAUSIBLE, summary="weak support",
        idempotency_key=f"ev-{uuid.uuid4()}",
    )
    superuser_db.commit()
    rejected = reject_evidence(superuser_db, owner_id=owner_id, evidence_id=evidence["id"], rationale="single non-independent source, insufficient", new_status=EvidenceLifecycleStatus.REJECTED_AS_SUPPORT)
    assert rejected["lifecycle_status"] == EvidenceLifecycleStatus.REJECTED_AS_SUPPORT.value
    assert rejected["lifecycle_status"] != EvidenceLifecycleStatus.PROVEN_FALSE.value
    # Rejected evidence is never deleted -- still queryable.
    still_there = list_evidence_for_hypothesis(superuser_db, owner_id=owner_id, hypothesis_id=hypothesis["id"])
    assert len(still_there) == 1
    assert still_there[0]["rejection_rationale"]


def test_reopen_evidence_preserves_original_rationale_and_records_event(superuser_db, owner_id):
    investigation = _investigation(superuser_db, owner_id)
    hypothesis = _hypothesis(superuser_db, owner_id, investigation["id"])
    superuser_db.commit()
    evidence = record_evidence(
        superuser_db, owner_id=owner_id, hypothesis_id=hypothesis["id"], role=EvidenceRole.SUPPORT,
        underlying_source_id="study-B", evidence_state=EvidenceState.PLAUSIBLE, summary="weak support",
        idempotency_key=f"ev-{uuid.uuid4()}",
    )
    superuser_db.commit()
    reject_evidence(superuser_db, owner_id=owner_id, evidence_id=evidence["id"], rationale="only one source at the time", new_status=EvidenceLifecycleStatus.REJECTED_AS_SUPPORT)
    superuser_db.commit()

    reopened = reopen_evidence(superuser_db, owner_id=owner_id, evidence_id=evidence["id"], reason="a second, independent source has since replicated this", new_lifecycle_status=EvidenceLifecycleStatus.VALID_SUPPORT)
    assert reopened["lifecycle_status"] == EvidenceLifecycleStatus.VALID_SUPPORT.value

    events = list_reopen_events(superuser_db, owner_id=owner_id, evidence_id=evidence["id"])
    assert len(events) == 1
    assert events[0]["original_lifecycle_status"] == EvidenceLifecycleStatus.REJECTED_AS_SUPPORT.value
    assert events[0]["new_lifecycle_status"] == EvidenceLifecycleStatus.VALID_SUPPORT.value


def test_saturated_for_now_is_not_permanently_closed(superuser_db, owner_id):
    investigation = _investigation(superuser_db, owner_id)
    superuser_db.commit()
    saturated = mark_investigation_saturated(superuser_db, owner_id=owner_id, investigation_id=investigation["id"], reason="repeated searches yield the same underlying sources")
    assert saturated["status"] == InvestigationStatus.SATURATED_FOR_NOW.value

    reopened = reopen_investigation(superuser_db, owner_id=owner_id, investigation_id=investigation["id"])
    assert reopened["status"] == InvestigationStatus.ACTIVE.value


def test_owner_isolation(superuser_db, owner_id):
    other = User(email=f"rl-other-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(other)
    superuser_db.flush()
    superuser_db.commit()

    investigation = _investigation(superuser_db, owner_id)
    superuser_db.commit()
    with pytest.raises(ResearchError):
        record_hypothesis(superuser_db, owner_id=other.id, investigation_id=investigation["id"], statement="x", idempotency_key="h1")


def test_falsification_round_increment(superuser_db, owner_id):
    investigation = _investigation(superuser_db, owner_id)
    hypothesis = _hypothesis(superuser_db, owner_id, investigation["id"])
    superuser_db.commit()
    updated = set_hypothesis_status(superuser_db, owner_id=owner_id, hypothesis_id=hypothesis["id"], status=HypothesisStatus.SURVIVED_FALSIFICATION, increment_falsification_round=True)
    assert updated["falsification_rounds"] == 1
    updated2 = set_hypothesis_status(superuser_db, owner_id=owner_id, hypothesis_id=hypothesis["id"], status=HypothesisStatus.SURVIVED_FALSIFICATION, increment_falsification_round=True)
    assert updated2["falsification_rounds"] == 2


def test_list_and_get_investigation_read_helpers(superuser_db, owner_id):
    """Additive, read-only helpers added for `app.mainai_cognitive_ops`'s cross-investigation
    reopen trigger -- no existing function in this module was modified."""

    active_inv = _investigation(superuser_db, owner_id, question="Active one?")
    saturated_inv = _investigation(superuser_db, owner_id, question="Saturated one?")
    mark_investigation_saturated(superuser_db, owner_id=owner_id, investigation_id=saturated_inv["id"], reason="no more leads")
    superuser_db.commit()

    all_investigations = list_investigations(superuser_db, owner_id=owner_id)
    assert {i["id"] for i in all_investigations} >= {active_inv["id"], saturated_inv["id"]}

    only_active = list_investigations(superuser_db, owner_id=owner_id, status=InvestigationStatus.ACTIVE)
    assert saturated_inv["id"] not in {i["id"] for i in only_active}

    fetched = get_investigation(superuser_db, owner_id=owner_id, investigation_id=active_inv["id"])
    assert fetched["id"] == active_inv["id"]
    assert get_investigation(superuser_db, owner_id=owner_id, investigation_id=uuid.uuid4()) is None

    hypothesis = _hypothesis(superuser_db, owner_id, active_inv["id"])
    superuser_db.commit()
    hyps = list_hypotheses_for_investigation(superuser_db, owner_id=owner_id, investigation_id=active_inv["id"])
    assert [h["id"] for h in hyps] == [hypothesis["id"]]
