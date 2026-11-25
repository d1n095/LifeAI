"""V5 fault-injection and self-healing reliability tests.

These tests exercise the real database/Redis fixtures and the production modules. They are
deliberately adversarial: expired leases, exhausted retries, duplicate claims, Redis loss,
provider circuit state and an interrupted stream must all leave durable, truthful state.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import redis
from sqlalchemy import select

from app.incidents.health import health_summary, sweep_runtime_health
from app.incidents.service import record_incident
from app.jobs.heartbeat import record_worker_heartbeat, worker_process_alive
from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.models.conversation import Conversation, Message as MessageModel, MessageRole, MessageStatus
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.runtime_incident import RuntimeIncident
from app.models.user import User
from app.providers import circuit_breaker, registry
from app.providers.base import Message, ProviderError
from app.routers.chat import _mark_stream_interrupted

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _login(client) -> str:
    response = client.post(
        "/api/auth/login",
        json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD},
    )
    assert response.status_code == 200
    return response.json()["csrf_token"]


def _job(*, owner_id, job_type: str, **overrides) -> MainAIJob:
    values = {
        "owner_id": owner_id,
        "job_type": job_type,
        "status": MainAIJobStatus.running,
        "created_by": "v5-test",
        "locked_by": "dead-worker",
        "started_at": datetime.utcnow() - timedelta(seconds=10),
        "last_heartbeat_at": datetime.utcnow() - timedelta(seconds=10),
        "lease_expires_at": datetime.utcnow() - timedelta(seconds=5),
        "retry_count": 0,
        "max_retries": 3,
    }
    values.update(overrides)
    return MainAIJob(**values)


def test_expired_internal_job_is_requeued_and_incidented(superuser_db, make_verified_user):
    user, _ = make_verified_user()
    job = _job(owner_id=user.id, job_type="corpus_review")
    superuser_db.add(job)
    superuser_db.commit()

    result = sweep_runtime_health(superuser_db, worker_id="v5-worker")
    superuser_db.commit()
    superuser_db.refresh(job)

    assert result.requeued == 1
    assert job.status == MainAIJobStatus.queued
    assert job.retry_count == 1
    assert job.locked_by is None
    assert job.lease_expires_at is None

    incident = superuser_db.execute(
        select(RuntimeIncident).where(
            RuntimeIncident.owner_id == user.id,
            RuntimeIncident.error_code == "job_lease_expired",
        )
    ).scalar_one()
    assert incident.severity == "medium"
    assert incident.details["job_id"] == str(job.id)
    assert incident.analysis["automatic_action"] == "none"


def test_expired_task_execution_is_never_blindly_requeued(superuser_db, make_verified_user):
    user, _ = make_verified_user()
    job = _job(owner_id=user.id, job_type="task_execution")
    superuser_db.add(job)
    superuser_db.commit()

    sweep_runtime_health(superuser_db, worker_id="v5-worker")
    superuser_db.commit()
    superuser_db.refresh(job)

    assert job.status == MainAIJobStatus.running
    incident = superuser_db.execute(
        select(RuntimeIncident).where(
            RuntimeIncident.owner_id == user.id,
            RuntimeIncident.error_code == "job_lease_expired_task_execution",
        )
    ).scalar_one()
    assert incident.details["external_effect_state"] == "unknown"


def test_repeated_sweep_does_not_inflate_the_same_incident(superuser_db, make_verified_user):
    user, _ = make_verified_user()
    job = _job(owner_id=user.id, job_type="task_execution")
    superuser_db.add(job)
    superuser_db.commit()

    sweep_runtime_health(superuser_db, worker_id="v5-worker")
    superuser_db.commit()
    sweep_runtime_health(superuser_db, worker_id="v5-worker")
    superuser_db.commit()

    incident = superuser_db.execute(
        select(RuntimeIncident).where(
            RuntimeIncident.owner_id == user.id,
            RuntimeIncident.error_code == "job_lease_expired_task_execution",
        )
    ).scalar_one()
    assert incident.occurrence_count == 1


def test_exhausted_internal_job_is_failed_not_retried_forever(superuser_db, make_verified_user):
    user, _ = make_verified_user()
    job = _job(owner_id=user.id, job_type="corpus_review", retry_count=3, max_retries=3)
    superuser_db.add(job)
    superuser_db.commit()

    result = sweep_runtime_health(superuser_db, worker_id="v5-worker")
    superuser_db.commit()
    superuser_db.refresh(job)

    assert result.failed_exhausted == 1
    assert job.status == MainAIJobStatus.failed
    assert job.error_category == "timeout"
    assert job.completed_at is not None
    incident = superuser_db.execute(
        select(RuntimeIncident).where(
            RuntimeIncident.owner_id == user.id,
            RuntimeIncident.error_code == "job_retry_exhausted",
        )
    ).scalar_one()
    assert incident.severity == "high"


def test_claim_never_reclaims_an_exhausted_expired_job(superuser_db, make_verified_user):
    user, _ = make_verified_user()
    job = _job(owner_id=user.id, job_type="corpus_review", retry_count=3, max_retries=3)
    superuser_db.add(job)
    superuser_db.commit()

    assert claim_next_mainai_job(superuser_db, "v5-worker", 60) is None
    superuser_db.refresh(job)
    assert job.status == MainAIJobStatus.running


def test_expired_import_lease_is_visible_but_left_for_the_existing_reclaimer(superuser_db, make_verified_user):
    user, _ = make_verified_user()
    job = ImportJob(
        owner_id=user.id,
        status=ImportJobStatus.running,
        locked_by="dead-worker",
        lease_expires_at=datetime.utcnow() - timedelta(seconds=5),
    )
    superuser_db.add(job)
    superuser_db.commit()

    sweep_runtime_health(superuser_db, worker_id="v5-worker")
    superuser_db.commit()
    superuser_db.refresh(job)

    assert job.status == ImportJobStatus.running
    incident = superuser_db.execute(
        select(RuntimeIncident).where(
            RuntimeIncident.owner_id == user.id,
            RuntimeIncident.error_code == "import_lease_expired",
        )
    ).scalar_one()
    assert incident.component == "worker.import_job"


def test_health_api_is_owner_scoped_and_reports_blocked(client, superuser_db, make_verified_user):
    founder = superuser_db.query(User).filter_by(email=FOUNDER_EMAIL).one()
    other, _ = make_verified_user()
    record_incident(
        superuser_db,
        owner_id=founder.id,
        error_code="provider_timeout",
        component="chat.provider",
        severity="critical",
        summary="Kritisk provider-incident",
    )
    record_incident(
        superuser_db,
        owner_id=other.id,
        error_code="other_owner_incident",
        component="other.component",
        severity="critical",
        summary="Ska inte synas för founder",
    )
    superuser_db.commit()

    headers = {"X-CSRF-Token": _login(client)}
    response = client.get("/api/incidents/health", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "blocked"
    assert body["critical_open"] == 1
    assert [item["component"] for item in body["components"]] == ["chat.provider"]


def test_redis_loss_degrades_to_unknown_without_raising(monkeypatch):
    import app.jobs.heartbeat as heartbeat

    def _broken_client():
        raise redis.ConnectionError("simulated redis loss")

    monkeypatch.setattr(heartbeat, "_client", _broken_client)
    record_worker_heartbeat("v5-worker", ttl_seconds=10)  # must not raise
    assert worker_process_alive() is None


def test_circuit_breaker_opens_after_repeated_failures_and_resets_on_success():
    provider, model = "openai", "v5-circuit-test"
    for _ in range(circuit_breaker.FAILURE_THRESHOLD):
        circuit_breaker.record_failure(provider, model)
    assert circuit_breaker.is_open(provider, model) is True

    circuit_breaker.record_success(provider, model)
    assert circuit_breaker.is_open(provider, model) is False


@pytest.mark.asyncio
async def test_registry_skips_an_open_provider_without_invoking_it(db_session, monkeypatch):
    calls: list[str] = []

    class _FakeProvider:
        name = "fake-local"
        is_local = True

        def is_configured(self) -> bool:
            return True

        async def chat(self, messages, model, **kwargs):
            calls.append(model)
            raise AssertionError("an open circuit must never invoke the provider")

    monkeypatch.setattr(
        registry,
        "resolve_chat_chain",
        lambda db, selection_policy=None: [(_FakeProvider(), "fake-model")],
    )
    monkeypatch.setattr(registry.circuit_breaker, "is_open", lambda provider, model: True)

    with pytest.raises(ProviderError) as exc:
        await registry.chat_with_fallback(
            db_session,
            [Message(role="user", content="hej")],
        )
    assert exc.value.category == "circuit_open"
    assert calls == []


def test_duplicate_job_delivery_claims_exactly_once(superuser_db, make_verified_user):
    user, _ = make_verified_user()
    job = MainAIJob(
        owner_id=user.id,
        job_type="corpus_review",
        status=MainAIJobStatus.queued,
        created_by="v5-test",
    )
    superuser_db.add(job)
    superuser_db.commit()

    first = claim_next_mainai_job(superuser_db, "worker-a", 60)
    second = claim_next_mainai_job(superuser_db, "worker-b", 60)
    assert first is not None
    assert second is None


def test_interrupted_stream_after_provider_call_is_unresolved_and_incidented(
    superuser_db, make_verified_user
):
    user, _ = make_verified_user()
    conversation = Conversation(title="v5 stream", user_id=user.id)
    superuser_db.add(conversation)
    superuser_db.flush()
    user_message = MessageModel(
        conversation_id=conversation.id,
        role=MessageRole.user,
        content="streama",
    )
    superuser_db.add(user_message)
    superuser_db.flush()
    reply = MessageModel(
        conversation_id=conversation.id,
        role=MessageRole.assistant,
        content="",
        in_reply_to_id=user_message.id,
        status=MessageStatus.failed,
        error_category="inflight:test",
        provider="openai",
        model="gpt-test",
    )
    superuser_db.add(reply)
    superuser_db.commit()

    _mark_stream_interrupted(
        superuser_db,
        user_message_id=user_message.id,
        owner_id=user.id,
    )
    superuser_db.refresh(reply)

    assert reply.status == MessageStatus.failed
    assert reply.error_category == "external_effect_unresolved"
    incident = superuser_db.execute(
        select(RuntimeIncident).where(
            RuntimeIncident.owner_id == user.id,
            RuntimeIncident.error_code == "chat_stream_interrupted",
        )
    ).scalar_one()
    assert incident.severity == "high"
    assert incident.details["external_effect_state"] == "unknown"


def test_health_summary_recovers_when_only_recent_resolved_incidents_remain(
    superuser_db, make_verified_user
):
    user, _ = make_verified_user()
    row = record_incident(
        superuser_db,
        owner_id=user.id,
        error_code="provider_timeout",
        component="chat.provider",
        severity="medium",
        summary="Tillfälligt providerfel",
    )
    superuser_db.commit()
    row.status = "resolved"
    row.resolved_at = datetime.utcnow()
    superuser_db.commit()

    summary = health_summary(superuser_db, owner_id=user.id)
    assert summary["state"] == "recovered"
    assert summary["recovered_incidents"] == 1
    assert summary["open_incidents"] == 0
