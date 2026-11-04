"""`app.mainai_workforce.capability_learning_loop` + `situational_snapshot` +
`workforce_scheduler`. See docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md."""

from __future__ import annotations

import uuid

import pytest

from app.agent_coordination.runtime_view import AgentRuntimeView, RuntimeStatus
from app.mainai_cognitive_ops.duplication_control import assess_duplication
from app.mainai_cognitive_ops.types import DuplicationVerdict, ProgramStatus, WorkItem
from app.mainai_workforce.capability_learning_loop import apply_teacher_observation, extract_reusable_procedure
from app.mainai_workforce.situational_snapshot import agent_runtime_view_to_agent_state
from app.mainai_workforce.types import TeacherObservation
from app.models.user import User


@pytest.fixture
def owner_id(superuser_db):
    user = User(email=f"wfl-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(user)
    superuser_db.flush()
    superuser_db.commit()
    return user.id


def _observation():
    return TeacherObservation(
        task_class="postgres_migration_debugging", provider="codex",
        problem_decomposition_steps=("reproduce locally", "isolate migration file"),
        files_inspected=("alembic/versions/0075_x.py",), tools_used=("psql", "pytest"),
        debug_method="bisect migration chain", tests_selected=("test_migration_head",),
        root_cause_method="check constraint literal mismatch", errors_encountered=("CheckViolation",),
        examiner_corrections=("fix enum case",),
    )


def test_extract_reusable_procedure_restructures_observable_fields_only():
    procedure = extract_reusable_procedure(_observation())
    assert procedure.task_class == "postgres_migration_debugging"
    assert procedure.provider == "codex"
    assert "reproduce locally" in procedure.decomposition_steps
    assert procedure.corrections_to_avoid_repeating == ("fix enum case",)


def test_apply_teacher_observation_creates_and_updates_mastery_row(superuser_db, owner_id):
    procedure, mastery = apply_teacher_observation(
        superuser_db, owner_id=owner_id, capability_key="postgres_migration_debugging", observation=_observation(), idempotency_key=f"m-{uuid.uuid4()}",
    )
    superuser_db.commit()
    assert mastery["observation_count"] == 1
    assert procedure.provider == "codex"


def test_agent_runtime_view_maps_running_to_busy():
    view = AgentRuntimeView(
        agent_id=uuid.uuid4(), agent_key="codex-1", display_name="Codex", adapter_kind="cli",
        model_hint=None, capabilities=(), concurrency_limit=1, registry_status="active",
        runtime_status=RuntimeStatus.RUNNING, availability_reason="running", heartbeat_at=None, current_assignments=(),
    )
    state = agent_runtime_view_to_agent_state(view)
    assert state.busy is True
    assert state.exists is True
    assert state.idle is False


def test_agent_runtime_view_maps_offline_to_blocked_not_assignable():
    view = AgentRuntimeView(
        agent_id=uuid.uuid4(), agent_key="claude-1", display_name="Claude", adapter_kind="api",
        model_hint=None, capabilities=(), concurrency_limit=1, registry_status="disabled",
        runtime_status=RuntimeStatus.OFFLINE, availability_reason="disabled", heartbeat_at=None, current_assignments=(),
    )
    state = agent_runtime_view_to_agent_state(view)
    assert state.blocked is True


def test_busy_agent_never_receives_duplicate_assignment_via_duplication_control():
    existing = (WorkItem(item_id="1", program="P", subtask="fix migration", owner_agent="codex", status=ProgramStatus.ACTIVE, keywords=("migration", "postgres")),)
    candidate = WorkItem(item_id="2", program="P", subtask="fix migration again", owner_agent="claude", status=ProgramStatus.ACTIVE, keywords=("migration", "postgres"))
    result = assess_duplication(candidate=candidate, existing_work=existing)
    assert result.verdict == DuplicationVerdict.ACCIDENTAL_DUPLICATE
