"""First real safe-internal boot from merged tip path."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.mainai_executive.internal_start import (
    run_first_real_internal_boot,
    startup_status_surface,
)
from app.models.user import User
from app.request_context import current_user_id as current_user_id_var
from app.workforce.kill_switch import reset_kill_switch_for_tests


@pytest.fixture(autouse=True, scope="module")
def _priv():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture(autouse=True)
def _ks(superuser_db):
    reset_kill_switch_for_tests(superuser_db)
    try:
        superuser_db.execute(
            __import__("sqlalchemy", fromlist=["text"]).text(
            )
        )
        superuser_db.flush()
    except Exception:
        pass


def test_first_real_internal_boot_milestone(superuser_db):
    report = run_first_real_internal_boot(
        superuser_db,
        owner_email=f"boot-test-{uuid.uuid4().hex[:8]}@local.internal",
    )
    assert report.first_task_ok is True
    assert report.local_attempt_used is True
    assert report.school_used is True
    assert report.provider_call_count == 0
    assert report.shutdown_ok is True
    assert report.restart_ok is True
    assert report.resume_ok is True
    assert report.offline_ok is True
    assert report.durable_receipts["continuity_note_id"]
    assert report.durable_receipts["provider_invoked"] is False
    assert report.status_surface["PROVIDER_ENABLED"] is False
    assert report.status_surface["READINESS"]
    assert "local_reasoning" in report.safe_internal_boundary["allowed"]
    assert "real_external_provider_invocation" in report.safe_internal_boundary["forbidden"]
