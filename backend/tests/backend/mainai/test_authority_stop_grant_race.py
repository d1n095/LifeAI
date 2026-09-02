"""Authority stop/grant serialization — AFTER STOP no reusable live authority."""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.orm import sessionmaker

from app.models.user import User
from app.workforce import (
    KillSwitchError,
    activate_global_emergency_stop,
    activate_owner_stop,
    prove_no_reusable_live_authority,
    register_workforce_agent,
    reset_kill_switch_for_tests,
    resolve_delegation,
    submit_delegation_request,
)
from app.workforce.authority import assignment_authority_is_live as live_check
from app.workforce.kill_switch import query_stop_status, record_boot_blocked


@pytest.fixture(autouse=True, scope="module")
def _priv():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture(autouse=True)
def _reset(superuser_db):
    reset_kill_switch_for_tests(superuser_db)
    superuser_db.flush()


def _owner(db, tag="r"):
    u = User(email=f"race-{tag}-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", email_verified=True)
    db.add(u)
    db.flush()
    return u


def _seed_open_request(db, owner_id, key_suffix: str):
    b = register_workforce_agent(
        db,
        owner_id=owner_id,
        agent_key=f"b-{key_suffix}",
        name="B",
        role="builder",
        agent_type="CODING",
        capability_tags=["low_risk_classification"],
        status="active",
        trust_zone="LOCAL_INTERNAL",
        allowed_tool_classes=["read_excerpt"],
    )
    v = register_workforce_agent(
        db,
        owner_id=owner_id,
        agent_key=f"v-{key_suffix}",
        name="V",
        role="verifier",
        agent_type="VERIFIER",
        capability_tags=["verification"],
        status="active",
        trust_zone="LOCAL_INTERNAL",
        allowed_tool_classes=["read_excerpt"],
    )
    req = submit_delegation_request(
        db,
        owner_id=owner_id,
        goal_text="race-proof",
        required_capability="low_risk_classification",
        risk="low",
        data_sensitivity="low",
        verification_requirement="independent_verifier",
    )
    db.flush()
    return b, v, req


def test_grant_while_owner_stopped_is_rejected(superuser_db):
    owner = _owner(superuser_db, "post")
    _, v, req = _seed_open_request(superuser_db, owner.id, uuid.uuid4().hex[:6])
    activate_owner_stop(superuser_db, owner_id=owner.id, reason="already_stopped")
    with pytest.raises(KillSwitchError):
        resolve_delegation(
            superuser_db, owner_id=owner.id, request=req, verifier_profile_id=v.id
        )
    assert prove_no_reusable_live_authority(superuser_db, owner_id=owner.id) is True


def test_owner_stop_then_prove_no_live_authority(superuser_db):
    owner = _owner(superuser_db, "pre")
    _, v, req = _seed_open_request(superuser_db, owner.id, uuid.uuid4().hex[:6])
    asg = resolve_delegation(
        superuser_db, owner_id=owner.id, request=req, verifier_profile_id=v.id
    )
    assert live_check(asg).live is True
    activate_owner_stop(superuser_db, owner_id=owner.id, reason="revoke_existing")
    superuser_db.refresh(asg)
    assert live_check(asg).live is False
    assert prove_no_reusable_live_authority(superuser_db, owner_id=owner.id) is True


def test_two_connection_grant_vs_owner_stop_no_surviving_authority(superuser_db):
    """Real two-connection race: grant concurrent with owner stop must not leave live auth."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    from app.config import get_settings
    from app.models.workforce import WorkforceAssignment, WorkforceDelegationRequest

    owner = _owner(superuser_db, "2c")
    _, v, req = _seed_open_request(superuser_db, owner.id, uuid.uuid4().hex[:6])
    owner_id = owner.id
    req_id = req.id
    verifier_id = v.id
    superuser_db.commit()

    url = get_settings().database_url
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    granted_ids: list[uuid.UUID] = []

    def granter():
        eng = create_engine(url, poolclass=NullPool, pool_pre_ping=True)
        Session = sessionmaker(bind=eng)
        db = Session()
        try:
            r = db.get(WorkforceDelegationRequest, req_id)
            barrier.wait(timeout=10)
            asg = resolve_delegation(
                db, owner_id=owner_id, request=r, verifier_profile_id=verifier_id
            )
            db.commit()
            granted_ids.append(asg.id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            db.rollback()
        finally:
            db.close()
            eng.dispose()

    def stopper():
        eng = create_engine(url, poolclass=NullPool, pool_pre_ping=True)
        Session = sessionmaker(bind=eng)
        db = Session()
        try:
            barrier.wait(timeout=10)
            activate_owner_stop(db, owner_id=owner_id, reason="race_stop")
            db.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            db.rollback()
        finally:
            db.close()
            eng.dispose()

    t1 = threading.Thread(target=granter)
    t2 = threading.Thread(target=stopper)
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    unexpected = [e for e in errors if not isinstance(e, KillSwitchError)]
    assert not unexpected, f"unexpected errors: {unexpected!r}; all={errors!r}"

    eng = create_engine(url, poolclass=NullPool, pool_pre_ping=True)
    db = sessionmaker(bind=eng)()
    try:
        status = query_stop_status(db, owner_id=owner_id)
        assert status["blocked"] is True, f"status={status} errors={errors!r} granted={granted_ids}"
        assert status["owner"]["active"] is True
        assert prove_no_reusable_live_authority(db, owner_id=owner_id) is True
        for aid in granted_ids:
            asg = db.get(WorkforceAssignment, aid)
            if asg is not None:
                assert live_check(asg).live is False
    finally:
        db.close()
        eng.dispose()


def test_global_stop_revokes_live_authority(superuser_db):
    a = _owner(superuser_db, "ga")
    _, v, req = _seed_open_request(superuser_db, a.id, uuid.uuid4().hex[:6])
    asg = resolve_delegation(superuser_db, owner_id=a.id, request=req, verifier_profile_id=v.id)
    assert live_check(asg).live is True
    activate_global_emergency_stop(
        superuser_db,
        reason="global_race",
        founder_authority_ref="founder_ack:global-emergency-test",
    )
    superuser_db.refresh(asg)
    assert live_check(asg).live is False
    assert prove_no_reusable_live_authority(superuser_db, owner_id=a.id) is True


def test_record_boot_blocked_uses_single_blocking_identity(superuser_db):
    a = _owner(superuser_db, "aud")
    activate_global_emergency_stop(
        superuser_db,
        reason="global_for_audit",
        founder_authority_ref="founder_ack:audit-identity-check",
    )
    status_before = query_stop_status(superuser_db, owner_id=a.id)
    gseq = status_before["global"]["sequence"]
    record_boot_blocked(superuser_db, owner_id=a.id, reason="boot_saw_stop")
    superuser_db.flush()
    status_after = query_stop_status(superuser_db, owner_id=a.id)
    # Single blocking identity: global stop, not a mixed owner+global row.
    assert status_after["blocked"] is True
    assert status_after["code"] == "BLOCKED_BY_GLOBAL_EMERGENCY_STOP"
    assert status_after["global"]["active"] is True
    assert int(status_after["global"]["sequence"]) == int(gseq)
    assert status_after["canonical_table"] == "workforce_authority_epoch"