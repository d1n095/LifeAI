"""The recommended first Founder-supervised cycle scope is exactly one new test file:
`backend/tests/backend/test_first_cycle_probe.py` (no production module). Every repository-side
validator on the Path A path must accept it, and it must be the ONLY structured edit target.
This does not create the probe task or the file; it proves the scope is admissible.
"""

from pathlib import Path

import pytest
from sqlalchemy import text as sa_text

from app.mainai_execution import planner
from app.mainai_execution.envelope_scope import (
    map_verification_target_to_repo_path,
    path_in_authorized_scope,
    validate_target_path,
)
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.verify import validate_targeted_tests_target
from app.models.mainai_execution import MainAITask
from app.models.mainai_recovery import is_protected_branch
from app.request_context import current_user_id as current_user_id_var

PROBE_FILE = "backend/tests/backend/test_first_cycle_probe.py"
PROBE_TEST_TARGET = "tests/backend/test_first_cycle_probe.py"
REPO_ROOT = Path(__file__).resolve().parents[3]


def test_probe_file_does_not_exist_yet():
    # The first cycle creates it; its absence keeps it a NEW focused test (editable under TA-8b).
    assert not (REPO_ROOT / PROBE_FILE).exists()


def test_every_path_validator_accepts_the_single_probe_file():
    assert validate_target_path(PROBE_FILE) == PROBE_FILE
    assert validate_targeted_tests_target(PROBE_TEST_TARGET)
    assert map_verification_target_to_repo_path(PROBE_TEST_TARGET) == PROBE_FILE
    assert path_in_authorized_scope(PROBE_FILE, [PROBE_FILE])
    assert not is_protected_branch(PROBE_FILE)


@pytest.mark.parametrize("other", ["backend/app/first_cycle_probe.py", "backend/tests/backend/test_other.py", ".github/workflows/ci.yml"])
def test_exact_one_file_envelope_admits_nothing_else(other):
    assert not path_in_authorized_scope(other, [PROBE_FILE])


@pytest.fixture(autouse=True, scope="module")
def _privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


def test_planner_persists_exactly_the_probe_file_as_structured_authority(db_session, make_verified_user):
    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    goal = planner.create_goal(db_session, owner_id=user.id, title="first cycle", original_instruction="probe", created_by="founder")
    planner.create_plan(db_session, goal=goal, rationale="first cycle", created_by="founder", tasks=[
        PlannedTaskSpec(description="Add the first-cycle focused test.", task_type="repo_edit",
                        target_files=[PROBE_FILE],
                        verification_plan=[{"kind": "targeted_tests", "target": PROBE_TEST_TARGET}]),
        PlannedTaskSpec(description="Open a draft PR.", task_type="open_pr", depends_on=[0], verification_plan=[]),
    ])
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id, MainAITask.task_type == "repo_edit").one()
    detail = db_session.execute(
        sa_text("SELECT detail FROM mainai_task_events WHERE task_id=:t AND event_type='created'"), {"t": str(task.id)}
    ).scalar_one()
    assert detail["target_files"] == [PROBE_FILE]
    assert detail.get("verify_only_tests", []) == []
