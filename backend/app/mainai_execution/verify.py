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

from app.models.mainai_execution import MainAITask


class VerificationStepError(Exception):
    """Raised for a structurally invalid verification_plan entry (unknown kind, missing
    field) — never silently skipped or treated as a pass."""


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
    target = step.get("target")
    if not target:
        raise VerificationStepError("verification_plan entry of kind 'targeted_tests' requires a 'target' field.")
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
