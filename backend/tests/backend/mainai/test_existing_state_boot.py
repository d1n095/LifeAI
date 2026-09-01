"""P1 certification: existing-state boot + independent provider ledger cross-check."""

from __future__ import annotations

import uuid

import pytest

from app.mainai_executive.internal_start import run_first_real_internal_boot
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
                "UPDATE mainai_stop_state SET active=false, reason='', sequence=0"
            )
        )
        superuser_db.flush()
    except Exception:
        pass


def test_existing_state_boot_preserves_current_vs_superseded(superuser_db):
    report = run_first_real_internal_boot(
        superuser_db,
        owner_email=f"rich-boot-{uuid.uuid4().hex[:8]}@local.internal",
        seed_existing_state=True,
    )
    assert report.first_task_ok is True
    assert report.provider_call_count == 0
    assert report.provider_ledger_crosscheck.get("unchanged") is True
    assert report.provider_ledger_crosscheck.get("mainai_report_alone_insufficient") is True

    post = report.existing_state_inspect.get("post_boot") or {}
    inv = post.get("invariants") or {}
    assert inv.get("no_oldest_row_as_current") is True
    assert inv.get("superseded_not_in_current") is True
    assert inv.get("disputed_not_in_current") is True
    assert inv.get("no_false_provider_competence") is True
    assert post.get("SUPERSEDED_MEMORY"), "expected superseded history present"
    assert any("CURRENT correction" in (m.get("content") or "") for m in post.get("CURRENT_MEMORY") or [])
    assert "research.local_dry_classify" in (post.get("VERIFIED_CAPABILITIES") or [])
    assert "research.external_provider_invoke" in (post.get("DISABLED_CAPABILITIES") or [])


def test_clean_boot_includes_provider_ledger_crosscheck(superuser_db):
    report = run_first_real_internal_boot(
        superuser_db,
        owner_email=f"ledger-boot-{uuid.uuid4().hex[:8]}@local.internal",
    )
    assert report.provider_call_count == 0
    assert report.provider_ledger_crosscheck["unchanged"] is True
    assert report.provider_ledger_crosscheck["before"]["spend_reservations"] == report.provider_ledger_crosscheck["after"]["spend_reservations"]
    assert (
        report.provider_ledger_crosscheck["before"]["workforce_provider_receipts"]
        == report.provider_ledger_crosscheck["after"]["workforce_provider_receipts"]
    )
