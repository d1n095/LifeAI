"""`app.mainai_workforce.workforce_scheduler` + `situational_snapshot` -- REAL composition
against the actual `app.agent_coordination` registry (a real `CoordinationAgent` row, not a
hand-built stub), proving `SCHEDULER OUTPUT != EXECUTION AUTHORITY` end to end. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md."""

from __future__ import annotations

import uuid

import pytest

from app.mainai_cognitive_ops.types import ProgramStatus, WorkItem
from app.mainai_workforce.situational_snapshot import real_agent_states_snapshot
from app.mainai_workforce.types import WaitOrAssignDecision
from app.mainai_workforce.workforce_scheduler import recommend_for_task
from app.models.agent_coordination import AgentAdapterKind, CoordinationAgent
from app.models.user import User


@pytest.fixture
def owner_id(superuser_db):
    user = User(email=f"wfs-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(user)
    superuser_db.flush()
    superuser_db.commit()
    return user.id


@pytest.fixture
def real_idle_agent(superuser_db):
    agent = CoordinationAgent(agent_key=f"claude-{uuid.uuid4()}", display_name="Claude", adapter_kind=AgentAdapterKind.api)
    superuser_db.add(agent)
    superuser_db.flush()
    superuser_db.commit()
    return agent


def test_real_agent_states_snapshot_includes_the_real_idle_agent(superuser_db, owner_id, real_idle_agent):
    states = real_agent_states_snapshot(superuser_db, owner_id=owner_id)
    matching = [s for s in states if s.agent_id == real_idle_agent.agent_key]
    assert len(matching) == 1
    assert matching[0].idle is True
    assert matching[0].busy is False


def test_recommend_for_task_output_is_never_authorized(superuser_db, owner_id, real_idle_agent):
    candidate = WorkItem(item_id="new-task", program="P", subtask="fix flaky test", owner_agent=None, status=ProgramStatus.ACTIVE, keywords=("flaky", "test"))
    recommendation = recommend_for_task(
        superuser_db, owner_id=owner_id, candidate_work_item=candidate, active_work=(),
        best_agent_available=False, best_agent_eta_seconds=3 * 3600, best_agent_competency=0.9,
        candidate_agent_available=True, candidate_agent_competency=0.6, critical_path=True,
    )
    assert recommendation.authorized is False
    assert recommendation.decision == WaitOrAssignDecision.WORK_NOW
    assert real_idle_agent.agent_key in recommendation.assignable_agent_ids


def test_recommend_for_task_blocks_on_accidental_duplication(superuser_db, owner_id, real_idle_agent):
    existing = (WorkItem(item_id="existing-1", program="P", subtask="fix flaky test in auth", owner_agent="codex", status=ProgramStatus.ACTIVE, keywords=("flaky", "test", "auth")),)
    candidate = WorkItem(item_id="new-task", program="P", subtask="fix flaky auth test", owner_agent=None, status=ProgramStatus.ACTIVE, keywords=("flaky", "test", "auth"))
    recommendation = recommend_for_task(
        superuser_db, owner_id=owner_id, candidate_work_item=candidate, active_work=existing,
        best_agent_available=True, best_agent_eta_seconds=None, best_agent_competency=0.9,
        candidate_agent_available=True, candidate_agent_competency=0.6,
    )
    assert recommendation.decision == WaitOrAssignDecision.DEFER
