"""Founder-workflow P3: remaining independently confirmed #295 defects.

R1/P1 trusted peer identity, D4 fail-closed health/STOP, D5 BLOCKED-with-blockers,
N1 questions are not claims, N4 historical dispatch is not current acknowledgment.
Preserves R2–R6, FO-6/FO-7, Founder-only auth, and the frozen security module.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.bootstrap import bootstrap_founder_user
from app.founder import FOUNDER_USER_ID
from app.limiter import rate_limit_key, trusted_client_ip
from app.mainai_founder_ops.communication import contradiction_notices
from app.mainai_founder_ops.snapshot import build_founder_ops_snapshot
from app.mainai_founder_ops.status import classify_mainai_task
from app.mainai_startup_readiness.receipts import ReadinessLevel, StartupReadinessReport
from app.models.mainai_execution import MainAITaskEventType, MainAITaskStatus
from app.models.mainai_job import MainAIJobStatus
from app.request_context import current_user_id as current_user_id_var
from sqlalchemy import text as sa_text


def _set_rls(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


class _Req:
    def __init__(self, path: str, host: str, headers: dict | None = None, user_id=None):
        self.url = SimpleNamespace(path=path)
        self.client = SimpleNamespace(host=host)
        self.headers = headers or {}
        self.state = SimpleNamespace(user_id=user_id)


def test_px9_unknown_collapse_is_not_an_identity():
    collapsed = _Req("/api/auth/login", "127.0.0.1", {"x-mainai-client-ip": "unknown", "x-forwarded-for": "unknown"})
    assert trusted_client_ip(collapsed) == "127.0.0.1"
    assert rate_limit_key(collapsed) == "ip:127.0.0.1:auth.login"


def test_px10_px11_proxy_peers_stay_split():
    a = _Req("/api/auth/login", "127.0.0.1", {"x-mainai-client-ip": "203.0.113.10"})
    b = _Req("/api/auth/login", "127.0.0.1", {"x-mainai-client-ip": "203.0.113.20"})
    assert trusted_client_ip(a) != trusted_client_ip(b)
    assert rate_limit_key(a) != rate_limit_key(b)
    user = uuid.uuid4()
    chat = _Req("/api/chat", "127.0.0.1", user_id=user)
    ops = _Req("/api/mainai/founder-ops/snapshot", "127.0.0.1", user_id=user)
    assert rate_limit_key(chat) != rate_limit_key(ops)


def test_d4_missing_health_is_not_reachable(db_session):
    bootstrap_founder_user(db_session)
    _set_rls(db_session, FOUNDER_USER_ID)
    snap = build_founder_ops_snapshot(db_session, owner_id=FOUNDER_USER_ID, health={"status": "unknown"})
    assert snap["health"]["backend_reachable"] is False
    assert snap["health"]["evidence_available"] is False
    snap_ok = build_founder_ops_snapshot(
        db_session, owner_id=FOUNDER_USER_ID, health={"status": "ok", "worker": "unknown"}
    )
    assert snap_ok["health"]["backend_reachable"] is True
    assert snap_ok["health"]["evidence_available"] is True


def test_d5_blocked_without_blockers_is_named(db_session, monkeypatch):
    bootstrap_founder_user(db_session)
    _set_rls(db_session, FOUNDER_USER_ID)

    empty = StartupReadinessReport(level=ReadinessLevel.BLOCKED, checks=(), blocking=())
    assert empty.as_dict()["blocking"] == ["blocked_without_named_cause"]
    assert empty.as_dict()["level"] == "BLOCKED"

    def _empty(**kwargs):
        return empty

    monkeypatch.setattr("app.mainai_founder_ops.snapshot.evaluate_startup_readiness", _empty)
    snap = build_founder_ops_snapshot(db_session, owner_id=FOUNDER_USER_ID, health={"status": "ok"})
    assert snap["startup_readiness"]["level"] == "BLOCKED"
    assert snap["startup_readiness"]["blocking"]
    assert snap["blocker_counts"]["startup_readiness"] >= 1


def test_n1_questions_do_not_fabricate_disagreement():
    assert contradiction_notices(founder_message="Vad är first-cycle GO?", evidence={"first_cycle_go": False}) == []
    assert contradiction_notices(
        founder_message="First-cycle GO är true",
        evidence={"first_cycle_go": False},
    )


def test_n4_historical_dispatch_is_not_current_ack():
    event = SimpleNamespace(event_type=MainAITaskEventType.dispatched)
    task = SimpleNamespace(
        id=uuid.uuid4(),
        goal_id=uuid.uuid4(),
        status=MainAITaskStatus.ready,
        blocker_reason=None,
        mainai_job_id=None,
        description="stale dispatch",
        approval_required=False,
    )
    stale = classify_mainai_task(task, events=[event])
    assert stale["founder_status"] == "queued"
    assert stale["dispatched"] is True
    assert stale["current_acknowledgment"] is False

    job = SimpleNamespace(
        id=uuid.uuid4(),
        status=MainAIJobStatus.queued,
        locked_by="worker-1",
        job_type="task_execution",
        public_message=None,
        error_category=None,
    )
    live = classify_mainai_task(
        SimpleNamespace(**{**task.__dict__, "mainai_job_id": job.id, "description": "live"}),
        job,
        events=[event],
    )
    assert live["founder_status"] == "acknowledged"
    assert live["current_acknowledgment"] is True
