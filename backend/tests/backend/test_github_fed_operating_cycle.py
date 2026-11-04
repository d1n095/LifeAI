"""Phase 3 — operating cycle fed by read-only GitHub intake (needs independent review).

End-to-end over SYNTHETIC GitHub state (an injected GET-only transport) against real Postgres:
Founder objective -> read-only GitHub discovery -> durable ledger -> authorized operating cycle
-> next eligible task, with restart recovery, no duplicate assignment, and provider-unavailable
(rate-limit) handling that pauses only GitHub intake while the cycle still runs.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy import text as sa_text

from app.agent_coordination.github_fed_cycle import run_github_fed_operating_cycle
from app.agent_coordination.github_intake import GitHubReadClient, passing_required_runs
from app.agent_coordination.persist import persist_status
from app.models.agent_coordination import AgentWorkAssignment
from app.request_context import current_user_id as current_user_id_var

_SHA = "1105e331a444c47d80d10265c4947eb1b48aeb72"
_REPO = "d1n095/LifeAI"


@pytest.fixture(autouse=True, scope="module")
def _priv():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _set_rls(session, owner_id):
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


class _Resp:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._body)

    @property
    def content(self):
        return b"x"

    def json(self):
        return self._body


def _ok_transport(sha):
    checks, workflows = passing_required_runs(sha)

    def transport(method, url, headers=None):
        assert method == "GET"  # read-only
        if "/check-runs" in url:
            return _Resp(200, {"check_runs": checks})
        if "/actions/runs" in url:
            return _Resp(200, {"workflow_runs": workflows})
        if "/reviews" in url:
            return _Resp(200, [])
        if "/commits?per_page" in url or url.endswith("/commits"):
            return _Resp(200, [{"sha": sha, "parents": []}])
        # the PR object
        return _Resp(200, {
            "number": 299, "title": "intake", "draft": False, "merged": False, "state": "open",
            "html_url": "https://github.com/d1n095/LifeAI/pull/299",
            "head": {"sha": sha, "ref": "cursor/x"}, "base": {"sha": "0" * 40, "ref": "main"},
            "updated_at": "2026-11-04T00:00:00Z",
        })

    return transport


def _rate_limited_transport():
    def transport(method, url, headers=None):
        return _Resp(403, {"message": "rate"}, {"X-RateLimit-Remaining": "0"})

    return transport


def _owner(make_verified_user):
    user, _ = make_verified_user()
    return user.id


def test_github_fed_cycle_end_to_end(db_session, make_verified_user):
    owner_id = _owner(make_verified_user)
    _set_rls(db_session, owner_id)
    client = GitHubReadClient(repo=_REPO, transport=_ok_transport(_SHA))

    result = run_github_fed_operating_cycle(
        db_session, owner_id=owner_id, goal_text="synthetic: review the open PR", client=client, pr_numbers=[299],
    )
    db_session.commit()

    # Read-only discovery happened and found the reviewable PR.
    assert result["github"]["outcome"] == "applied"
    assert result["github"]["blocked"] is False
    cands = result["github"]["view"]["candidates"]
    assert any(c["pr"] == 299 and c["head_sha"] == _SHA for c in cands)
    # Life now has a next task derived from real GitHub state (independent review of PR 299).
    assert any(r["pr"] == 299 and r["need"] == "independent_review" for r in result["needed_review"])
    # The authorized operating cycle ran, fail-safe invariants intact.
    assert result["cycle"]["first_cycle_go"] is False
    assert result["cycle"]["live_paid_mutate"] is False
    assert result["write_attempted"] is False and result["delivery_claimed"] is False
    assert result["founder_view"]["first_cycle_go"] is False


def test_restart_recovery_and_no_duplicate_assignment(db_session, make_verified_user):
    owner_id = _owner(make_verified_user)
    _set_rls(db_session, owner_id)
    client = GitHubReadClient(repo=_REPO, transport=_ok_transport(_SHA))

    run_github_fed_operating_cycle(db_session, owner_id=owner_id, goal_text="g1", client=client, pr_numbers=[299])
    db_session.commit()

    # Restart: a later tick with NO client (no new discovery) still sees the persisted PR.
    resumed = run_github_fed_operating_cycle(db_session, owner_id=owner_id, goal_text="g2", client=None)
    db_session.commit()
    assert any(c["pr"] == 299 for c in resumed["github"]["view"]["candidates"]), "discovery must survive restart"

    # Re-discovery of the same PR does not create a second ledger-backing row (idempotent).
    run_github_fed_operating_cycle(db_session, owner_id=owner_id, goal_text="g3", client=client, pr_numbers=[299])
    db_session.commit()
    status = persist_status(db_session, owner_id=owner_id)
    backing = [
        r for r in db_session.execute(
            select(AgentWorkAssignment).where(AgentWorkAssignment.owner_id == owner_id)
        ).scalars()
        if (r.provenance or {}).get("ledger_backing")
    ]
    assert len(backing) == 1, "exactly one synthetic ledger-backing row across repeated discovery"
    assert status["created_without_manual_assignment"] is True


def test_provider_unavailable_pauses_only_github(db_session, make_verified_user):
    owner_id = _owner(make_verified_user)
    _set_rls(db_session, owner_id)
    client = GitHubReadClient(repo=_REPO, transport=_rate_limited_transport())

    result = run_github_fed_operating_cycle(
        db_session, owner_id=owner_id, goal_text="synthetic: proceed despite GitHub outage", client=client, pr_numbers=[299],
    )
    db_session.commit()

    # GitHub intake is BLOCKED with a named missing capability (WAITING_FOR_PROVIDER-style)...
    assert result["github"]["blocked"] is True
    assert result["github"]["missing_capability"] == "github_read_rate_limit"
    # ...but the authorized cycle still ran — a GitHub outage does not block unrelated work.
    assert result["cycle"]["first_cycle_go"] is False
    assert "outcome" in result["cycle"]
    assert result["delivery_claimed"] is False
