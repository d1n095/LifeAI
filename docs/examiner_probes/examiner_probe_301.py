"""EXAMINER PROBE (not part of the candidate) — reproduce DEF-1/GH-4 on #301.

Claim under test: GitHub discovery WITHOUT an existing AgentWorkAssignment does not persist
its obligations, so a reload loses them and a later refresh recreates them with new ids.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy import text as sa_text

from app.agent_coordination import lifecycle
from app.agent_coordination.durable import load_owner_ledger
from app.agent_coordination.github_intake import GitHubDevelopmentIntake, passing_required_runs
from app.models.agent_coordination import AgentWorkAssignment
from app.request_context import current_user_id as current_user_id_var

import pytest


@pytest.fixture(autouse=True, scope="module")
def _priv(_apply=None):
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _set_rls(session, owner_id):
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


_SHA = "1105e331a444c47d80d10265c4947eb1b48aeb72"


def _reviewable_pr(sha):
    checks, workflows = passing_required_runs(sha)
    return {
        "number": 299, "title": "intake", "draft": False, "merged": False, "state": "open",
        "html_url": "https://github.com/d1n095/LifeAI/pull/299", "head_sha": sha,
        "base_sha": None, "head_branch": "cursor/x", "base_branch": "main",
        "ancestry": [sha], "reviews": [], "check_runs": checks, "workflow_runs": workflows,
    }


def test_def1_gh4_pure_github_discovery_is_not_durable(db_session, make_verified_user):
    user, _ = make_verified_user()
    owner_id = user.id
    _set_rls(db_session, owner_id)

    # Pure GitHub discovery: NO AgentWorkAssignment rows exist for this owner.
    assert db_session.execute(
        select(AgentWorkAssignment).where(AgentWorkAssignment.owner_id == owner_id)
    ).first() is None

    intake = GitHubDevelopmentIntake(owner_id=str(owner_id))
    applied = intake.ingest_pull_request(_reviewable_pr(_SHA))
    e1 = applied["examiner_assignment_id"]
    assert e1 is not None
    assert str(intake.state["candidates"].get("299", {}).get("head_sha")) == _SHA

    # Persist whatever the lifecycle layer would persist for this owner.
    lifecycle._persist_ledger(db_session, owner_id=owner_id, service=intake.service)
    db_session.commit()

    # Still no AgentWorkAssignment row was created to carry the obligation...
    rows_after = db_session.execute(
        select(AgentWorkAssignment).where(AgentWorkAssignment.owner_id == owner_id)
    ).all()

    # Reload the ledger strictly from the database.
    reloaded = load_owner_ledger(db_session, owner_id=owner_id)
    reloaded_candidates = (reloaded._github_intake or {}).get("candidates") or {}

    print(f"EXAMINER-301 assignment_rows_after_persist={len(rows_after)} "
          f"reloaded_github_candidates={len(reloaded_candidates)}")

    # DEFECT (DEF-1/GH-4): the discovered candidate did not survive the reload.
    assert len(rows_after) == 0, "no backing assignment row was created for the discovery"
    assert reloaded_candidates == {}, (
        "EXPECTED the defect: github discovery is lost on reload when no AgentWorkAssignment "
        f"exists; instead reloaded candidates = {list(reloaded_candidates)}"
    )
