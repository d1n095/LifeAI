"""Founder cancel after verify success / before task finalize.

Past verified filesystem effects remain historical. Late finalize must not
convert an authoritative cancel into task/job completed.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import select

from app.development_driver.service import DevelopmentPlan, DriverStep, run_driver
from app.development_operator.service import LOCAL_EXECUTION, LOCAL_WRITE, READ_ONLY
from app.mainai_execution import checkpoint as checkpoint_module
from app.models.mainai_execution import MainAITaskEvent, MainAITaskEventType, MainAITaskStatus
from app.models.mainai_job import MainAIJobStatus
from tests.backend.mainai.test_autonomous_development_driver import (
    _driver_foundation,
    _plan,
)


def test_cancel_after_verify_before_finalize_keeps_past_fs_refuses_completed(
    superuser_db, tmp_path, monkeypatch
):
    _, _, task, job, _, context = _driver_foundation(superuser_db, tmp_path)
    context = replace(context, allowed_paths=("calc.py", "test_calc.py"))
    calc = "def add(left, right):\n    return left + right\n"
    test = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"

    real_record = checkpoint_module.record_checkpoint

    def _record_then_cancel(db, **kwargs):
        result = real_record(db, **kwargs)
        if kwargs.get("step") == "verification":
            verification = (kwargs.get("data") or {}).get("verification") or {}
            if verification.get("passed") is True:
                job.cancel_requested = True
                db.flush()
        return result

    monkeypatch.setattr(checkpoint_module, "record_checkpoint", _record_then_cancel)
    # Driver imports record_checkpoint into its module namespace.
    import app.development_driver.service as driver_svc

    monkeypatch.setattr(driver_svc, "record_checkpoint", _record_then_cancel)

    plan = _plan(
        context,
        "cancel-after-verify",
        DriverStep(
            "create_file",
            "add deterministic function",
            "new source hash",
            {"path": "calc.py", "content": calc, "expected_sha256": None},
            LOCAL_WRITE,
        ),
        DriverStep(
            "create_file",
            "add focused regression test",
            "new test hash",
            {"path": "test_calc.py", "content": test, "expected_sha256": None},
            LOCAL_WRITE,
        ),
        DriverStep(
            "run_focused_test",
            "prove deterministic behavior",
            "pytest exit zero",
            {"profile_name": "focused_pytest", "arguments": ["test_calc.py"]},
            LOCAL_EXECUTION,
            verification_required=True,
        ),
        DriverStep(
            "verification_evaluate",
            "evaluate required operator evidence",
            "verification checkpoint",
            required_risk=READ_ONLY,
        ),
    )
    result = run_driver(superuser_db, context=context, plan=plan, max_actions=10)
    assert result.classification == "CANCELLED"
    superuser_db.refresh(task)
    superuser_db.refresh(job)
    assert task.status == MainAITaskStatus.cancelled
    assert job.status == MainAIJobStatus.cancelled
    # Past effect preserved.
    assert (context.repository_root / "calc.py").read_text(encoding="utf-8") == calc
    assert (context.repository_root / "test_calc.py").read_text(encoding="utf-8") == test
    events = {
        e.event_type
        for e in superuser_db.execute(
            select(MainAITaskEvent).where(MainAITaskEvent.task_id == task.id)
        ).scalars()
    }
    assert MainAITaskEventType.cancelled in events
    assert MainAITaskEventType.completed not in events
