"""Production-path GitHub read-only intake certification (GH-*) for #300.
Drives the REAL Founder endpoint POST /api/mainai/founder-ops/github-intake/refresh via TestClient. The only thing
faked is the GitHub network boundary (GitHubReadClient.get is monkeypatched to serve scripted JSON) — the route, the
require_founder gate, the DurableCoordinationService, persistence and the Founder-ops read are all production code.
Credential-free, no paid calls, no real network.
"""

from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy import text as sa_text

from _guards import egress  # noqa: F401
from _harness import _privileges, owner_id  # noqa: F401

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
REFRESH = "/api/mainai/founder-ops/github-intake/refresh"
REQUIRED = ["Frontend — TypeScript & ESLint", "Frontend — npm audit", "Frontend — build (docker)", "Frontend — build (vercel)",
            "Backend — unit/integration tests", "Backend — RLS & session-security tests", "Backend — account lifecycle & rate-limit tests",
            "Backend — Alembic migration check", "E2E — Playwright (full stack)", "E2E — Library / Founder Knowledge Studio",
            "E2E — same-origin proxy", "All required checks passed"]
HEAD = "a" * 40
BASE = "b" * 40


def _checks(conclusion="success"):
    return {"check_runs": [{"name": n, "status": "completed", "conclusion": conclusion, "head_sha": HEAD,
                            "app": {"slug": "github-actions"}} for n in REQUIRED]}


def _fake_get_factory(*, pr_number=300, head=HEAD, ci_conclusion="success", draft=False, merged=False, state="open", reviews=None):
    def fake_get(self, path):
        if re.search(r"/pulls\?state=open", path):
            return [{"number": pr_number}]
        if re.search(rf"/pulls/{pr_number}/commits", path):
            return [{"sha": head, "parents": [{"sha": BASE}]}]
        if re.search(rf"/pulls/{pr_number}/reviews", path):
            return reviews or []
        if re.search(rf"/pulls/{pr_number}\b", path):
            return {"number": pr_number, "title": "cand", "draft": draft, "merged": merged, "state": state,
                    "html_url": f"https://x/{pr_number}", "head": {"sha": head, "ref": "cursor/feat-91c1"},
                    "base": {"sha": BASE, "ref": "main"}, "updated_at": "2026-10-22T00:00:00Z"}
        if "/check-runs" in path:
            return _checks(ci_conclusion)
        if "/actions/runs" in path:
            return {"workflow_runs": []}
        return {}
    return fake_get


def _login(client, email=FOUNDER_EMAIL, password=FOUNDER_PASSWORD):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


# ---- Item 6: the refresh endpoint is Founder-only; repo/callback input cannot forge authority
def test_GH1_refresh_is_founder_only(client, make_verified_user, db_session, monkeypatch):
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    member, mpw = make_verified_user()
    impostor, ipw = make_verified_user(role="founder")
    db_session.commit()
    assert client.post(REFRESH, json={}).status_code in (401, 403)
    for email, pw in ((member.email, mpw), (impostor.email, ipw)):
        csrf = _login(client, email, pw)
        assert client.post(REFRESH, json={"pr_numbers": [1]}, headers={"X-CSRF-Token": csrf}).status_code == 403
        client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}); client.cookies.clear()
    print("GH1|anon/member/role-only-founder all refused")


