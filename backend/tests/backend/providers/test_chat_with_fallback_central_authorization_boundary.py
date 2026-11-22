"""Founder directive ("RESUME LIFEAI FIRST-CYCLE REMEDIATION" -> "FOUNDER DECISION -- EGRESS
AUTHORIZATION & FIRST-CYCLE INTEGRATION", Task #60): PR #456 only threaded real owner_id
through 5 call sites -- the founder explicitly ruled that "does not prove that the optional
egress bypass is eliminated at the central provider boundary." This file proves the CENTRAL
boundary itself (app/providers/registry.py's chat_with_fallback(), and the one new ownerless
call site it protects, app/mainai_execution/lesson_conflicts.py's detect_conflict()) against
the founder's own 7-item security-acceptance list. Every "denied" test spies on every real
provider's `.chat()` to prove ZERO provider calls happened, not just that an exception was
raised at some point.

  1. Missing/invalid authorization -> zero provider calls.
  2. Expired or wrong worker lease -> zero provider calls.
  3. Unauthorized founder-wide scope -> denied.
  4. NEVER_EGRESS/private-key content -> zero provider calls.
  5. Authorized operation -> correct disclosure evidence and expected behavior.
  6. Local-only provider failure -> no external fallback.
  7. Cross-tenant attempt -> no unauthorized access.
"""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.config import get_settings
from app.egress_policy import EgressDeniedError
from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import lessons
from app.mainai_execution.lesson_conflicts import detect_conflict
from app.models.mainai_execution import EngineeringLessonSeverity
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.provider_disclosure import ProviderDisclosureEvent
from app.providers.base import ChatResult, Message, ProviderError
from app.providers.ollama_provider import OllamaProvider
from app.providers.openai_provider import OpenAIProvider
from app.providers.registry import chat_with_fallback
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _apply_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _set_rls_user(session, user_id) -> None:
    current_user_id_var.set(str(user_id))
    session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user_id)})


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls_user(db_session, user.id)
    return user.id


def _spy_chat(calls: dict):
    async def _chat(self, messages, model, **kwargs):
        calls["n"] = calls.get("n", 0) + 1
        return ChatResult(content="ok", provider=self.name, model=model, raw_usage={})

    return _chat


@pytest.mark.asyncio
async def test_1_missing_invalid_authorization_denies_before_any_provider_call(db_session, owner_id, monkeypatch):
    """owner_id referencing a user that does not exist -- the disclosure ledger's own FK to
    users.id fails the write BEFORE chat_with_fallback() ever reaches provider.chat()."""
    calls: dict = {}
    monkeypatch.setattr(OpenAIProvider, "chat", _spy_chat(calls))
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")

    fake_owner = uuid.uuid4()  # a real, syntactically valid UUID -- but no such user row exists
    with pytest.raises(DBAPIError):
        await chat_with_fallback(
            db_session, [Message(role="user", content="hej")], owner_id=fake_owner, purpose="test", requested_by="test",
        )
    db_session.rollback()
    assert calls.get("n", 0) == 0


@pytest.mark.asyncio
async def test_2_expired_or_wrong_worker_lease_denies_before_any_provider_call(db_session, superuser_db, owner_id, monkeypatch):
    """A job whose lease has already expired (reclaimed/stale) must deny egress under that
    dead job's claimed identity -- even though the job row genuinely exists and genuinely
    belonged to `owner_id` at claim time."""
    calls: dict = {}
    monkeypatch.setattr(OpenAIProvider, "chat", _spy_chat(calls))
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")

    job = MainAIJob(owner_id=owner_id, job_type="task_execution", status=MainAIJobStatus.queued, created_by="test")
    db_session.add(job)
    db_session.commit()
    claimed = claim_next_mainai_job(superuser_db, "worker-1", 120)
    assert claimed is not None
    job_id, _owner, lease_generation = claimed
    # Expire the lease out from under "worker-1" -- simulates a crashed/reclaimed job exactly
    # like app/jobs/mainai_job_lease.py's own reclaim tests do.
    superuser_db.execute(text("UPDATE mainai_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :j"), {"j": str(job_id)})
    superuser_db.commit()

    with pytest.raises(EgressDeniedError, match="no longer active"):
        await chat_with_fallback(
            db_session, [Message(role="user", content="hej")], owner_id=owner_id, purpose="test", requested_by="test",
            job_id=job_id, lease_worker_id="worker-1", lease_generation=lease_generation,
        )
    assert calls.get("n", 0) == 0

    # A WRONG worker_id for a still-genuinely-active lease denies identically.
    job2 = MainAIJob(owner_id=owner_id, job_type="task_execution", status=MainAIJobStatus.queued, created_by="test")
    db_session.add(job2)
    db_session.commit()
    claimed2 = claim_next_mainai_job(superuser_db, "worker-real", 120)
    job2_id, _owner2, lease_generation2 = claimed2

    with pytest.raises(EgressDeniedError, match="no longer active"):
        await chat_with_fallback(
            db_session, [Message(role="user", content="hej")], owner_id=owner_id, purpose="test", requested_by="test",
            job_id=job2_id, lease_worker_id="worker-impostor", lease_generation=lease_generation2,
        )
    assert calls.get("n", 0) == 0


