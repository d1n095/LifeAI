"""EXAMINER PROBE (not part of candidate) — verify #303 fixes DEF-1/GH-4 and idempotency.

Same scenario that failed on #301: pure GitHub discovery with NO prior AgentWorkAssignment.
On #303 the candidate must now SURVIVE a reload (durable), and a second discovery+persist
must NOT create a duplicate backing row (bounded, idempotent).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy import text as sa_text

from app.agent_coordination import lifecycle
from app.agent_coordination.durable import load_owner_ledger
from app.agent_coordination.github_intake import GitHubDevelopmentIntake, passing_required_runs
from app.agent_coordination.persist import persist_status
from app.models.agent_coordination import AgentWorkAssignment
from app.request_context import current_user_id as current_user_id_var

import pytest


@pytest.fixture(autouse=True, scope="module")
def _priv():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _set_rls(session, owner_id):
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


_SHA = "1105e331a444c47d80d10265c4947eb1b48aeb72"


def _pr(sha):
    checks, workflows = passing_required_runs(sha)
    return {
        "number": 299, "title": "intake", "draft": False, "merged": False, "state": "open",
        "html_url": "https://github.com/d1n095/LifeAI/pull/299", "head_sha": sha,
        "base_sha": None, "head_branch": "cursor/x", "base_branch": "main",
        "ancestry": [sha], "reviews": [], "check_runs": checks, "workflow_runs": workflows,
    }


def test_def1_gh4_fixed_cold_github_discovery_survives_reload(db_session, make_verified_user):
    user, _ = make_verified_user()
    owner_id = user.id
    _set_rls(db_session, owner_id)

    assert db_session.execute(
        select(AgentWorkAssignment).where(AgentWorkAssignment.owner_id == owner_id)
    ).first() is None  # cold start — no prior assignment

    intake = GitHubDevelopmentIntake(owner_id=str(owner_id))
    applied = intake.ingest_pull_request(_pr(_SHA))
    e1 = applied["examiner_assignment_id"]
    lifecycle._persist_ledger(db_session, owner_id=owner_id, service=intake.service)
    db_session.commit()

    status = persist_status(db_session, owner_id=owner_id)
    reloaded = load_owner_ledger(db_session, owner_id=owner_id)
    cands = (reloaded._github_intake or {}).get("candidates") or {}
    rows = db_session.execute(
        select(AgentWorkAssignment).where(AgentWorkAssignment.owner_id == owner_id)
    ).scalars().all()
    print(f"EXAMINER-303 backing_rows={len(rows)} cold_backing={status['created_without_manual_assignment']} "
          f"reloaded_candidates={list(cands)} e1={e1}")

    # FIX: a synthetic backing row now carries the ledger, and the candidate survives reload.
    assert status["created_without_manual_assignment"] is True
    assert "299" in cands and cands["299"]["head_sha"] == _SHA

    # Idempotency/no-duplicate: a 2nd cold discovery+persist reuses the SAME backing row.
    intake2 = GitHubDevelopmentIntake(owner_id=str(owner_id))
    intake2.ingest_pull_request(_pr(_SHA))
    lifecycle._persist_ledger(db_session, owner_id=owner_id, service=intake2.service)
    db_session.commit()
    rows2 = db_session.execute(
        select(AgentWorkAssignment).where(AgentWorkAssignment.owner_id == owner_id)
    ).scalars().all()
    backing_rows = [r for r in rows2 if (r.provenance or {}).get("ledger_backing")]
    print(f"EXAMINER-303 after_second_discovery total_rows={len(rows2)} backing_rows={len(backing_rows)}")
    assert len(backing_rows) == 1  # exactly one synthetic backing row, not recreated
