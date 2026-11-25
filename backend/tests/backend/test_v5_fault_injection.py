"""Executable V5 fault-injection matrix (10 scenarios).

Each test maps to one scenario in the V5 reliability directive. The tests use the real
PostgreSQL/Redis fixtures and production modules; process termination is represented by the
same durable state a killed process leaves behind, because a test cannot honestly prove what
happens after SIGKILL by calling the cleanup path that a real SIGKILL never reaches.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import redis
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.billing.reservation import ChatAttemptBudget
from app.incidents import health
from app.incidents.health import sweep_runtime_health
from app.jobs.heartbeat import record_worker_heartbeat, worker_process_alive
from app.jobs.mainai_job_lease import (
    JobLeaseLostError,
    claim_next_mainai_job,
    renew_mainai_job_lease,
)
from app.models.conversation import Conversation, Message as MessageModel, MessageRole, MessageStatus
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.runtime_incident import RuntimeIncident
from app.models.user import User
from app.providers import circuit_breaker, registry
from app.providers.base import ChatResult, Message, ProviderError
from app.routers.chat import _mark_stream_interrupted
from app.storage.local_fs import LocalFilesystemStorage

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _login(client) -> str:
    response = client.post(
        "/api/auth/login",
        json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def _founder(superuser_db) -> User:
    return superuser_db.query(User).filter_by(email=FOUNDER_EMAIL).one()


def _stream_turn(superuser_db, *, user: User, provider: str | None, category: str) -> tuple[MessageModel, MessageModel]:
    conversation = Conversation(title="v5 fault injection", user_id=user.id)
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
        error_category=category,
        provider=provider,
        model="gpt-test" if provider else None,
    )
    superuser_db.add(reply)
    superuser_db.commit()
    return user_message, reply


def _job(*, owner_id, job_type: str = "corpus_review", **overrides) -> MainAIJob:
    values = {
        "owner_id": owner_id,
        "job_type": job_type,
        "status": MainAIJobStatus.running,
        "created_by": "v5-fault-test",
        "locked_by": "dead-worker",
        "started_at": datetime.utcnow() - timedelta(seconds=10),
        "last_heartbeat_at": datetime.utcnow() - timedelta(seconds=10),
        "lease_expires_at": datetime.utcnow() - timedelta(seconds=5),
        "retry_count": 0,
        "max_retries": 3,
    }
    values.update(overrides)
    return MainAIJob(**values)


class _FakeProvider:
    is_local = True

    def __init__(self, name: str, behavior):
        self.name = name
        self._behavior = behavior
        self.calls = 0

    def is_configured(self) -> bool:
        return True

    async def chat(self, messages, model, **kwargs):
        self.calls += 1
        return await self._behavior(model)


def test_01_backend_termination_during_stream_blocks_blind_retry(
    client, superuser_db
):
    founder = _founder(superuser_db)
    user_message, _reply = _stream_turn(
        superuser_db, user=founder, provider="openai", category="inflight:crash-before-finally"
    )

    headers = {"X-CSRF-Token": _login(client)}
    response = client.post(f"/api/chat/messages/{user_message.id}/retry", headers=headers)
    assert response.status_code == 409, response.text
    assert "unresolved" in response.text.lower()

    replies = superuser_db.query(MessageModel).filter_by(in_reply_to_id=user_message.id).all()
    assert len(replies) == 1
    assert replies[0].status == MessageStatus.failed
    assert replies[0].error_category.startswith("inflight:")


def test_02_worker_termination_requeues_idempotent_internal_job(
    superuser_db, make_verified_user
):
    user, _ = make_verified_user()
    job = _job(owner_id=user.id, job_type="message_source_backfill")
    superuser_db.add(job)
    superuser_db.commit()

    result = sweep_runtime_health(superuser_db, worker_id="recovery-worker")
    superuser_db.commit()
    superuser_db.refresh(job)

    assert result.requeued == 1
    assert job.status == MainAIJobStatus.queued
    assert job.retry_count == 1
    incident = superuser_db.execute(
        select(RuntimeIncident).where(
            RuntimeIncident.owner_id == user.id,
            RuntimeIncident.error_code == "job_lease_expired",
        )
    ).scalar_one()
    assert incident.details["failure_class"] == "lease_expired"


def test_03_redis_loss_fails_open_for_heartbeat_and_circuit_breaker(monkeypatch):
    import app.jobs.heartbeat as heartbeat

    def _broken_client():
        raise redis.ConnectionError("simulated redis loss")

    monkeypatch.setattr(heartbeat, "_client", _broken_client)
    record_worker_heartbeat("v5-worker", ttl_seconds=10)
    assert worker_process_alive() is None

    monkeypatch.setattr(circuit_breaker, "_client", _broken_client)
    assert circuit_breaker.is_open("openai", "redis-loss-model") is False
    assert circuit_breaker.record_failure("openai", "redis-loss-model") is False


def test_04_postgresql_outage_degrades_sweep_and_recovers(
    superuser_db, make_verified_user, monkeypatch
):
    user, _ = make_verified_user()
    real_scan = health._scan_mainai_jobs
    attempts = {"count": 0}

    def _flaky_scan(db, *, now, result):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise OperationalError("SELECT 1", {}, Exception("simulated postgres outage"))
        return real_scan(db, now=now, result=result)

    monkeypatch.setattr(health, "_scan_mainai_jobs", _flaky_scan)
    first = sweep_runtime_health(superuser_db, worker_id="recovery-worker")
    assert first.errors == ["OperationalError"]
    assert first.requeued == 0

    job = _job(owner_id=user.id, job_type="corpus_review")
    superuser_db.add(job)
    superuser_db.commit()
    second = sweep_runtime_health(superuser_db, worker_id="recovery-worker")
    superuser_db.commit()
    superuser_db.refresh(job)

    assert second.errors == []
    assert second.requeued == 1
    assert job.status == MainAIJobStatus.queued


@pytest.mark.asyncio
async def test_05_provider_timeout_falls_back_but_malformed_response_fails_closed(
    db_session, monkeypatch
):
    async def _timeout(model):
        raise ProviderError("provider timeout", category="timeout", provider_request_may_have_left=False)

    async def _ok(model):
        return ChatResult(
            content="fallback reply",
            provider="backup",
            model=model,
            raw_usage={"prompt_tokens": 2, "completion_tokens": 2},
        )

    first = _FakeProvider("primary", _timeout)
    backup = _FakeProvider("backup", _ok)
    monkeypatch.setattr(
        registry,
        "resolve_chat_chain",
        lambda db, selection_policy=None: [(first, "primary-model"), (backup, "backup-model")],
    )

    result, attempted = await registry.chat_with_fallback(
        db_session,
        [Message(role="user", content="hej")],
        effect_guard=True,
    )
    assert result.content == "fallback reply"
    assert attempted == ["primary", "backup"]
    assert first.calls == 1
    assert backup.calls == 1

    async def _malformed(model):
        return ChatResult(content="", provider="malformed", model=model, raw_usage={"prompt_tokens": 1, "completion_tokens": 0})

    malformed = _FakeProvider("malformed", _malformed)
    monkeypatch.setattr(
        registry,
        "resolve_chat_chain",
        lambda db, selection_policy=None: [(malformed, "malformed-model")],
    )
    with pytest.raises(ProviderError) as exc:
        await registry.chat_with_fallback(
            db_session,
            [Message(role="user", content="hej")],
            effect_guard=True,
        )
    assert exc.value.category == "external_effect_unknown"
    assert exc.value.provider_request_may_have_left is True


def test_06_browser_disconnect_marks_effect_unresolved_and_blocks_retry(
    client, superuser_db
):
    founder = _founder(superuser_db)
    user_message, reply = _stream_turn(
        superuser_db, user=founder, provider="openai", category="inflight:disconnect"
    )

    _mark_stream_interrupted(superuser_db, user_message_id=user_message.id, owner_id=founder.id)
    superuser_db.refresh(reply)
    assert reply.status == MessageStatus.failed
    assert reply.error_category == "external_effect_unresolved"

    headers = {"X-CSRF-Token": _login(client)}
    response = client.post(f"/api/chat/messages/{user_message.id}/retry", headers=headers)
    assert response.status_code == 409, response.text
    assert superuser_db.query(MessageModel).filter_by(in_reply_to_id=user_message.id).count() == 1


def test_07_duplicate_job_delivery_claims_exactly_once(superuser_db, make_verified_user):
    user, _ = make_verified_user()
    job = MainAIJob(
        owner_id=user.id,
        job_type="corpus_review",
        status=MainAIJobStatus.queued,
        created_by="v5-fault-test",
    )
    superuser_db.add(job)
    superuser_db.commit()

    first = claim_next_mainai_job(superuser_db, "worker-a", 60)
    second = claim_next_mainai_job(superuser_db, "worker-b", 60)
    assert first is not None
    assert second is None
    superuser_db.refresh(job)
    assert job.locked_by == "worker-a"
    assert job.lease_generation == 1


@pytest.mark.asyncio
async def test_08_repeated_provider_errors_open_circuit_and_stop_invocation(
    db_session, monkeypatch
):
    calls: list[str] = []

    async def _must_not_run(model):
        calls.append(model)
        return ChatResult(
            content="should not run",
            provider="circuit",
            model=model,
            raw_usage={"prompt_tokens": 1, "completion_tokens": 1},
        )

    provider = _FakeProvider("circuit", _must_not_run)
    monkeypatch.setattr(
        registry,
        "resolve_chat_chain",
        lambda db, selection_policy=None: [(provider, "circuit-model")],
    )
    for _ in range(circuit_breaker.FAILURE_THRESHOLD):
        circuit_breaker.record_failure("circuit", "circuit-model")
    assert circuit_breaker.is_open("circuit", "circuit-model") is True

    with pytest.raises(ProviderError) as exc:
        await registry.chat_with_fallback(
            db_session,
            [Message(role="user", content="hej")],
            effect_guard=True,
        )
    assert exc.value.category == "circuit_open"
    assert calls == []

    circuit_breaker.record_success("circuit", "circuit-model")
    result, _attempted = await registry.chat_with_fallback(
        db_session,
        [Message(role="user", content="hej")],
        effect_guard=True,
    )
    assert result.content == "should not run"
    assert calls == ["circuit-model"]


def test_09_expired_worker_lease_is_fenced_after_reclaim(superuser_db, make_verified_user):
    user, _ = make_verified_user()
    job = _job(owner_id=user.id, job_type="corpus_review", lease_generation=0)
    superuser_db.add(job)
    superuser_db.commit()

    claimed = claim_next_mainai_job(superuser_db, "new-worker", 60)
    assert claimed is not None
    _job_id, _owner_id, new_generation = claimed
    assert new_generation == 1

    renew_mainai_job_lease(superuser_db, job.id, "new-worker", new_generation, 60)
    with pytest.raises(JobLeaseLostError):
        renew_mainai_job_lease(superuser_db, job.id, "dead-worker", 0, 60)


def test_10_storage_failure_leaves_no_partial_blob_and_unknown_budget_holds(
    tmp_path, superuser_db, make_verified_user, monkeypatch
):
    storage = LocalFilesystemStorage(str(tmp_path))
    chunks = [b"partial"]

    def _broken_read() -> bytes:
        if chunks:
            return chunks.pop(0)
        raise OSError("simulated disk full")

    with pytest.raises(OSError):
        storage.write_stream(_broken_read, max_bytes=1024)
    assert list((tmp_path / "tmp").iterdir()) == []

    payload = b"durable bytes"
    offset = {"value": 0}

    def _good_read() -> bytes:
        start = offset["value"]
        if start >= len(payload):
            return b""
        offset["value"] = len(payload)
        return payload[start:]

    stored = storage.write_stream(_good_read, max_bytes=1024)
    assert storage.verify(stored.storage_key, expected_sha256=stored.sha256, expected_size=len(payload))

    founder, _ = make_verified_user()
    budget = ChatAttemptBudget(
        superuser_db,
        user=founder,
        limits=None,
        estimated_tokens=10,
        source_ref="v5-budget-fault",
        reply_slot=SimpleNamespace(
            id=founder.id,
            conversation_id=founder.id,
            in_reply_to_id=founder.id,
            provider=None,
            model=None,
        ),
        enforce=True,
    )
    budget.reservation = object()
    budget.attempt = object()
    held: list[dict] = []
    released: list[object] = []

    from app.billing import chat_attempts

    monkeypatch.setattr(chat_attempts, "mark_held", lambda db, attempt, **kwargs: held.append(kwargs))
    monkeypatch.setattr(
        "app.billing.reservation.release_reservation",
        lambda reservation: released.append(reservation),
    )

    budget.failed(
        ProviderError(
            "ambiguous provider outcome",
            category="external_effect_unknown",
            provider_request_may_have_left=True,
        )
    )
    assert budget.reservation is not None
    assert released == []
    assert held and held[0]["evidence"]["reason"] == "ambiguous_external_effect"

    budget.reservation = object()
    budget.failed(
        ProviderError(
            "proven pre-egress failure",
            category="unreachable",
            provider_request_may_have_left=False,
        )
    )
    assert budget.reservation is None
    assert len(released) == 1
