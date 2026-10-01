"""Runtime sessions may read only their own integrity receipts and cannot mint authority."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from app.claim_action_integrity import record_evidence, record_receipt
from app.models.claim_action_integrity import ClaimActionEvidence, ClaimActionReceipt


def _set_owner(session, owner_id):
    session.execute(text("SET LOCAL app.current_user_id = :owner"), {"owner": str(owner_id)})


def _seed(admin, owner_id, suffix):
    evidence = record_evidence(
        admin, owner_id=owner_id, execution_id=f"exec-{suffix}", subject_key=f"subject-{suffix}",
        action_key="branch_push", source_type="github", source_ref=f"github:{suffix}",
        artifact_sha="1" * 40,
        payload={"pushed": True, "branch": suffix, "local_sha": "1" * 40, "remote_sha": "1" * 40},
        recorded_by="trusted-test",
    )
    receipt = record_receipt(
        admin, owner_id=owner_id, execution_id=f"exec-{suffix}", subject_key=f"subject-{suffix}",
        action_key="branch_push", declared_state="externally_observed", action_state="requested",
        declared_action={}, permitted_action={}, executed_action={}, observed_result={"pushed": True},
        authority_snapshot={}, created_by="trusted-test", artifact_sha="1" * 40, evidence_id=evidence.id,
    )
    admin.commit()
    return evidence, receipt


def test_owner_reads_only_own_evidence_and_receipts(db_session, superuser_db, make_verified_user):
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()
    _seed(superuser_db, owner_a.id, "a")
    _seed(superuser_db, owner_b.id, "b")

    _set_owner(db_session, owner_a.id)
    assert {row.owner_id for row in db_session.query(ClaimActionEvidence).all()} == {owner_a.id}
    assert {row.owner_id for row in db_session.query(ClaimActionReceipt).all()} == {owner_a.id}

    db_session.rollback()
    _set_owner(db_session, owner_b.id)
    assert {row.owner_id for row in db_session.query(ClaimActionEvidence).all()} == {owner_b.id}
    assert {row.owner_id for row in db_session.query(ClaimActionReceipt).all()} == {owner_b.id}


def test_no_owner_context_reads_nothing(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    _seed(superuser_db, owner.id, str(uuid.uuid4()))
    db_session.rollback()
    assert db_session.query(ClaimActionEvidence).all() == []
    assert db_session.query(ClaimActionReceipt).all() == []


def test_runtime_cannot_insert_or_mutate_integrity_authority(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    evidence, _ = _seed(superuser_db, owner.id, str(uuid.uuid4()))
    _set_owner(db_session, owner.id)
    db_session.add(ClaimActionEvidence(
        owner_id=owner.id, execution_id="forged", subject_key="forged", action_key="generic",
        source_type="database", source_ref="forged", artifact_sha=None, authoritative=True,
        payload={"success": True}, payload_digest="f" * 64,
        observed_at=datetime.now(timezone.utc), recorded_by="mainai",
    ))
    try:
        db_session.commit()
        assert False, "runtime insert must be rejected"
    except Exception:
        db_session.rollback()

    _set_owner(db_session, owner.id)
    visible = db_session.get(ClaimActionEvidence, evidence.id)
    visible.source_ref = "rewritten"
    try:
        db_session.commit()
        assert False, "append-only evidence update must be rejected"
    except Exception:
        db_session.rollback()