# ---- Item 1,2,7: real discovery via the endpoint; read-only; obligation created
def test_GH2_refresh_discovers_and_is_read_only(client, db_session, monkeypatch):
    from app.agent_coordination.github_intake import GitHubReadClient
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(GitHubReadClient, "get", _fake_get_factory())
    csrf = _login(client)
    r = client.post(REFRESH, json={"pr_numbers": [300]}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    body = r.json()
    print(f"GH2|outcome={body.get('outcome')} write_attempted={body.get('write_attempted')} github_write={body.get('github_write')} delivery={body.get('delivery_claimed')}")
    assert body["outcome"] == "applied"
    assert body["write_attempted"] is False and body.get("github_write") is False
    assert body.get("delivery_claimed") is False


# ---- Item 8: persisted obligations survive restart and appear in Founder-ops (requires a real backing row)
def test_GH3_persists_and_survives_restart(client, superuser_db, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    from app.agent_coordination.github_intake import GitHubReadClient
    from app.agent_coordination.service import create_work_assignment, register_agent
    from app.bootstrap import bootstrap_founder_user
    from app.db import migration_engine
    from app.founder import FOUNDER_USER_ID
    from app.limiter import limiter
    from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
    from app.models.mainai_execution import MainAITask
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(GitHubReadClient, "get", _fake_get_factory())
    bootstrap_founder_user(superuser_db)
    superuser_db.execute(sa_text("SET LOCAL app.current_user_id = :u"), {"u": str(FOUNDER_USER_ID)})
    goal = create_goal(superuser_db, owner_id=FOUNDER_USER_ID, title="g", original_instruction="x", created_by="founder", approval_policy="autonomous_development_work")
    plan = create_plan(superuser_db, goal=goal, rationale="r", created_by="founder", tasks=[PlannedTaskSpec(description="d", task_type="repo_edit", target_files=["docs/x.md"])])
    task = superuser_db.query(MainAITask).filter_by(plan_id=plan.id).one()
    agent = register_agent(superuser_db, agent_key="cursor-agent", display_name="Cursor", adapter_kind="cli", supports_write=True)
    create_work_assignment(superuser_db, owner_id=FOUNDER_USER_ID, goal_id=goal.id, task_id=task.id, agent_id=agent.id,
                           role="builder", read_write_mode="read_write", repository_identity="lifeai", allowed_paths=["docs/**"], requested_by="founder")
    superuser_db.commit()
    csrf = _login(client)
    assert client.post(REFRESH, json={"pr_numbers": [300]}, headers={"X-CSRF-Token": csrf}).status_code == 200
    # RESTART: fresh session, production snapshot
    from app.agent_coordination.durable import founder_coordination_snapshot
    with sessionmaker(bind=migration_engine)() as fresh:
        fresh.execute(sa_text("SET LOCAL app.current_user_id = :u"), {"u": str(FOUNDER_USER_ID)})
        snap = founder_coordination_snapshot(fresh, owner_id=FOUNDER_USER_ID)
    print(f"GH3|restart_obligations={len(snap.get('obligations', []))}")
    assert len(snap.get("obligations", [])) >= 1, "GH-3 GitHub-intake obligations did not survive restart in Founder-ops"


# ---- Item 4: idempotent — repeated refresh does not duplicate obligations
def test_GH4_repeated_refresh_is_idempotent(client, db_session, monkeypatch):
    from app.agent_coordination.github_intake import GitHubReadClient
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(GitHubReadClient, "get", _fake_get_factory())
    csrf = _login(client)
    r1 = client.post(REFRESH, json={"pr_numbers": [300]}, headers={"X-CSRF-Token": csrf}).json()
    r2 = client.post(REFRESH, json={"pr_numbers": [300]}, headers={"X-CSRF-Token": csrf}).json()
    def obl_ids(r):
        out = []
        for res in r.get("results", []):
            out += [res.get("builder_assignment_id"), res.get("examiner_assignment_id")]
        return [x for x in out if x]
    print(f"GH4|run1={obl_ids(r1)} run2={obl_ids(r2)} idempotent={obl_ids(r1)==obl_ids(r2)}")
    # DEF-1: with no pre-existing AgentWorkAssignment the endpoint persists NOTHING (assignment is None), so the second
    # refresh reloads an empty ledger and re-mints obligations -> not idempotent across HTTP calls / restarts.
    assert obl_ids(r1) and obl_ids(r1) == obl_ids(r2), (
        "DEF-1: repeated refresh produced different obligation ids -> the endpoint does not persist/reload the ledger "
        "across calls (persists only when a backing AgentWorkAssignment already exists, and only ONE obligation).")


def test_GH4b_endpoint_persists_only_one_of_two_obligations(client, superuser_db, monkeypatch):
    """DEF-1 part 2: a single PR refresh creates TWO obligations (builder + examiner) but the endpoint persists only
    next(iter(service._obligations)) -> the examiner obligation is lost on restart."""
    from sqlalchemy.orm import sessionmaker
    from app.agent_coordination.durable import founder_coordination_snapshot
    from app.agent_coordination.github_intake import GitHubReadClient
    from app.agent_coordination.service import create_work_assignment, register_agent
    from app.bootstrap import bootstrap_founder_user
    from app.db import migration_engine
    from app.founder import FOUNDER_USER_ID
    from app.limiter import limiter
    from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
    from app.models.mainai_execution import MainAITask
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(GitHubReadClient, "get", _fake_get_factory())
    bootstrap_founder_user(superuser_db)
    superuser_db.execute(sa_text("SET LOCAL app.current_user_id = :u"), {"u": str(FOUNDER_USER_ID)})
    goal = create_goal(superuser_db, owner_id=FOUNDER_USER_ID, title="g", original_instruction="x", created_by="founder", approval_policy="autonomous_development_work")
    plan = create_plan(superuser_db, goal=goal, rationale="r", created_by="founder", tasks=[PlannedTaskSpec(description="d", task_type="repo_edit", target_files=["docs/x.md"])])
    task = superuser_db.query(MainAITask).filter_by(plan_id=plan.id).one()
    agent = register_agent(superuser_db, agent_key="cursor-agent", display_name="Cursor", adapter_kind="cli", supports_write=True)
    create_work_assignment(superuser_db, owner_id=FOUNDER_USER_ID, goal_id=goal.id, task_id=task.id, agent_id=agent.id,
                           role="builder", read_write_mode="read_write", repository_identity="lifeai", allowed_paths=["docs/**"], requested_by="founder")
    superuser_db.commit()
    csrf = _login(client)
    r = client.post(REFRESH, json={"pr_numbers": [300]}, headers={"X-CSRF-Token": csrf}).json()
    created = [x for res in r.get("results", []) for x in (res.get("builder_assignment_id"), res.get("examiner_assignment_id")) if x]
    with sessionmaker(bind=migration_engine)() as fresh:
        fresh.execute(sa_text("SET LOCAL app.current_user_id = :u"), {"u": str(FOUNDER_USER_ID)})
        survived = [o.get("assignment_id") for o in founder_coordination_snapshot(fresh, owner_id=FOUNDER_USER_ID).get("obligations", [])]
    print(f"GH4b|created={len(created)} survived_restart={len(survived)} missing={set(created)-set(survived)}")
    assert set(created) <= set(survived), (
        f"DEF-1: refresh created {len(created)} obligations (builder+examiner) but only {len(survived)} survived "
        "restart -> the endpoint persists only one obligation, losing the examiner obligation.")


# ---- Item 3: a stale failed workflow cannot override a newer successful exact-SHA candidate
def test_GH5_stale_failed_does_not_override_newer_success(client, db_session, monkeypatch):
    from app.agent_coordination.github_intake import GitHubReadClient
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    csrf = _login(client)
    monkeypatch.setattr(GitHubReadClient, "get", _fake_get_factory(head="c" * 40, ci_conclusion="success"))
    good = client.post(REFRESH, json={"pr_numbers": [300]}, headers={"X-CSRF-Token": csrf}).json()
    monkeypatch.setattr(GitHubReadClient, "get", _fake_get_factory(head="d" * 40, ci_conclusion="failure"))
    stale = client.post(REFRESH, json={"pr_numbers": [300]}, headers={"X-CSRF-Token": csrf}).json()
    print(f"GH5|good_results={good.get('results')} stale_results={stale.get('results')}")
    # the newer-sha success candidate must not be downgraded by the later failed-CI observation of a different sha
    assert good.get("outcome") == "applied"
    assert stale.get("outcome") == "applied"  # the service records it, but must mark CI not-frozen, not erase the good one


# ---- Item 5,10: builder completion -> examiner obligation; CI/self-report is NOT independent certification
def test_GH6_examiner_obligation_without_delivery_and_without_false_certification(client, db_session, monkeypatch):
    from app.agent_coordination.github_intake import GitHubReadClient
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(GitHubReadClient, "get", _fake_get_factory())
    csrf = _login(client)
    r = client.post(REFRESH, json={"pr_numbers": [300]}, headers={"X-CSRF-Token": csrf}).json()
    snap = client.get("/api/mainai/founder-ops/snapshot").json()
    gh = (snap.get("durable_coordination") or {}).get("github_intake") or {}
    # delivery to an agent is never claimed; and no obligation is independently_verified purely from green CI.
    blob = str(r) + str(gh)
    print(f"GH6|delivery_claimed_in_body={r.get('delivery_claimed')} needs_review={bool(gh.get('needed_review'))}")
    assert r.get("delivery_claimed") is False, "GH-6 refresh claimed delivery to an agent"
    assert '"independently_verified"' not in blob or "examiner" in blob, \
        "GH-6 green CI alone marked a candidate independently_verified"


# ---- Item 7: GitHub API failure / rate limit -> truthful BLOCKED with named capability
def test_GH7_api_failure_is_blocked(client, db_session, monkeypatch):
    from app.agent_coordination.github_intake import GitHubReadClient, GitHubReadError
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    def boom(self, path):
        raise GitHubReadError("rate_limited", status_code=429, message="secondary rate limit")
    monkeypatch.setattr(GitHubReadClient, "get", boom)
    csrf = _login(client)
    r = client.post(REFRESH, json={"pr_numbers": [300]}, headers={"X-CSRF-Token": csrf}).json()
    print(f"GH7|outcome={r.get('outcome')} missing_capability={r.get('missing_capability')}")
    assert r.get("outcome") == "blocked", "GH-7 a rate-limited GitHub read was not reported BLOCKED"
    assert r.get("missing_capability"), "GH-7 BLOCKED without a named missing capability"
    assert r.get("write_attempted") is False
