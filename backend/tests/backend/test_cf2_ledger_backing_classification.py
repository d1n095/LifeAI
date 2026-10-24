"""CF-2: real AgentWorkAssignment rows must never become synthetic ledger backing."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.orm.attributes import flag_modified

from app.agent_coordination.delivery import issue_bridge_credential, resolve_bridge
from app.agent_coordination.durable import load_owner_ledger
from app.agent_coordination.lifecycle import ingest_lifecycle_event, reconcile_owner
from app.agent_coordination.persist import (
    LEDGER_BACKING_KEY,
    ensure_owner_ledger_backing,
    find_owner_assignments,
    persist_owner_ledger,
    persist_status,
)
from app.agent_coordination.service import create_work_assignment, register_agent
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.mainai_execution import MainAITask
from app.request_context import current_user_id as current_user_id_var
from app.worker import Worker


def _set_rls(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture(autouse=True, scope="module")
def _privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _owner(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls(db_session, user.id)
    return user.id


def _real_assignment(db, owner_id, *, key: str):
    goal = create_goal(
        db,
        owner_id=owner_id,
        title=f"Real work {key}",
        original_instruction="CF-2",
        created_by="founder",
        approval_policy="autonomous_development_work",
    )
    plan = create_plan(
        db,
        goal=goal,
        rationale="cf2",
        tasks=[PlannedTaskSpec(description="Build", task_type="read_only_audit", verification_plan=[])],
        created_by="founder",
    )
    db.flush()
    task = db.query(MainAITask).filter_by(plan_id=plan.id).one()
    agent = register_agent(
        db,
        agent_key=f"cursor-{key}-{uuid.uuid4().hex[:8]}",
        display_name="cursor",
        adapter_kind="cli",
    )
    return create_work_assignment(
        db,
        owner_id=owner_id,
        goal_id=goal.id,
        task_id=task.id,
        agent_id=agent.id,
        role="builder",
        read_write_mode="read_write",
        repository_identity="lifeai",
        allowed_paths=["docs/**"],
        requested_by="founder",
        provenance={"key": key},
    )


def _flags(row) -> dict[str, bool]:
    provenance = row.provenance or {}
    dump = provenance.get("durable_coordination") or {}
    return {
        "ledger_backing": bool(provenance.get(LEDGER_BACKING_KEY) or (isinstance(dump, dict) and dump.get(LEDGER_BACKING_KEY))),
        "created_without_manual_assignment": bool(provenance.get("created_without_manual_assignment")),
    }


def _assert_real(row) -> None:
    flags = _flags(row)
    assert flags["ledger_backing"] is False
    assert flags["created_without_manual_assignment"] is False


def _assert_synthetic(row) -> None:
    flags = _flags(row)
    assert flags["ledger_backing"] is True
    assert flags["created_without_manual_assignment"] is True


def test_cf2_warm_issue_bridge_does_not_retag_existing_assignment(db_session, make_verified_user):
    owner_id = _owner(db_session, make_verified_user)
    real = _real_assignment(db_session, owner_id, key="warm")
    db_session.commit()
    issued = issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder")
    db_session.commit()
    assert issued["outcome"] == "issued"
    db_session.refresh(real)
    _assert_real(real)
    rows = find_owner_assignments(db_session, owner_id=owner_id)
    assert len(rows) == 1
    assert persist_status(db_session, owner_id=owner_id)["created_without_manual_assignment"] is False


def test_cf1_cold_owner_still_creates_synthetic_backing(db_session, make_verified_user, client):
    owner_id = _owner(db_session, make_verified_user)
    assert find_owner_assignments(db_session, owner_id=owner_id) == []
    issued = issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder")
    db_session.commit()
    rows = find_owner_assignments(db_session, owner_id=owner_id)
    assert len(rows) == 1
    _assert_synthetic(rows[0])
    db_session.expire_all()
    _set_rls(db_session, owner_id)
    assert resolve_bridge(db_session, token=issued["token"]) is not None
    inbox = client.get("/api/mainai/agent-gateway/inbox", headers={"Authorization": f"Bearer {issued['token']}"})
    assert inbox.status_code == 200


def test_cf2_multiple_real_assignments_stay_unmarked(db_session, make_verified_user):
    owner_id = _owner(db_session, make_verified_user)
    first = _real_assignment(db_session, owner_id, key="a")
    second = _real_assignment(db_session, owner_id, key="b")
    db_session.commit()
    issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder")
    issue_bridge_credential(db_session, owner_id=owner_id, recipient="claude", actor="founder")
    db_session.commit()
    db_session.refresh(first)
    db_session.refresh(second)
    _assert_real(first)
    _assert_real(second)
    assert not any(_flags(row)["created_without_manual_assignment"] for row in find_owner_assignments(db_session, owner_id=owner_id))


def test_cf2_lifecycle_and_worker_restart_do_not_retag(db_session, make_verified_user):
    owner_id = _owner(db_session, make_verified_user)
    real = _real_assignment(db_session, owner_id, key="life")
    db_session.commit()
    ingest_lifecycle_event(
        db_session,
        owner_id=owner_id,
        actor="worker",
        payload={"kind": "assignment_created", "event_id": "life-1", "assignment_id": str(real.id)},
    )
    db_session.commit()
    db_session.refresh(real)
    _assert_real(real)
    db_session.expire_all()
    _set_rls(db_session, owner_id)
    Worker()._reconcile_durable_coordination(db_session)
    db_session.commit()
    db_session.refresh(real)
    _assert_real(real)
    reconcile_owner(db_session, owner_id=owner_id)
    db_session.commit()
    db_session.refresh(real)
    _assert_real(real)


def test_cf2_repairs_previously_misclassified_real_assignment(db_session, make_verified_user):
    owner_id = _owner(db_session, make_verified_user)
    real = _real_assignment(db_session, owner_id, key="corrupt")
    provenance = dict(real.provenance or {})
    provenance[LEDGER_BACKING_KEY] = True
    provenance["durable_coordination"] = {LEDGER_BACKING_KEY: True, "obligations": {}}
    real.provenance = provenance
    flag_modified(real, "provenance")
    db_session.commit()
    assert _flags(real)["ledger_backing"] is True
    ensure_owner_ledger_backing(db_session, owner_id=owner_id)
    db_session.commit()
    db_session.refresh(real)
    _assert_real(real)
    issued = issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder")
    db_session.commit()
    db_session.refresh(real)
    _assert_real(real)
    db_session.expire_all()
    _set_rls(db_session, owner_id)
    assert resolve_bridge(db_session, token=issued["token"]) is not None
    db_session.refresh(real)
    _assert_real(real)


def test_cf2_sequential_credential_issuance_and_persist_are_idempotent(db_session, make_verified_user):
    owner_id = _owner(db_session, make_verified_user)
    real = _real_assignment(db_session, owner_id, key="seq")
    db_session.commit()
    first = issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder")
    second = issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder")
    third = issue_bridge_credential(db_session, owner_id=owner_id, recipient="codex", actor="founder")
    db_session.commit()
    assert first["outcome"] == "issued"
    assert second["outcome"] == "issued"
    assert third["outcome"] == "issued"
    service = load_owner_ledger(db_session, owner_id=owner_id)
    persist_owner_ledger(db_session, owner_id=owner_id, service=service, actor="delivery")
    persist_owner_ledger(db_session, owner_id=owner_id, service=service, actor="lifecycle")
    db_session.commit()
    db_session.refresh(real)
    _assert_real(real)
    db_session.expire_all()
    _set_rls(db_session, owner_id)
    Worker()._reconcile_durable_coordination(db_session)
    db_session.commit()
    db_session.refresh(real)
    _assert_real(real)
    assert resolve_bridge(db_session, token=second["token"]) is not None
    assert resolve_bridge(db_session, token=third["token"]) is not None
