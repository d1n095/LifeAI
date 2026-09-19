"""`app.mainai_cognitive_ops.founder_communication_ledger` + `founder_anti_repetition`. See
docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import InternalError

from app.mainai_cognitive_ops.founder_anti_repetition import assess_communication_necessity
from app.mainai_cognitive_ops.founder_communication_ledger import (
    latest_communication_for_topic,
    list_communications_for_topic,
    record_communication,
)
from app.mainai_cognitive_ops.types import CommunicationDeltaVerdict
from app.models.user import User


@pytest.fixture
def owner_id(superuser_db):
    user = User(email=f"ops-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(user)
    superuser_db.flush()
    superuser_db.commit()
    return user.id


def test_record_communication_is_idempotent(superuser_db, owner_id):
    key = f"comm-{uuid.uuid4()}"
    first = record_communication(superuser_db, owner_id=owner_id, topic="research-program", status_communicated="started", idempotency_key=key)
    second = record_communication(superuser_db, owner_id=owner_id, topic="research-program", status_communicated="started", idempotency_key=key)
    superuser_db.commit()
    assert first["id"] == second["id"]


def test_communication_ledger_is_append_only(superuser_db, owner_id):
    row = record_communication(superuser_db, owner_id=owner_id, topic="t", status_communicated="s", idempotency_key=f"c-{uuid.uuid4()}")
    superuser_db.commit()
    with pytest.raises(InternalError):
        superuser_db.execute(
            text("UPDATE mainai_ops_founder_communications SET status_communicated='hacked' WHERE id=:id"),
            {"id": row["id"]},
        )
        superuser_db.commit()
    superuser_db.rollback()


def test_no_previous_communication_always_reports_first():
    verdict = assess_communication_necessity(previous=None, candidate_status="started")
    assert verdict.verdict == CommunicationDeltaVerdict.REPORT_MATERIAL_CHANGE


def test_already_reported_identical_status_is_suppressed(superuser_db, owner_id):
    record_communication(superuser_db, owner_id=owner_id, topic="quota", status_communicated="quota at 40%", idempotency_key=f"q-{uuid.uuid4()}", material_facts={"pct": 40})
    superuser_db.commit()
    previous = latest_communication_for_topic(superuser_db, owner_id=owner_id, topic="quota")
    verdict = assess_communication_necessity(previous=previous, candidate_status="quota at 40%", candidate_material_facts={"pct": 40})
    assert verdict.verdict == CommunicationDeltaVerdict.SUPPRESS_ALREADY_REPORTED
    assert verdict.should_notify is False


def test_material_status_change_is_reported(superuser_db, owner_id):
    record_communication(superuser_db, owner_id=owner_id, topic="quota", status_communicated="quota at 40%", idempotency_key=f"q-{uuid.uuid4()}", material_facts={"pct": 40})
    superuser_db.commit()
    previous = latest_communication_for_topic(superuser_db, owner_id=owner_id, topic="quota")
    verdict = assess_communication_necessity(previous=previous, candidate_status="quota at 9%", candidate_material_facts={"pct": 9})
    assert verdict.should_notify is True


def test_same_recommendation_no_new_evidence_produces_no_spam():
    previous = {"status_communicated": "recommend KEEP provider X", "material_facts": {"observations": 12}}
    verdict = assess_communication_necessity(previous=previous, candidate_status="recommend KEEP provider X", candidate_material_facts={"observations": 12})
    assert verdict.should_notify is False


def test_decision_required_always_reports_even_if_status_text_unchanged():
    previous = {"status_communicated": "s", "material_facts": {}}
    verdict = assess_communication_necessity(previous=previous, candidate_status="s", candidate_material_facts={}, decision_now_required=True)
    assert verdict.verdict == CommunicationDeltaVerdict.REPORT_DECISION_REQUIRED


def test_supersession_points_forward_from_new_row_never_edits_old(superuser_db, owner_id):
    old = record_communication(superuser_db, owner_id=owner_id, topic="t2", status_communicated="v1", idempotency_key=f"t2a-{uuid.uuid4()}")
    superuser_db.commit()
    new = record_communication(superuser_db, owner_id=owner_id, topic="t2", status_communicated="v2", idempotency_key=f"t2b-{uuid.uuid4()}", supersedes_id=old["id"])
    superuser_db.commit()
    assert new["supersedes_id"] == old["id"]
    history = list_communications_for_topic(superuser_db, owner_id=owner_id, topic="t2")
    assert [h["status_communicated"] for h in history] == ["v1", "v2"]
