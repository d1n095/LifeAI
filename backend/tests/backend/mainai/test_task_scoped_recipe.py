"""F2 -- the deterministic recipe is TASK-scoped, cumulative, and never rewrites earlier work.

Measured 2026-11-27: a plan with two `repo_edit` tasks ("implement subtract" then "add its
focused test") gave BOTH tasks the same goal-scoped recipe, so the second task repeated the
first one's two files. It only looked harmless because the first task had already committed
both files; a different plan (or a source-only first task) would have lost work.

The fix keeps the same bounded registry and the same authority model -- the task's own
structured description (`requested_outcome`) only selects WHICH of the recipe's already-bounded
steps to run:

  * implementation task  -> calculator.py only, verified with `ruff_check`;
  * focused-test task    -> test_calculator.py only, APPENDED (existing tests preserved),
                             verified with `focused_pytest`;
  * both / neither       -> the previous full change, unchanged;
  * already present      -> a verified no-op (no empty commit, no duplicate function).
"""

import hashlib
from pathlib import Path
from types import SimpleNamespace

from app.safe_planner.service import (
    build_multiplication_repair_candidate,
    deterministic_candidate,
)

GOAL_INSTRUCTION = (
    "Lägg till en subtract-funktion i calculator.py och ett focused test som verifierar den. "
    "Endast calculator.py och test_calculator.py får ändras."
)


COMMIT_CAPABILITIES = ("stage_scoped_changes", "commit_scoped_changes")


def _context(
    root: Path,
    *,
    allowed=("calculator.py", "test_calculator.py"),
    capabilities=COMMIT_CAPABILITIES,
) -> SimpleNamespace:
    return SimpleNamespace(
        repository_root=root, allowed_paths=tuple(allowed), allowed_capabilities=tuple(capabilities)
    )


def _candidate(
    root: Path,
    *,
    task_description: str | None,
    allowed=("calculator.py", "test_calculator.py"),
    capabilities=COMMIT_CAPABILITIES,
):
    return deterministic_candidate(
        GOAL_INSTRUCTION,
        _context(root, allowed=allowed, capabilities=capabilities),
        requested_outcome=task_description,
    )


def _ids(candidate) -> list[str]:
    return [step.step_id for step in candidate.steps]


def test_an_implementation_task_changes_only_the_source(tmp_path):
    candidate = _candidate(tmp_path, task_description="Lägg till en subtract-funktion i calculator.py")
    assert _ids(candidate) == ["source", "verify", "gate", "stage", "commit"]
    source = next(step for step in candidate.steps if step.step_id == "source")
    assert source.capability == "create_file"
    assert source.arguments["path"] == "calculator.py"
    assert "def subtract" in source.arguments["content"]
    verify = next(step for step in candidate.steps if step.step_id == "verify")
    assert verify.capability == "run_static_check"
    assert verify.arguments["arguments"] == ["calculator.py"]
    stage = next(step for step in candidate.steps if step.step_id == "stage")
    assert stage.arguments["paths"] == ["calculator.py"]


def test_a_focused_test_task_appends_only_its_test_and_preserves_existing_work(tmp_path):
    (tmp_path / "calculator.py").write_text(
        "def subtract(left, right):\n    return left - right\n", encoding="utf-8"
    )
    (tmp_path / "test_calculator.py").write_text(
        "def test_existing():\n    assert True\n", encoding="utf-8"
    )
    candidate = _candidate(
        tmp_path, task_description="Lägg till ett focused test för subtract-funktionen i test_calculator.py"
    )
    assert _ids(candidate) == ["test", "verify", "gate", "stage", "commit"]
    test_step = next(step for step in candidate.steps if step.step_id == "test")
    assert test_step.capability == "patch_file"
    assert test_step.arguments["path"] == "test_calculator.py"
    content = test_step.arguments["content"]
    assert "def test_existing" in content, "an earlier test must never be dropped"
    assert "def test_subtract" in content
    assert test_step.arguments["expected_sha256"] == hashlib.sha256(
        (tmp_path / "test_calculator.py").read_bytes()
    ).hexdigest()
    verify = next(step for step in candidate.steps if step.step_id == "verify")
    assert verify.capability == "run_focused_test"
    stage = next(step for step in candidate.steps if step.step_id == "stage")
    assert stage.arguments["paths"] == ["test_calculator.py"]


