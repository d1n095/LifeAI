"""VERIFICATION — a separate, gated step (the founder's explicit V0.1 requirement: "En task
får INTE bli completed bara för att executor säger 'klart'"). verify_task() is never called
from inside a task_type handler's own "did my work succeed" logic — it is a distinct function
called AFTER the handler runs, against the task's own `verification_plan` (structured,
closed-vocabulary steps set by the planner — never free text the executor could reinterpret),
and its result is what actually gates whether a task may become `completed`
(app/mainai_execution/execution_job.py). A handler claiming success never bypasses this."""

import dataclasses
import subprocess
import uuid
from pathlib import PurePosixPath

from app.models.mainai_execution import MainAITask


class VerificationStepError(Exception):
    """Raised for a structurally invalid verification_plan entry (unknown kind, missing
    field) — never silently skipped or treated as a pass."""


def validate_targeted_tests_target(target: object) -> str:
    """Hardening pass finding (P1): a `targeted_tests` step's `target` is AI-proposed content
    (app/mainai_execution/planner.py's propose_plan_via_ai() -- untrusted input per the
    hardening pass's own framing) that ultimately becomes an argv element to a real
    `python -m pytest` subprocess (below, and app/mainai_execution/execution_job.py's
    `_run_pytest()` for `run_tests` tasks). Pytest COLLECTS AND IMPORTS whatever file that
    argument resolves to -- an absolute path or a `..`-escaping relative path would let a
    hallucinated or prompt-injected plan point verification at an arbitrary file on the
    executor's own filesystem and have its module-level code (and any `test_*` function)
    actually run. `_handle_repo_edit()`'s own file-write path already refuses `..` in
    AI-proposed file paths for exactly this class of risk (see its `_parse_code_agent_response()`)
    -- this applies the identical discipline to verification targets, which had been missed.
    Called from THREE places (plan-creation time in planner.py's create_plan(), and both real
    execution call sites here and in execution_job.py) so a target that somehow reached
    execution without ever passing through create_plan() (e.g. a future direct-insert bug) is
    still caught at the actual subprocess boundary, not just at the earlier, bypassable one."""
    if not isinstance(target, str) or not target:
        raise VerificationStepError(f"verification_plan entry of kind 'targeted_tests' requires a non-empty string 'target', got {target!r}.")
    path = PurePosixPath(target)
    if path.is_absolute() or ".." in path.parts:
        raise VerificationStepError(f"verification_plan 'target' must be a relative path with no '..' segments and no leading '/': {target!r}.")
    return target


@dataclasses.dataclass
class VerificationStepResult:
    kind: str
    passed: bool
    detail: dict


@dataclasses.dataclass
class VerificationResult:
    passed: bool
    steps: list[VerificationStepResult]

    def evidence(self) -> dict:
        """Structured, storable evidence — this IS the record verify_task()'s caller persists
        (see execution_job.py's `verification_passed`/`verification_failed` task events), not
        a summary reconstructed from memory later."""
        return {"passed": self.passed, "steps": [dataclasses.asdict(s) for s in self.steps]}


def _run_targeted_tests(step: dict, *, cwd: str) -> VerificationStepResult:
    target = validate_targeted_tests_target(step.get("target"))
    result = subprocess.run(
        ["python", "-m", "pytest", "-q", target],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=step.get("timeout_seconds", 300),
    )
    passed = result.returncode == 0
    # stdout/stderr truncated -- this is stored as durable evidence (MainAITaskEvent.detail,
    # jsonb), never an unbounded blob.
    return VerificationStepResult(
        kind="targeted_tests",
        passed=passed,
        detail={"target": target, "returncode": result.returncode, "stdout_tail": result.stdout[-4000:], "stderr_tail": result.stderr[-2000:]},
    )


_STEP_RUNNERS = {"targeted_tests": _run_targeted_tests}


def verify_task(task: MainAITask, *, cwd: str) -> VerificationResult:
    """Runs every entry in `task.verification_plan` in order. ALL steps must pass for the
    overall result to pass — a single failing step fails the whole verification, never
    averaged or partially credited. An empty verification_plan passes trivially (recorded as
    zero steps, visible as such in the evidence — never silently indistinguishable from "ran
    and passed real checks") — the planner is responsible for populating verification_plan for
    any task whose work has real risk of being wrong; a task genuinely without a verifiable
    side effect (e.g. read_only_audit) legitimately has nothing to verify.

    Raises VerificationStepError for a malformed step or an unknown `kind` -- never silently
    skips a step it doesn't understand and calls that "passed"."""
    steps: list[VerificationStepResult] = []
    for step in task.verification_plan:
        if not isinstance(step, dict) or "kind" not in step:
            raise VerificationStepError(f"Malformed verification_plan entry: {step!r}")
        runner = _STEP_RUNNERS.get(step["kind"])
        if runner is None:
            raise VerificationStepError(f"Unknown verification step kind '{step['kind']}'.")
        steps.append(runner(step, cwd=cwd))

    return VerificationResult(passed=all(s.passed for s in steps), steps=steps)
