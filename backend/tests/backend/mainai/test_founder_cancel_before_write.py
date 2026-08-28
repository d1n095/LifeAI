"""Founder cancel after plan ACCEPT / before Operator write → zero future FS effect.

Past effects remain historical. cancel_requested at effect time must refuse write_file.
Driver must stop between steps when cancel lands mid-flight.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.development_driver import service as driver_svc
from app.development_driver.service import DevelopmentPlan, DriverStep, run_driver
from app.development_operator.service import (
    LOCAL_WRITE,
    OperatorAuthorizationError,
    write_file,
)
from app.models.mainai_execution import MainAITaskStatus
from app.models.work_intelligence import WorkTraceEvent
from tests.backend.mainai.test_development_operator import _foundation


def test_cancel_requested_before_write_produces_zero_filesystem_effect(
    superuser_db, tmp_path
):
    _, _, _, job, _, context = _foundation(superuser_db, tmp_path)
    target = context.repository_root / "safe.txt"
    original = target.read_text(encoding="utf-8")
    before = hashlib.sha256(original.encode()).hexdigest()

    # Simulate founder cancel after planning / before write.
    job.cancel_requested = True
    superuser_db.flush()

    with pytest.raises(OperatorAuthorizationError, match="cancel requested"):
        write_file(
            superuser_db,
            context,
            path="safe.txt",
            content="after-cancel\n",
            expected_sha256=before,
            idempotency_key="cancel-before-write",
        )
    assert target.read_text(encoding="utf-8") == original


def test_mid_driver_cancel_after_first_write_blocks_second_write(
    superuser_db, tmp_path, monkeypatch
):
    """Cancel after ACCEPTED plan step 1 succeeds → step 2 must not mutate FS."""
    _, _, task, job, _, context = _foundation(superuser_db, tmp_path)
    task.status = MainAITaskStatus.running
    superuser_db.flush()
    context = replace(context, allowed_paths=("a.txt", "b.txt"))

    real_invoke = driver_svc._invoke_operator

    def _invoke_then_cancel(db, ctx, step, idem):
        result = real_invoke(db, ctx, step, idem)
        job.cancel_requested = True
        db.flush()
        return result

    monkeypatch.setattr(driver_svc, "_invoke_operator", _invoke_then_cancel)

    plan = DevelopmentPlan(
        plan_id="cancel-mid-write",
        steps=(
            DriverStep(
                "create_file",
                "first authorized write",
                "a.txt exists",
                {"path": "a.txt", "content": "first\n", "expected_sha256": None},
                LOCAL_WRITE,
            ),
            DriverStep(
                "create_file",
                "second write must not run after cancel",
                "b.txt must not exist",
                {"path": "b.txt", "content": "second\n", "expected_sha256": None},
                LOCAL_WRITE,
            ),
        ),
        strategy_execution_id=context.strategy_execution_id,
        max_failures=2,
    )
    result = run_driver(superuser_db, context=context, plan=plan, max_actions=10)
    assert result.classification == "CANCELLED"
    assert result.completed_steps == 1
    assert (context.repository_root / "a.txt").read_text(encoding="utf-8") == "first\n"
    assert not (context.repository_root / "b.txt").exists()
    caps = [
        e.action_detail["operator_capability"]
        for e in superuser_db.execute(
            select(WorkTraceEvent).order_by(WorkTraceEvent.sequence_number)
        ).scalars()
    ]
    assert caps == ["create_file"]
