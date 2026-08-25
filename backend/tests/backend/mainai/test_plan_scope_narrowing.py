"""Plan-derived task scope narrowing (Autonomy Activation B4).

Proves PLANNER OUTPUT != FOUNDER AUTHORITY: narrowing is always intersection with the
envelope ceiling; escape fails closed; empty citations do not fall back to full envelope.
"""

import pytest

from app.development_supervisor.plan_scope_narrowing import (
    PlanScopeNarrowingError,
    extract_plan_citations,
    narrow_task_scope_from_accepted_plan,
)
from app.safe_planner.service import CandidateStep, PlanCandidate


def _candidate(*steps: CandidateStep) -> PlanCandidate:
    return PlanCandidate(
        interpretation="bounded edit",
        requested_outcome="local verified change",
        rationale="cite only needed paths",
        steps=steps,
    )


def test_extracts_paths_and_capabilities_from_plan_steps():
    candidate = _candidate(
        CandidateStep(
            "read", "inspect", "text", "read_file", {"path": "backend/app/foo.py"}
        ),
        CandidateStep(
            "patch",
            "edit",
            "hash",
            "patch_file",
            {"path": "backend/app/foo.py", "content": "x", "expected_sha256": "a" * 64},
        ),
        CandidateStep(
            "test",
            "verify",
            "ok",
            "run_focused_test",
            {"profile_name": "focused_pytest", "arguments": ["backend/tests/test_foo.py"]},
        ),
    )
    paths, caps = extract_plan_citations(candidate)
    assert paths == ("backend/app/foo.py", "backend/tests/test_foo.py")
    assert caps == ("read_file", "patch_file", "run_focused_test")


def test_narrowing_is_intersection_never_widens_past_envelope():
    candidate = _candidate(
        CandidateStep("r", "inspect", "t", "read_file", {"path": "a.py"}),
        CandidateStep("p", "edit", "h", "patch_file", {"path": "a.py"}),
    )
    narrowed = narrow_task_scope_from_accepted_plan(
        envelope_paths=("a.py", "b.py", "c.py"),
        envelope_capabilities=("read_file", "patch_file", "run_focused_test", "create_file"),
        candidate=candidate,
    )
    assert narrowed.allowed_paths == ("a.py",)
    assert narrowed.allowed_capabilities == ("read_file", "patch_file")
    assert "b.py" not in narrowed.allowed_paths
    assert "create_file" not in narrowed.allowed_capabilities


def test_plan_path_outside_envelope_fails_closed():
    candidate = _candidate(
        CandidateStep("r", "inspect", "t", "read_file", {"path": "secret.py"}),
    )
    with pytest.raises(PlanScopeNarrowingError, match="outside the founder envelope"):
        narrow_task_scope_from_accepted_plan(
            envelope_paths=("a.py",),
            envelope_capabilities=("read_file",),
            candidate=candidate,
        )


def test_plan_capability_outside_envelope_fails_closed():
    candidate = _candidate(
        CandidateStep("r", "inspect", "t", "push_branch", {"path": "a.py"}),
    )
    with pytest.raises(PlanScopeNarrowingError, match="capabilities outside"):
        narrow_task_scope_from_accepted_plan(
            envelope_paths=("a.py",),
            envelope_capabilities=("read_file", "patch_file"),
            candidate=candidate,
        )


def test_empty_citations_do_not_fall_back_to_full_envelope():
    candidate = _candidate(
        CandidateStep("gate", "evaluate", "ok", "verification_evaluate", {}),
    )
    narrowed = narrow_task_scope_from_accepted_plan(
        envelope_paths=("a.py", "b.py"),
        envelope_capabilities=("read_file", "patch_file", "verification_evaluate"),
        candidate=candidate,
    )
    assert narrowed.allowed_paths == ()
    assert narrowed.allowed_capabilities == ("verification_evaluate",)
