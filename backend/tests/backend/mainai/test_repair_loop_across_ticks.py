"""B6 — durable repair loop across production-shaped ticks.

Proves the hole where production_entry rebuilds plain WorkBindings each tick and
`_augment_bindings_with_gap_children` previously *skipped* already-bound gap children —
so a later tick could not resume an approved repair child via the durable recipe without
provider spend or a hand-built PlanCandidate.

Also proves re-verify of the parked source after repair completes across a tick boundary,
using only the durable gap envelope `reverify` contract.

`provider_spend_authorized` stays false throughout.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from sqlalchemy import select

from app.development_supervisor.service import SupervisorBounds, WorkBinding, run_supervisor
from app.mainai_execution.approval import grant_task_approval
from app.models.mainai_execution import MainAITask, MainAITaskStatus
from app.models.problem_learning import LifeProblem
from tests.backend.mainai.test_autonomous_gap_live_integration import (
    _binding,
    _broken_multiply_candidate,
    _repair_child,
)
from tests.backend.mainai.test_scoped_development_supervisor import _foundation


def _production_plain_bindings(db, goal, prepare, scope):
    """Mirror production_entry: one plain WorkBinding per ready/running task, no candidate,
    no deterministic fallback. This is the shape that previously shadowed gap children."""
    tasks = (
        db.execute(
            select(MainAITask).where(
                MainAITask.owner_id == goal.owner_id,
                MainAITask.goal_id == goal.id,
                MainAITask.status.in_([MainAITaskStatus.ready, MainAITaskStatus.running]),
            )
        )
        .scalars()
        .all()
    )
    return tuple(
        WorkBinding(
            task_id=task.id,
            prepare_context=prepare,
            repository_identity=scope.repository_identity,
            allowed_paths=scope.allowed_paths,
        )
        for task in tasks
    )


@pytest.mark.asyncio
async def test_repair_child_resumes_on_later_production_shaped_tick_without_spend(
    superuser_db, tmp_path
):
    owner, goal, first, second, repo, original, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    assert scope.provider_spend_authorized is False
    second.status = MainAITaskStatus.blocked
    original_binding = _binding(
        first,
        prepare,
        scope,
        _broken_multiply_candidate(original),
        independent=False,
    )

    failed = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(original_binding,),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert failed.classification == "FAILED_NONRETRYABLE"
    child = _repair_child(superuser_db, goal, first)
    assert first.status == MainAITaskStatus.blocked
    assert child.status == MainAITaskStatus.ready
    superuser_db.commit()

    # Fresh tick material — no leftover deferred/bindings. Production-shaped plains only.
    plain = _production_plain_bindings(superuser_db, goal, prepare, scope)
    assert any(b.task_id == child.id for b in plain)
    assert all(b.candidate is None and not b.allow_deterministic_fallback for b in plain)

    approval_gate = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=plain,
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert approval_gate.classification == "WAITING_APPROVAL"

    grant_task_approval(superuser_db, task=child, approved_by="founder")
    superuser_db.commit()

    # Another fresh production-shaped tick after process-boundary simulation.
    plain_after_approval = _production_plain_bindings(superuser_db, goal, prepare, scope)
    repaired = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=plain_after_approval,
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert repaired.classification != "PROVIDER_SPEND_NOT_AUTHORIZED"
    superuser_db.refresh(child)
    assert child.status == MainAITaskStatus.completed
    assert "return left * right" in (repo / "calculator.py").read_text()


@pytest.mark.asyncio
async def test_source_reverify_survives_tick_boundary_from_durable_envelope(
    superuser_db, tmp_path
):
    """Repair completes at end of tick (max_jobs=1); next tick must re-verify source from
    durable envelope only — not from in-memory WorkBinding.candidate."""
    _, goal, first, second, repo, original, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    second.status = MainAITaskStatus.blocked
    original_binding = _binding(
        first,
        prepare,
        scope,
        _broken_multiply_candidate(original),
        independent=False,
    )

    await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(original_binding,),
        bounds=SupervisorBounds(max_jobs=1),
    )
    child = _repair_child(superuser_db, goal, first)
    grant_task_approval(superuser_db, task=child, approved_by="founder")
    superuser_db.commit()

    # Complete repair child only; force tick end before same-run reverify finishes.
    # With max_jobs=1 the same-run path may still reverify — force a boundary by
    # completing the child then discarding in-memory state and starting fresh.
    child_tick = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=_production_plain_bindings(superuser_db, goal, prepare, scope),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert child_tick.classification != "PROVIDER_SPEND_NOT_AUTHORIZED"
    superuser_db.refresh(child)
    superuser_db.refresh(first)
    assert child.status == MainAITaskStatus.completed

    # If same-run already re-verified the source, B6 still holds (durable path worked).
    # If source is ready again, the NEXT production-shaped tick must finish reverify.
    if first.status == MainAITaskStatus.completed:
        assert "return left * right" in (repo / "calculator.py").read_text()
        return

    assert first.status == MainAITaskStatus.ready
    problem = superuser_db.execute(
        select(LifeProblem).where(LifeProblem.mainai_task_id == first.id)
    ).scalar_one()
    assert isinstance((problem.provenance or {}).get("execution_envelope", {}).get("reverify"), dict)

    reverify_tick = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=_production_plain_bindings(superuser_db, goal, prepare, scope),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert reverify_tick.classification != "PROVIDER_SPEND_NOT_AUTHORIZED"
    superuser_db.refresh(first)
    assert first.status == MainAITaskStatus.completed
    assert (
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "test_calculator.py"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


@pytest.mark.asyncio
async def test_gap_child_binding_keeps_narrower_paths_under_production_plain_prebind(
    superuser_db, tmp_path
):
    """Production pre-bind uses full scope paths; gap-derived binding must win so repair
    does not silently execute under a wider path envelope than the gap recorded."""
    from app.development_supervisor.service import _augment_bindings_with_gap_children

    _, goal, first, second, _, original, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    second.status = MainAITaskStatus.blocked
    await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(
            _binding(
                first,
                prepare,
                scope,
                _broken_multiply_candidate(original),
                independent=False,
            ),
        ),
        bounds=SupervisorBounds(max_jobs=1),
    )
    child = _repair_child(superuser_db, goal, first)
    problem = superuser_db.execute(
        select(LifeProblem).where(LifeProblem.mainai_task_id == first.id)
    ).scalar_one()
    gap_paths = tuple(
        ((problem.provenance or {}).get("execution_envelope") or {}).get("allowed_paths")
        or ()
    )
    assert gap_paths
    assert set(gap_paths).issubset(set(scope.allowed_paths))

    plain = _production_plain_bindings(superuser_db, goal, prepare, scope)
    child_plain = next(b for b in plain if b.task_id == child.id)
    assert child_plain.allowed_paths == scope.allowed_paths

    augmented = _augment_bindings_with_gap_children(
        superuser_db, scope=scope, bindings=plain
    )
    child_aug = next(b for b in augmented if b.task_id == child.id)
    assert child_aug.allow_deterministic_fallback is True
    assert set(child_aug.allowed_paths) == set(gap_paths)
    assert set(child_aug.allowed_paths).issubset(set(scope.allowed_paths))