def test_the_two_tasks_together_produce_distinct_cumulative_changes(tmp_path):
    implementation = _candidate(tmp_path, task_description="Lägg till en subtract-funktion i calculator.py")
    source = next(step for step in implementation.steps if step.step_id == "source")
    (tmp_path / "calculator.py").write_text(source.arguments["content"], encoding="utf-8")
    committed = (tmp_path / "calculator.py").read_text(encoding="utf-8")

    focused_test = _candidate(
        tmp_path, task_description="Lägg till ett focused test för subtract-funktionen i test_calculator.py"
    )
    test_step = next(step for step in focused_test.steps if step.step_id == "test")
    (tmp_path / "test_calculator.py").write_text(test_step.arguments["content"], encoding="utf-8")

    assert (tmp_path / "calculator.py").read_text(encoding="utf-8") == committed, (
        "the second task must not touch the first task's committed source"
    )
    assert "def test_subtract" in (tmp_path / "test_calculator.py").read_text(encoding="utf-8")
    assert "def subtract" in committed


def test_a_description_naming_both_halves_keeps_the_full_change(tmp_path):
    candidate = _candidate(
        tmp_path,
        task_description="Implementera subtract i calculator.py och lägg till focused test i test_calculator.py",
    )
    assert _ids(candidate) == ["source", "test", "verify", "gate", "stage", "commit"]
    verify = next(step for step in candidate.steps if step.step_id == "verify")
    assert verify.capability == "run_focused_test"
    stage = next(step for step in candidate.steps if step.step_id == "stage")
    assert stage.arguments["paths"] == ["calculator.py", "test_calculator.py"]


def test_no_task_description_keeps_the_previous_goal_scoped_behaviour(tmp_path):
    candidate = _candidate(tmp_path, task_description=None)
    assert _ids(candidate) == ["source", "test", "verify", "gate", "stage", "commit"]


def test_an_already_delivered_task_is_a_verified_no_op(tmp_path):
    (tmp_path / "calculator.py").write_text(
        "def subtract(left, right):\n    return left - right\n", encoding="utf-8"
    )
    (tmp_path / "test_calculator.py").write_text(
        "from calculator import subtract\n\ndef test_subtract():\n    assert subtract(7, 2) == 5\n",
        encoding="utf-8",
    )
    for description in (
        "Lägg till en subtract-funktion i calculator.py",
        "Lägg till ett focused test för subtract-funktionen i test_calculator.py",
    ):
        candidate = _candidate(tmp_path, task_description=description)
        assert _ids(candidate) == ["verify", "gate"], description
        assert all(step.capability not in {"create_file", "patch_file"} for step in candidate.steps)


def test_a_missing_test_file_is_created_not_patched(tmp_path):
    candidate = _candidate(
        tmp_path, task_description="Lägg till ett focused test för subtract-funktionen i test_calculator.py"
    )
    test_step = next(step for step in candidate.steps if step.step_id == "test")
    assert test_step.capability == "create_file"
    assert test_step.arguments["expected_sha256"] is None


def test_the_gap_repair_recipe_stays_a_full_change_and_still_refuses_a_narrow_envelope(tmp_path):
    # A repair child exists to fix a real failure, so it is deliberately NOT split by the task
    # description: "only the test" could never repair a missing helper.
    (tmp_path / "calculator.py").write_text(
        "def add(left, right):\n    return left + right\n", encoding="utf-8"
    )
    candidate = build_multiplication_repair_candidate(_context(tmp_path))
    assert _ids(candidate) == ["source", "test", "verify", "gate", "stage", "commit"]
    assert "def multiply" in candidate.steps[0].arguments["content"]
    assert "def add" in candidate.steps[0].arguments["content"], "existing helpers are preserved"
    stage = next(step for step in candidate.steps if step.step_id == "stage")
    assert stage.arguments["paths"] == ["calculator.py", "test_calculator.py"]

    try:
        build_multiplication_repair_candidate(_context(tmp_path, allowed=("calculator.py",)))
    except Exception as exc:  # CandidateValidationError
        assert "refusing to broaden binding scope" in str(exc)
    else:  # pragma: no cover - the assertion below is the real check
        raise AssertionError("a narrow envelope must be refused, not silently accepted")


def test_an_envelope_without_commit_capability_gets_the_previous_uncommitted_shape(tmp_path):
    # Committing is what preserves this task's work across the next task's worktree reset, but
    # it is authority the founder grants. Without it the recipe must NOT invent a commit step
    # (which would fail closed as OUT_OF_SCOPE) -- it keeps the original shape instead.
    candidate = _candidate(
        tmp_path,
        task_description="Lägg till en subtract-funktion i calculator.py",
        capabilities=("create_file", "patch_file", "run_focused_test"),
    )
    assert _ids(candidate) == ["source", "verify", "gate"]
