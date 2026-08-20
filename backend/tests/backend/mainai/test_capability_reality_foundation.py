"""Life Capability Reality / Self-Model foundation -- proves the deterministic boundary
between "code/a binary exists" and "Life may treat this as something she can actually do
right now," against a real Postgres database. See migration 0048's own module docstring and
docs/LIFE_CAPABILITY_REALITY.md for the full architecture."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.agent_coordination.adapter_config import SUPPORTED_LOCAL_CLI_PROVIDERS
from app.capability_reality import (
    get_capability_reality,
    list_capability_gaps,
    list_capability_records,
    record_capability_gap,
    record_capability_observation,
    sync_agent_adapter_capability,
)
from app.intelligence_governance.service import record_evidence, record_execution
from app.models.capability_reality import CapabilityObservationEvent, CapabilityRecord
from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask
from app.models.user import User


def _owner(db):
    user = User(email=f"cap-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def _task(db, owner_id):
    goal = MainAIGoal(owner_id=owner_id, title="problem", original_instruction="solve", created_by="test", completed_at=None)
    db.add(goal)
    db.flush()
    plan = MainAIPlan(owner_id=owner_id, goal_id=goal.id, version=1, rationale="test", created_by="test")
    db.add(plan)
    db.flush()
    task = MainAITask(
        owner_id=owner_id, goal_id=goal.id, plan_id=plan.id, task_type="repo_edit", description="t",
        status="pending", risk_level="low",
    )
    db.add(task)
    db.flush()
    return task


# ============================================================================ Requirement A:
# implemented+verified vs merely planned/configured stays distinguishable.

def test_planned_capability_never_reports_as_verified_available(superuser_db):
    owner = _owner(superuser_db)
    record = record_capability_gap(
        superuser_db, owner_id=owner.id, capability_key="document_ingestion.ocr", domain="document_ingestion",
        reason="no OCR provider configured yet", authority="deterministic_source",
    )
    superuser_db.commit()
    assert record.status == "planned"
    assert record.status_reason == "no OCR provider configured yet"

    fetched = get_capability_reality(superuser_db, owner_id=owner.id, capability_key="document_ingestion.ocr")
    assert fetched.status == "planned"
    assert fetched.status != "verified_available"


def test_verified_available_requires_an_explicit_status_and_real_evidence(superuser_db):
    owner = _owner(superuser_db)
    task = _task(superuser_db, owner.id)
    execution = record_execution(superuser_db, owner_id=owner.id, task_id=task.id, idempotency_key="cap-exec-1", provider="internal")
    evidence = record_evidence(
        superuser_db, owner_id=owner.id, execution_id=execution.id, evidence_kind="test_run_result",
        payload={"passed": True}, source_type="pytest", source_ref="tests/x.py::test_y",
        idempotency_key="cap-ev-1", deterministic=True,
    )
    superuser_db.commit()

    record = record_capability_observation(
        superuser_db, owner_id=owner.id, capability_key="test_execution.pytest_backend", domain="test_execution",
        status="verified_available", authority="deterministic_source", success=True, verification_evidence_id=evidence.id,
    )
    superuser_db.commit()
    assert record.status == "verified_available"
    assert record.last_verification_evidence_id == evidence.id
    assert record.last_verified_at is not None
    assert record.last_success_at is not None


def test_unknown_status_is_valid_and_is_the_default(superuser_db):
    owner = _owner(superuser_db)
    record = record_capability_observation(
        superuser_db, owner_id=owner.id, capability_key="deployment.blue_green", domain="deployment", status="unknown",
    )
    superuser_db.commit()
    assert record.status == "unknown"


def test_status_cannot_be_an_arbitrary_string(superuser_db):
    owner = _owner(superuser_db)
    with pytest.raises(DBAPIError):
        record_capability_observation(
            superuser_db, owner_id=owner.id, capability_key="x.y", domain="x", status="definitely_works_trust_me",
        )
    superuser_db.rollback()


# ============================================================================ Requirement B:
# recording a gap never pretends the capability exists.

def test_record_capability_gap_is_a_thin_wrapper_that_stays_planned(superuser_db):
    owner = _owner(superuser_db)
    record = record_capability_gap(
        superuser_db, owner_id=owner.id, capability_key="corpus_trial.mixed_source_scoring", domain="corpus_trial",
        reason="evaluation harness not built yet", dependencies=["corpus_trial_harness"],
    )
    superuser_db.commit()
    assert record.status == "planned"
    assert record.dependencies == ["corpus_trial_harness"]

    events = superuser_db.query(CapabilityObservationEvent).filter_by(capability_record_id=record.id).all()
    kinds = {e.event_type for e in events}
    assert "gap_recorded" in kinds


def test_list_capability_records_filters_by_domain_and_status(superuser_db):
    owner = _owner(superuser_db)
    record_capability_observation(superuser_db, owner_id=owner.id, capability_key="b.one", domain="b", status="verified_available")
    record_capability_observation(superuser_db, owner_id=owner.id, capability_key="b.two", domain="b", status="planned")
    record_capability_observation(superuser_db, owner_id=owner.id, capability_key="c.one", domain="c", status="verified_available")
    superuser_db.commit()

    all_b = list_capability_records(superuser_db, owner_id=owner.id, domain="b")
    assert {r.capability_key for r in all_b} == {"b.one", "b.two"}

    verified_only = list_capability_records(superuser_db, owner_id=owner.id, status="verified_available")
    assert {r.capability_key for r in verified_only} == {"b.one", "c.one"}


def test_list_capability_gaps_excludes_verified_available_and_includes_everything_else(superuser_db):
    owner = _owner(superuser_db)
    record_capability_observation(superuser_db, owner_id=owner.id, capability_key="a.verified", domain="a", status="verified_available")
    record_capability_observation(superuser_db, owner_id=owner.id, capability_key="a.disabled", domain="a", status="configured_disabled")
    record_capability_observation(superuser_db, owner_id=owner.id, capability_key="a.unavailable", domain="a", status="configured_unavailable")
    record_capability_gap(superuser_db, owner_id=owner.id, capability_key="a.planned", domain="a", reason="not built")
    record_capability_observation(superuser_db, owner_id=owner.id, capability_key="a.unknown", domain="a", status="unknown")
    superuser_db.commit()

    gaps = {r.capability_key for r in list_capability_gaps(superuser_db, owner_id=owner.id, domain="a")}
    assert gaps == {"a.disabled", "a.unavailable", "a.planned", "a.unknown"}
    assert "a.verified" not in gaps


# ============================================================================ Reuse, not
# duplication: the agent_coordination bridge.

def test_sync_agent_adapter_capability_never_reports_verified_available_even_when_locally_installed(superuser_db):
    """All three real providers may genuinely be installed on the machine running this test
    (see test_agent_real_execution_bridge.py's own test_no_real_provider_is_enabled_by_default)
    -- this proves the mission's own hard rule: an executable existing, even a genuinely
    enabled one, is still never verification on its own."""

    owner = _owner(superuser_db)
    for provider_key in SUPPORTED_LOCAL_CLI_PROVIDERS:
        record = sync_agent_adapter_capability(superuser_db, owner_id=owner.id, provider_key=provider_key)
        assert record.status != "verified_available"
        assert record.status in ("configured_disabled", "configured_unavailable", "unknown")
        assert record.capability_key == f"agent_dispatch.{provider_key}"
        assert record.domain == "agent_dispatch"
        assert record.provenance["provider_key"] == provider_key
    superuser_db.commit()


def test_sync_agent_adapter_capability_reflects_enabled_state(superuser_db, monkeypatch):
    owner = _owner(superuser_db)
    monkeypatch.delenv("LIFE_AGENT_ADAPTER_ENABLED__CODEX", raising=False)
    disabled = sync_agent_adapter_capability(superuser_db, owner_id=owner.id, provider_key="codex")
    assert disabled.status == "configured_disabled"

    monkeypatch.setenv("LIFE_AGENT_ADAPTER_ENABLED__CODEX", "true")
    enabled = sync_agent_adapter_capability(superuser_db, owner_id=owner.id, provider_key="codex")
    assert enabled.status == "configured_unavailable"  # NOT verified_available -- see module docstring
    assert enabled.id == disabled.id  # same live row, updated in place, not a new one
    superuser_db.commit()


# ============================================================================ Live row +
# append-only history, mirroring agent_coordination's own established split.

def test_repeated_observations_update_the_same_live_row_and_append_history(superuser_db):
    owner = _owner(superuser_db)
    first = record_capability_observation(
        superuser_db, owner_id=owner.id, capability_key="search.web", domain="search", status="planned", status_reason="not wired up",
    )
    superuser_db.commit()
    second = record_capability_observation(
        superuser_db, owner_id=owner.id, capability_key="search.web", domain="search", status="configured_disabled",
        status_reason="provider key not set",
    )
    superuser_db.commit()

    assert first.id == second.id  # same row, updated in place
    assert superuser_db.query(CapabilityRecord).filter_by(owner_id=owner.id, capability_key="search.web").count() == 1

    events = superuser_db.query(CapabilityObservationEvent).filter_by(capability_record_id=first.id).order_by(CapabilityObservationEvent.created_at).all()
    assert len(events) == 2
    assert events[0].detail["status"] == "planned"
    assert events[0].event_type == "status_changed"
    assert events[1].detail["status"] == "configured_disabled"
    assert events[1].event_type == "status_changed"


def test_reasserting_the_same_status_is_never_mislabeled_as_a_change_that_did_not_happen(superuser_db):
    """A real bug caught in review: re-recording the SAME status with no new verification/
    success info must never be labeled `status_changed` -- nothing changed. It gets its own
    honest label, `observation_reasserted`."""

    owner = _owner(superuser_db)
    first = record_capability_observation(superuser_db, owner_id=owner.id, capability_key="search.reassert", domain="search", status="planned")
    superuser_db.commit()
    second = record_capability_observation(superuser_db, owner_id=owner.id, capability_key="search.reassert", domain="search", status="planned")
    superuser_db.commit()
    assert first.id == second.id

    events = superuser_db.query(CapabilityObservationEvent).filter_by(capability_record_id=first.id).order_by(CapabilityObservationEvent.created_at).all()
    assert len(events) == 2
    assert events[0].event_type == "status_changed"  # the first-ever observation of this capability
    assert events[1].event_type == "observation_reasserted"  # same status, nothing new


def test_event_detail_carries_verification_and_success_facts_not_just_the_winning_label(superuser_db):
    """A real bug caught in review: the append-only event's own `detail` must carry every fact
    a call actually asserted (verification_evidence_id, success), never rely on the caller
    cross-referencing the live row's own overwritable last_verification_evidence_id/
    last_success_at to reconstruct what a specific past observation asserted."""

    owner = _owner(superuser_db)
    task = _task(superuser_db, owner.id)
    execution = record_execution(superuser_db, owner_id=owner.id, task_id=task.id, idempotency_key="cap-exec-detail", provider="internal")
    evidence = record_evidence(
        superuser_db, owner_id=owner.id, execution_id=execution.id, evidence_kind="test_run_result",
        payload={"passed": True}, source_type="pytest", source_ref="tests/x.py::test_z",
        idempotency_key="cap-ev-detail", deterministic=True,
    )
    superuser_db.commit()

    record = record_capability_observation(
        superuser_db, owner_id=owner.id, capability_key="test_execution.detail_check", domain="test_execution",
        status="verified_available", authority="deterministic_source", success=True, verification_evidence_id=evidence.id,
    )
    superuser_db.commit()

    event = superuser_db.query(CapabilityObservationEvent).filter_by(capability_record_id=record.id).order_by(CapabilityObservationEvent.created_at).first()
    assert event.detail["verification_evidence_id"] == str(evidence.id)
    assert event.detail["success"] is True


def test_capability_observation_events_are_append_only(superuser_db):
    owner = _owner(superuser_db)
    record = record_capability_observation(superuser_db, owner_id=owner.id, capability_key="a.b", domain="a", status="unknown")
    superuser_db.commit()
    event = superuser_db.query(CapabilityObservationEvent).filter_by(capability_record_id=record.id).first()

    with pytest.raises(DBAPIError):
        superuser_db.execute(text("UPDATE capability_observation_events SET detail = '{}'::jsonb WHERE id = :id"), {"id": event.id})
    superuser_db.rollback()

    record2 = record_capability_observation(superuser_db, owner_id=owner.id, capability_key="a.b", domain="a", status="unknown")
    superuser_db.commit()
    with pytest.raises(DBAPIError):
        superuser_db.execute(text("DELETE FROM capability_observation_events WHERE id = :id"), {"id": event.id})
    superuser_db.rollback()
    assert record2.id == record.id


# ============================================================================ Confidence /
# permissions / dependencies / limitations stay explicit, never fabricated.

def test_unknown_fields_stay_none_never_invented(superuser_db):
    owner = _owner(superuser_db)
    record = record_capability_observation(
        superuser_db, owner_id=owner.id, capability_key="a.c", domain="a", status="unknown",
    )
    superuser_db.commit()
    assert record.confidence is None
    assert record.required_permissions == []
    assert record.dependencies == []
    assert record.known_limitations == []
    assert record.last_verified_at is None
    assert record.last_success_at is None
    assert record.last_failure_at is None


def test_confidence_out_of_range_is_rejected(superuser_db):
    owner = _owner(superuser_db)
    with pytest.raises(DBAPIError):
        record_capability_observation(
            superuser_db, owner_id=owner.id, capability_key="a.d", domain="a", status="unknown", confidence=1.5,
        )
    superuser_db.rollback()


# ============================================================================ Authority stays
# the SAME vocabulary migration 0042 already established -- never a second taxonomy.

def test_authority_reuses_life_problem_vocabulary_and_rejects_foreign_values(superuser_db):
    owner = _owner(superuser_db)
    record = record_capability_observation(
        superuser_db, owner_id=owner.id, capability_key="a.e", domain="a", status="unknown", authority="founder",
    )
    superuser_db.commit()
    assert record.authority == "founder"

    with pytest.raises(DBAPIError):
        record_capability_observation(
            superuser_db, owner_id=owner.id, capability_key="a.f", domain="a", status="unknown", authority="admin_override",
        )
    superuser_db.rollback()
