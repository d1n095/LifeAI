"""Closes the three P1s from the Research, Truth & Advisory Intelligence round's own handoff
doc: cross-investigation auto-reopen, durable investigation graph invariants, and the
Resource Intelligence -> Provider Economics bridge. See
docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md."""

from __future__ import annotations

import uuid

import pytest

from app.mainai_cognitive_ops.provider_economics_bridge import derive_provider_economics_signal
from app.mainai_cognitive_ops.research_graph_provenance import build_containment_query_fragment, validate_graph_provenance_payload
from app.mainai_cognitive_ops.research_reopen_trigger import find_cross_investigation_reopen_candidates, trigger_cross_investigation_reopen
from app.mainai_research.research_ledger import get_investigation, record_investigation
from app.models.user import User


@pytest.fixture
def owner_id(superuser_db):
    user = User(email=f"cogops-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(user)
    superuser_db.flush()
    superuser_db.commit()
    return user.id


def test_unrelated_new_evidence_reopens_old_investigation(superuser_db, owner_id):
    record_investigation(superuser_db, owner_id=owner_id, question="Did vendor Acme misrepresent uptime guarantees in the Q3 contract?", idempotency_key=f"i-{uuid.uuid4()}")
    superuser_db.commit()

    reopened = trigger_cross_investigation_reopen(
        superuser_db, owner_id=owner_id,
        new_evidence_summary="internal memo proves vendor Acme did misrepresent uptime guarantees in the Q3 contract, discovered during an unrelated billing task",
    )
    superuser_db.commit()
    assert len(reopened) == 1


def test_weak_unrelated_evidence_does_not_reopen(superuser_db, owner_id):
    inv = record_investigation(superuser_db, owner_id=owner_id, question="Did vendor Acme misrepresent uptime guarantees in the Q3 contract?", idempotency_key=f"i-{uuid.uuid4()}")
    superuser_db.commit()

    candidates = find_cross_investigation_reopen_candidates(superuser_db, owner_id=owner_id, new_evidence_summary="the weather in a different city was rainy yesterday")
    superuser_db.commit()
    assert all(c.investigation_id != inv["id"] or not c.materiality_met for c in candidates)

    reopened = trigger_cross_investigation_reopen(superuser_db, owner_id=owner_id, new_evidence_summary="the weather in a different city was rainy yesterday")
    superuser_db.commit()
    assert reopened == ()


def test_closed_investigation_is_never_reopened_by_the_trigger(superuser_db, owner_id):
    from app.mainai_research.research_ledger import mark_investigation_saturated

    inv = record_investigation(superuser_db, owner_id=owner_id, question="Did contractor Beta overbill on the March invoice?", idempotency_key=f"i-{uuid.uuid4()}")
    mark_investigation_saturated(superuser_db, owner_id=owner_id, investigation_id=inv["id"], reason="no further leads")
    superuser_db.commit()
    # directly close it (bypassing saturation) to prove CLOSED is excluded from scanning
    from sqlalchemy import text
    superuser_db.execute(text("UPDATE mainai_research_investigations SET status='closed' WHERE id=:id"), {"id": inv["id"]})
    superuser_db.commit()

    reopened = trigger_cross_investigation_reopen(superuser_db, owner_id=owner_id, new_evidence_summary="contractor Beta overbill invoice March discovery")
    superuser_db.commit()
    assert reopened == ()
    current = get_investigation(superuser_db, owner_id=owner_id, investigation_id=inv["id"])
    assert current["status"] == "closed"


def test_graph_provenance_validation_accepts_well_formed_payload():
    payload = {"graph": {"actors": [{"actor_id": "a1", "name": "Acme Corp", "kind": "organization"}], "relationships": [{"from_actor_id": "a1", "to_actor_id": "a2", "kind": "co_authored"}]}}
    assert validate_graph_provenance_payload(payload) == ()


def test_graph_provenance_rejects_control_language_relationship():
    payload = {"graph": {"relationships": [{"from_actor_id": "a1", "to_actor_id": "a2", "kind": "controls"}]}}
    errors = validate_graph_provenance_payload(payload)
    assert any("RELATIONSHIP != CONTROL" in e for e in errors)


def test_graph_provenance_flags_missing_required_keys():
    payload = {"graph": {"actors": [{"name": "no id"}]}}
    errors = validate_graph_provenance_payload(payload)
    assert any("actor_id" in e for e in errors)


def test_containment_query_fragment_is_gin_compatible_shape():
    fragment = build_containment_query_fragment(actor_id="a1")
    assert fragment == {"graph": {"actors": [{"actor_id": "a1"}]}}


def test_resource_intelligence_derives_provider_economics_signal_with_no_history(superuser_db, owner_id):
    signal = derive_provider_economics_signal(superuser_db, owner_id=owner_id, provider_id="anthropic", agent_id=uuid.uuid4())
    assert signal.observation_count == 0
    assert signal.accepted_commit_rate is None  # UNKNOWN != ZERO
    assert signal.rework_rate is None


def test_provider_economics_signal_quota_missing_is_none_never_treated_as_unlimited(superuser_db, owner_id):
    signal = derive_provider_economics_signal(superuser_db, owner_id=owner_id, provider_id="anthropic", agent_id=uuid.uuid4(), goal_id=uuid.uuid4())
    assert signal.quota_remaining_fraction is None  # MISSING != FREE