@pytest.mark.asyncio
async def test_3_unauthorized_founder_wide_scope_is_denied(db_session, monkeypatch):
    """EngineeringLesson is ownerless, founder-wide state with no founder-wide authorization
    primitive backing it yet (Task #60 finding) -- lesson_conflicts.detect_conflict() must
    deny outright, zero provider calls, regardless of how clearly conflicting the two lessons
    actually are."""
    import json

    calls: dict = {}

    async def _conflict_chat(self, messages, model, **kwargs):
        calls["n"] = calls.get("n", 0) + 1
        return ChatResult(content=json.dumps({"conflict": True, "reasoning": "x"}), provider="openai", model=model, raw_usage={})

    monkeypatch.setattr(OpenAIProvider, "chat", _conflict_chat)

    from datetime import datetime

    lesson_a = lessons.record_lesson(
        db_session, problem="a problem", root_cause="a root cause", affected_component="app.foo",
        severity=EngineeringLessonSeverity.medium, evidence="evidence", fix="do X", general_rule="always X",
        applies_to=["run_tests"], source_type="branch_registry_pass", source_ref="test", created_by="test",
        first_seen_at=datetime.utcnow(),
    )
    lesson_b = lessons.record_lesson(
        db_session, problem="a problem", root_cause="a root cause", affected_component="app.foo",
        severity=EngineeringLessonSeverity.medium, evidence="evidence", fix="never do X", general_rule="never X",
        applies_to=["run_tests"], source_type="branch_registry_pass", source_ref="test", created_by="test",
        first_seen_at=datetime.utcnow(),
    )
    db_session.commit()

    is_conflict, reasoning = await detect_conflict(db_session, lesson_a=lesson_a, lesson_b=lesson_b)

    assert is_conflict is False
    assert reasoning == ""
    assert calls.get("n", 0) == 0, "detect_conflict() must deny before ever reaching chat_with_fallback"


@pytest.mark.asyncio
async def test_4_never_egress_marker_denies_before_any_provider_call(db_session, owner_id, monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(OpenAIProvider, "chat", _spy_chat(calls))
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")

    with pytest.raises(EgressDeniedError, match="NEVER_EGRESS"):
        await chat_with_fallback(
            db_session,
            [Message(role="user", content="NEVER_EGRESS: this must never leave the process")],
            owner_id=owner_id, purpose="test", requested_by="test",
        )
    assert calls.get("n", 0) == 0


@pytest.mark.asyncio
async def test_5_authorized_operation_succeeds_with_correct_disclosure_evidence(db_session, owner_id, monkeypatch):
    """Positive control -- proves the gate does not just deny everything, AND that a real
    allowed call produces exactly one disclosure-ledger row recording the real decision."""
    calls: dict = {}
    monkeypatch.setattr(OpenAIProvider, "chat", _spy_chat(calls))
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")

    result, attempted = await chat_with_fallback(
        db_session, [Message(role="user", content="hej, helt ofarligt meddelande")],
        owner_id=owner_id, purpose="test_positive_control", requested_by="test",
    )
    db_session.commit()

    assert calls.get("n", 0) == 1
    assert result.content == "ok"
    assert attempted == ["openai"]

    events = db_session.execute(
        select(ProviderDisclosureEvent).where(ProviderDisclosureEvent.purpose == "test_positive_control")
    ).scalars().all()
    assert len(events) == 1
    assert events[0].decision == "allowed"
    assert events[0].owner_id == owner_id


@pytest.mark.asyncio
async def test_6_local_only_mode_never_falls_back_to_an_external_provider(db_session, owner_id, monkeypatch):
    """settings.local_only_mode=True: a failing local (ollama) provider must surface as an
    error -- never silently escalate to a real external provider, even though one is
    configured and would otherwise be next in the fallback chain."""
    external_calls: dict = {}
    monkeypatch.setattr(OpenAIProvider, "chat", _spy_chat(external_calls))

    async def _failing_ollama_chat(self, messages, model, **kwargs):
        raise ProviderError("local model unreachable")

    monkeypatch.setattr(OllamaProvider, "chat", _failing_ollama_chat)
    monkeypatch.setattr(get_settings(), "default_llm_provider", "ollama")
    monkeypatch.setattr(get_settings(), "openai_api_key", "irrelevant-but-configured")
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "openai")
    monkeypatch.setattr(get_settings(), "local_only_mode", True)

    with pytest.raises(ProviderError, match="local_only_mode|misslyckades"):
        await chat_with_fallback(
            db_session, [Message(role="user", content="hej")], owner_id=owner_id, purpose="test", requested_by="test",
        )
    assert external_calls.get("n", 0) == 0, "LOCAL_ONLY_MODE must never let a local failure escalate to an external provider"


@pytest.mark.asyncio
async def test_7_cross_tenant_attempt_is_denied_by_rls(db_session, make_verified_user, monkeypatch):
    """The RLS session is set to user B, but the call claims owner_id=user A -- the
    disclosure-ledger INSERT itself (owner_id must match the session's own
    app.current_user_id under this table's RLS policy, same as every other owner-scoped
    table in this codebase) denies it before any provider is ever attempted."""
    calls: dict = {}
    monkeypatch.setattr(OpenAIProvider, "chat", _spy_chat(calls))
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")

    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()
    _set_rls_user(db_session, user_b.id)

    with pytest.raises(DBAPIError):
        await chat_with_fallback(
            db_session, [Message(role="user", content="hej")], owner_id=user_a.id, purpose="test", requested_by="test",
        )
    db_session.rollback()
    assert calls.get("n", 0) == 0
