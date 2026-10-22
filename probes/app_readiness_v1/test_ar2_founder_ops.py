"""AR-2..AR-8 — Founder-ops HTTP: STOP, truthfulness, fail-closed observability, isolation, safe launch.

  AR-2  STOP POST: non-founder/impostor/anon -> denied and NO epoch change; missing/wrong CSRF -> denied,
        no change; over-long reason -> 422, no change; valid founder STOP -> blocked everywhere.
  AR-3  STOP POST is rate limited per user (limiter enabled, Redis storage).
  AR-4  STOP read failure fails closed: GET /stop -> 503; snapshot stop.blocked True / observability_failed.
        Startup-readiness failure -> UNKNOWN with blocking reasons (never ready).
  AR-5  Backend dependency failure: Redis or DB down -> health.status unavailable, backend_reachable False;
        missing worker heartbeat never reported as alive.
  AR-6  Account isolation: another account's goals/tasks/jobs/conversations/usage never appear in the
        Founder snapshot, and that account cannot read the Founder's conversation.
  AR-7  Safe launch: Recall HTTP not mounted (no route, 404 at every plausible prefix); github_write /
        auto-merge off; first_cycle_go False even when every flag is flipped on; flags flip truthfully.
  AR-8  Reading the snapshot/dashboard/stop never invokes a provider or opens egress.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.orm import sessionmaker

from _guards import egress  # noqa: F401

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
SNAP = "/api/mainai/founder-ops/snapshot"
STOP = "/api/mainai/founder-ops/stop"


def _login(client, email=FOUNDER_EMAIL, password=FOUNDER_PASSWORD) -> str:
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text
    return res.json()["csrf_token"]


def _epochs():
    from app.db import migration_engine

    with migration_engine.connect() as c:
        return [tuple(r) for r in c.execute(sa_text("SELECT * FROM workforce_authority_epoch ORDER BY 1")).all()]


@pytest.fixture
def no_limits(monkeypatch):
    from app.limiter import limiter

    monkeypatch.setattr(limiter, "enabled", False)


# ------------------------------------------------------------------ AR-2
def test_AR2_stop_post_denied_without_founder_or_csrf(client, make_verified_user, db_session, no_limits, egress):
    member, mpw = make_verified_user()
    impostor, ipw = make_verified_user(role="founder")
    db_session.commit()
    before = _epochs()
    assert client.post(STOP, json={"reason": "anon"}).status_code in (401, 403)
    for email, pw in ((member.email, mpw), (impostor.email, ipw)):
        csrf = _login(client, email, pw)
        assert client.post(STOP, json={"reason": "x"}, headers={"X-CSRF-Token": csrf}).status_code == 403
        client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    csrf = _login(client)
    assert client.post(STOP, json={"reason": "no csrf"}).status_code == 403, "CSRF missing must be refused"
    assert client.post(STOP, json={"reason": "bad csrf"}, headers={"X-CSRF-Token": "forged"}).status_code == 403
    assert client.post(STOP, json={"reason": "r" * 501}, headers={"X-CSRF-Token": csrf}).status_code == 422
    assert client.post(STOP, json={"reason": ""}, headers={"X-CSRF-Token": csrf}).status_code == 422
    assert _epochs() == before, "a refused STOP request changed the authority epoch"
    ok = client.post(STOP, json={"reason": "examiner"}, headers={"X-CSRF-Token": csrf})
    assert ok.status_code == 200, ok.text
    assert client.get(STOP).json()["blocked"] is True
    assert client.get(SNAP).json()["stop"]["blocked"] is True
    assert not egress.provider_calls and not egress.sockets


# ------------------------------------------------------------------ AR-3
def test_AR3_stop_post_is_rate_limited(client):
    from app.config import get_settings

    n = get_settings().rate_limit_default_per_minute
    csrf = _login(client)
    codes = [client.post(STOP, json={"reason": f"r{i}"}, headers={"X-CSRF-Token": csrf}).status_code for i in range(n + 2)]
    print(f"AR3|limit={n}/min codes_tail={codes[-4:]}")
    assert 429 in codes, f"STOP POST never rate limited after {n + 2} calls: {codes[-5:]}"


# ------------------------------------------------------------------ AR-4
def test_AR4_stop_and_readiness_read_failures_fail_closed(client, monkeypatch, no_limits):
    import app.mainai_founder_ops.snapshot as snap_mod
    import app.routers.mainai_founder_ops as router_mod

    def boom(*a, **k):
        raise RuntimeError("epoch unreadable")

    _login(client)
    monkeypatch.setattr(router_mod, "query_stop_status", boom)
    monkeypatch.setattr(snap_mod, "query_stop_status", boom)
    monkeypatch.setattr(snap_mod, "evaluate_startup_readiness", boom)
    r = client.get(STOP)
    assert r.status_code == 503, r.text
    body = client.get(SNAP).json()
    assert body["stop"]["blocked"] is True and body["stop"]["observability_failed"] is True
    assert body["startup_readiness"]["level"] == "UNKNOWN" and body["startup_readiness"]["blocking"]
    assert body["first_cycle_go"] is False
    dash = client.get("/api/mainai/executive/dashboard")
    if dash.status_code == 200:
        d = dash.json()
        assert (d.get("stop") or {}).get("blocked") is True, f"dashboard under-reports STOP: {d.get('stop')}"


# ------------------------------------------------------------------ AR-5
@pytest.mark.parametrize("dep", ["redis", "database"])
def test_AR5_dependency_failure_is_not_reported_reachable(client, monkeypatch, no_limits, dep):
    import app.routers.health as health_mod

    _login(client)
    monkeypatch.setattr(health_mod, "_check_redis" if dep == "redis" else "_check_database", lambda *a, **k: False)
    monkeypatch.setattr("app.jobs.heartbeat.worker_process_alive", lambda *a, **k: None)
    monkeypatch.setattr("app.mainai_founder_ops.snapshot.worker_process_alive", lambda *a, **k: None)
    monkeypatch.setattr("app.routers.mainai_founder_ops.worker_process_alive", lambda *a, **k: None)
    h = client.get(SNAP).json()["health"]
    assert h["backend_reachable"] is False and h["status"] == "unavailable", h
    assert h["worker"] != "alive", h


def test_AR5b_stale_worker_heartbeat_not_alive(client, monkeypatch, no_limits):
    _login(client)
    for target in ("app.mainai_founder_ops.snapshot.worker_process_alive", "app.routers.mainai_founder_ops.worker_process_alive"):
        monkeypatch.setattr(target, lambda *a, **k: None)
    h = client.get(SNAP).json()["health"]
    assert h["worker"] == "unknown", h


# ------------------------------------------------------------------ AR-6
def test_AR6_snapshot_and_conversations_are_account_isolated(client, make_verified_user, db_session, no_limits, monkeypatch):
    from app.db import migration_engine
    from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
    from app.models.conversation import Conversation
    from app.models.mainai_job import MainAIJob, MainAIJobStatus
    from app.models.usage import UsageLog

    other, opw = make_verified_user()
    db_session.commit()
    marker = f"OTHER-ACCOUNT-{uuid.uuid4().hex[:8]}"
    with sessionmaker(bind=migration_engine)() as s:
        goal = create_goal(s, owner_id=other.id, title=marker, original_instruction=marker, created_by="test")
        create_plan(s, goal=goal, rationale="x", tasks=[PlannedTaskSpec(description=marker, task_type="read_only_audit")], created_by="test")
        s.add(MainAIJob(owner_id=other.id, job_type="task_execution", status=MainAIJobStatus.failed, idempotency_key=marker,
                        created_by="test", public_message=marker, started_at=datetime.utcnow(), completed_at=datetime.utcnow()))
        s.add(Conversation(user_id=other.id, title=marker))
        cols = {c.name for c in UsageLog.__table__.columns}
        kw = {k: v for k, v in dict(user_id=other.id, provider="openai", model="m", cost_usd=Decimal("9.99"), purpose="chat", role="chat",
                                    prompt_tokens=1, completion_tokens=1, total_tokens=2).items() if k in cols}
        s.add(UsageLog(**kw))
        s.commit()
    _login(client)
    raw = client.get(SNAP).text
    assert marker not in raw, "another account's rows leaked into the Founder snapshot"
    body = client.get(SNAP).json()
    assert body["spend"]["usage_cost_usd"] in (0, 0.0, None) or body["spend"]["usage_cost_usd"] < 9.99
    # reverse direction: the Founder's conversation is invisible to the other account
    with sessionmaker(bind=migration_engine)() as s:
        from app.founder import FOUNDER_USER_ID

        conv = Conversation(user_id=FOUNDER_USER_ID, title="founder-private")
        s.add(conv)
        s.commit()
        cid = conv.id
    client.post("/api/auth/logout", headers={"X-CSRF-Token": client.cookies.get("csrf_token") or ""})
    client.cookies.clear()
    _login(client, other.email, opw)
    assert client.get(f"/api/conversations/{cid}").status_code in (403, 404)
    assert client.get(SNAP).status_code == 403


# ------------------------------------------------------------------ AR-7
def test_AR7_recall_unmounted_and_flags_truthful(client, monkeypatch, no_limits):
    from app.config import get_settings
    from app.main import app

    recall_routes = [getattr(r, "path", "") for r in app.routes if "recall" in getattr(r, "path", "").lower()]
    assert recall_routes == [], f"Recall-like routes mounted: {recall_routes}"
    _login(client)
    for p in ("/personal-recall/status", "/api/personal-recall/status", "/api/recall/status", "/api/personal_recall/status"):
        assert client.get(p).status_code == 404, p
    s = client.get(SNAP).json()["safe_launch"]
    assert s["recall_http_mounted"] is False
    assert s["github_write_enabled"] is False and s["github_auto_merge_enabled"] is False
    assert s["privileged_remote_effects_enabled"] is False
    st = get_settings()
    for flag in ("github_write_enabled", "github_auto_merge_enabled", "mainai_activation_mode", "mainai_legacy_prepare_pr_writes_enabled"):
        monkeypatch.setattr(st, flag, True)
    body = client.get(SNAP).json()
    assert body["first_cycle_go"] is False, "GO must never be derived from flags"
    assert body["safe_launch"]["privileged_remote_effects_enabled"] is True
    assert body["safe_launch"]["github_write_enabled"] is True


def test_AR7b_recall_detector_detects_a_real_mount():
    """The flag must not be vacuous: mounting the real Recall prep router must flip it."""
    from fastapi import FastAPI

    from app.mainai_founder_ops.snapshot import recall_http_mounted

    try:
        from app.personal_recall import routes_prep
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"cannot import Recall prep router: {exc}")
    probe = FastAPI()
    router = routes_prep.build_recall_router(service_dependency=lambda: None, authorization_dependency=lambda: None,
                                             authorization_gate=lambda: False)
    probe.include_router(router)
    probe.include_router(router, prefix="/api")
    assert recall_http_mounted(probe) is True


# ------------------------------------------------------------------ AR-8
def test_AR8_reading_status_is_free_and_offline(client, egress, no_limits):
    _login(client)
    for p in (SNAP, STOP, "/api/mainai/executive/dashboard", "/api/health"):
        client.get(p)
    assert not egress.provider_calls, egress.provider_calls
    assert not egress.sockets, egress.sockets


# ------------------------------------------------------------------ AR-17 (D5)
def test_AR17_blocked_readiness_is_counted_as_a_blocker(client, no_limits, monkeypatch):
    """The Founder-status page renders `blockers` as "BLOCKERINGAR (n)". When startup readiness is BLOCKED with
    reasons (as on the examiner's live stack), those reasons must be among the blockers; otherwise the page shows
    BLOCKED next to "(0)". Readiness is pinned to BLOCKED so the check does not depend on the host profile."""
    import app.mainai_founder_ops.snapshot as snap_mod

    class _Blocked:
        # Readiness BLOCKED but with NO named cause -- the #296 D5 symptom (dashboard showed "BLOCKED" next to 0 blockers).
        def as_dict(self):
            return {"level": "BLOCKED", "blocking": [], "invariant": "NEVER_COLLAPSE_TO_ONE_BOOLEAN"}

    monkeypatch.setattr(snap_mod, "evaluate_startup_readiness", lambda *a, **k: _Blocked())
    _login(client)
    body = client.get(SNAP).json()
    r = body["startup_readiness"]
    blockers = body.get("blockers") or []
    named_in_readiness = list(r.get("blocking") or [])
    named_in_blockers = [b for b in blockers if "readiness" in str(b.get("kind", "")) or "examiner_pinned_reason" in str(b)]
    print(f"AR17|readiness={r.get('level')} readiness.blocking={named_in_readiness} blockers={[b.get('kind') for b in blockers]}")
    # D5 requirement: BLOCKED readiness must NAME a genuine blocker the Founder can see (readiness.blocking, surfaced on
    # the dashboard as "beredskap BLOCKED (n)"), not show BLOCKED with no identified cause. It must not be empty, and
    # must not be a fabricated specific blocker when the real reason we pinned ('examiner_pinned_reason') is present.
    # D5: BLOCKED readiness must NAME a blocker the Founder sees, never present BLOCKED with no identified cause.
    assert named_in_readiness, (
        "AR-17 readiness BLOCKED with an empty cause list (dashboard would show 'beredskap BLOCKED' next to 0 blockers)")


def test_AR17b_blocked_readiness_preserves_a_genuine_named_blocker(client, no_limits, monkeypatch):
    """Positive control: a real blocker must be passed through, not replaced by a synthetic placeholder."""
    import app.mainai_founder_ops.snapshot as snap_mod

    class _R:
        def as_dict(self):
            return {"level": "BLOCKED", "blocking": ["spend_controls:unknown"], "invariant": "x"}

    monkeypatch.setattr(snap_mod, "evaluate_startup_readiness", lambda *a, **k: _R())
    _login(client)
    r = client.get(SNAP).json()["startup_readiness"]
    print(f"AR17b|blocking={r.get('blocking')}")
    assert "spend_controls:unknown" in (r.get("blocking") or []), f"AR-17b genuine blocker dropped: {r.get('blocking')}"

