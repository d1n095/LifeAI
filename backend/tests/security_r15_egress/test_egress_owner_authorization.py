"""Negative and positive regression tests for owner/lease-gated external egress.

The central boundary is ``providers.registry.chat_with_fallback``. These tests prove:

* an external chain without a real owner or a verified worker lease is denied before
  any provider call and before any disclosure-policy attempt;
* an invalid, expired, revoked, or mismatched worker lease is denied the same way;
* a local-only chain (Ollama) still works without an owner because nothing leaves
  the host;
* an authorized owner and a valid running lease both reach the provider with the
  owner carried into ``enforce_egress_policy``;
* a provider fallback re-runs the gate for every attempt and cannot bypass it.

Run without PostgreSQL/Redis:

    cd backend
    PYTHONPATH=. /tmp/r15-venv/bin/python -m pytest \
      tests/security_r15_egress -q \
      --confcutdir=tests/security_r15_egress
"""

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from app.egress_policy.service import EgressDeniedError, WorkerLease
from app.models.mainai_job import MainAIJobStatus
from app.providers.base import ChatResult, Message, ProviderError
from app.providers.registry import chat_with_fallback


class _FakeUser:
    def __init__(self, *, active=True, verified=True):
        self.is_active = active
        self.email_verified = verified


class _FakeDB:
    def __init__(self, job=None, user=None):
        self.job = job
        self.user = user

    def get(self, model, _identifier):
        if getattr(model, "__name__", "") == "User":
            return self.user
        return self.job


class _FakeProvider:
    def __init__(self, *, name="openai", local=False, fail=False):
        self.name = name
        self.is_local = local
        self.fail = fail
        self.calls = 0

    def is_configured(self):
        return True

    async def chat(self, messages, model, **_kwargs):
        self.calls += 1
        if self.fail:
            raise ProviderError(
                "provider unavailable",
                category="unreachable",
                provider_request_may_have_left=False,
            )
        return ChatResult(
            content="ok",
            provider=self.name,
            model=model,
            raw_usage={"prompt_tokens": 1, "completion_tokens": 1},
        )


class _FakeJob:
    def __init__(
        self,
        *,
        owner_id,
        worker_id,
        lease_generation=1,
        status=MainAIJobStatus.running,
        lease_expires_at=None,
    ):
        self.id = uuid.uuid4()
        self.owner_id = owner_id
        self.locked_by = worker_id
        self.lease_generation = lease_generation
        self.status = status
        self.lease_expires_at = lease_expires_at or (datetime.utcnow() + timedelta(minutes=5))


def _install_chain(monkeypatch, providers):
    from app.providers import registry

    monkeypatch.setattr(
        registry,
        "resolve_chat_chain",
        lambda db, selection_policy=None: [(provider, "test-model") for provider in providers],
    )


def _install_egress_spy(monkeypatch):
    from app.egress_policy import service as egress_service

    calls = []

    def fake_enforce(db, **kwargs):
        calls.append(kwargs)
        return kwargs["payload"]

    monkeypatch.setattr(egress_service, "enforce_egress_policy", fake_enforce)
    return calls


def _message():
    return Message(role="user", content="hej")


def test_external_chain_without_owner_or_lease_is_denied_before_provider(monkeypatch):
    provider = _FakeProvider()
    _install_chain(monkeypatch, [provider])
    egress_calls = _install_egress_spy(monkeypatch)

    with pytest.raises(EgressDeniedError):
        asyncio.run(chat_with_fallback(_FakeDB(), [_message()]))

    assert provider.calls == 0
    assert egress_calls == []


def test_local_chain_without_owner_still_runs(monkeypatch):
    provider = _FakeProvider(name="ollama", local=True)
    _install_chain(monkeypatch, [provider])
    egress_calls = _install_egress_spy(monkeypatch)

    result, attempted = asyncio.run(chat_with_fallback(_FakeDB(), [_message()]))

    assert result.content == "ok"
    assert attempted == ["ollama"]
    assert provider.calls == 1
    assert egress_calls == []


def test_unknown_owner_identity_is_denied_before_provider(monkeypatch):
    provider = _FakeProvider()
    _install_chain(monkeypatch, [provider])
    egress_calls = _install_egress_spy(monkeypatch)

    with pytest.raises(EgressDeniedError):
        asyncio.run(
            chat_with_fallback(_FakeDB(), [_message()], owner_id=uuid.uuid4())
        )

    assert provider.calls == 0
    assert egress_calls == []


def test_inactive_owner_is_denied_before_provider(monkeypatch):
    owner_id = uuid.uuid4()
    provider = _FakeProvider()
    _install_chain(monkeypatch, [provider])
    egress_calls = _install_egress_spy(monkeypatch)

    with pytest.raises(EgressDeniedError):
        asyncio.run(
            chat_with_fallback(
                _FakeDB(user=_FakeUser(active=False)),
                [_message()],
                owner_id=owner_id,
            )
        )

    assert provider.calls == 0
    assert egress_calls == []


def test_owner_identity_reaches_egress_gate(monkeypatch):
    owner_id = uuid.uuid4()
    provider = _FakeProvider()
    _install_chain(monkeypatch, [provider])
    egress_calls = _install_egress_spy(monkeypatch)

    asyncio.run(
        chat_with_fallback(
            _FakeDB(user=_FakeUser()), [_message()], owner_id=owner_id
        )
    )

    assert provider.calls == 1
    assert len(egress_calls) == 1
    assert egress_calls[0]["owner_id"] == owner_id


def test_fallback_cannot_bypass_gate(monkeypatch):
    owner_id = uuid.uuid4()
    first = _FakeProvider(name="openai", fail=True)
    second = _FakeProvider(name="anthropic")
    _install_chain(monkeypatch, [first, second])
    egress_calls = _install_egress_spy(monkeypatch)

    result, attempted = asyncio.run(
        chat_with_fallback(
            _FakeDB(user=_FakeUser()), [_message()], owner_id=owner_id
        )
    )

    assert result.provider == "anthropic"
    assert attempted == ["openai", "anthropic"]
    assert first.calls == 1
    assert second.calls == 1
    assert len(egress_calls) == 2
    assert all(call["owner_id"] == owner_id for call in egress_calls)


def test_expired_worker_lease_is_denied_before_provider(monkeypatch):
    owner_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    job = _FakeJob(
        owner_id=owner_id,
        worker_id=worker_id,
        lease_expires_at=datetime.utcnow() - timedelta(seconds=1),
    )
    lease = WorkerLease(
        job_id=job.id,
        owner_id=owner_id,
        worker_id=worker_id,
        lease_generation=1,
    )
    provider = _FakeProvider()
    _install_chain(monkeypatch, [provider])
    egress_calls = _install_egress_spy(monkeypatch)

    with pytest.raises(EgressDeniedError):
        asyncio.run(chat_with_fallback(_FakeDB(job), [_message()], worker_lease=lease))

    assert provider.calls == 0
    assert egress_calls == []


def test_revoked_worker_lease_is_denied_before_provider(monkeypatch):
    owner_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    job = _FakeJob(
        owner_id=owner_id,
        worker_id=worker_id,
        status=MainAIJobStatus.failed,
    )
    lease = WorkerLease(
        job_id=job.id,
        owner_id=owner_id,
        worker_id=worker_id,
        lease_generation=1,
    )
    provider = _FakeProvider()
    _install_chain(monkeypatch, [provider])

    with pytest.raises(EgressDeniedError):
        asyncio.run(chat_with_fallback(_FakeDB(job), [_message()], worker_lease=lease))

    assert provider.calls == 0


def test_mismatched_worker_lease_is_denied_before_provider(monkeypatch):
    owner_id = uuid.uuid4()
    job = _FakeJob(owner_id=owner_id, worker_id=uuid.uuid4())
    lease = WorkerLease(
        job_id=job.id,
        owner_id=owner_id,
        worker_id=uuid.uuid4(),
        lease_generation=1,
    )
    provider = _FakeProvider()
    _install_chain(monkeypatch, [provider])

    with pytest.raises(EgressDeniedError):
        asyncio.run(chat_with_fallback(_FakeDB(job), [_message()], worker_lease=lease))

    assert provider.calls == 0


def test_registry_planning_explicit_provider_enforces_egress_before_chat(monkeypatch):
    import app.egress_policy as egress_policy
    from app.provider_planning.service import RegistryPlanningAdapter
    from app.providers import registry

    owner_id = uuid.uuid4()
    provider = _FakeProvider()
    calls = []

    def fake_enforce(db, **kwargs):
        calls.append(kwargs)
        return kwargs["payload"]

    monkeypatch.setattr(egress_policy, "enforce_egress_policy", fake_enforce)
    monkeypatch.setattr(registry, "get_provider", lambda _name: provider)
    adapter = RegistryPlanningAdapter(
        _FakeDB(user=_FakeUser()),
        owner_id=owner_id,
        provider_name="openai",
        model="gpt-test",
    )

    response = asyncio.run(
        adapter.propose([], timeout_seconds=5, max_output_bytes=100)
    )

    assert response.provider == "openai"
    assert provider.calls == 1
    assert len(calls) == 1
    assert calls[0]["owner_id"] == owner_id


def test_registry_planning_explicit_provider_denies_before_chat(monkeypatch):
    import app.egress_policy as egress_policy
    from app.provider_planning.service import RegistryPlanningAdapter
    from app.providers import registry

    provider = _FakeProvider()

    def deny(_db, **_kwargs):
        raise EgressDeniedError("denied")

    monkeypatch.setattr(egress_policy, "enforce_egress_policy", deny)
    monkeypatch.setattr(registry, "get_provider", lambda _name: provider)
    adapter = RegistryPlanningAdapter(
        _FakeDB(user=_FakeUser()),
        owner_id=uuid.uuid4(),
        provider_name="openai",
        model="gpt-test",
    )

    with pytest.raises(EgressDeniedError):
        asyncio.run(adapter.propose([], timeout_seconds=5, max_output_bytes=100))

    assert provider.calls == 0


def test_registry_planning_fallback_forwards_owner(monkeypatch):
    from app.provider_planning.service import RegistryPlanningAdapter
    from app.providers import registry

    owner_id = uuid.uuid4()
    captured = {}

    async def fake_chat(db, messages, **kwargs):
        captured.update(kwargs)
        return ChatResult("ok", "openai", "gpt-test", {}), ["openai"]

    monkeypatch.setattr(registry, "chat_with_fallback", fake_chat)
    adapter = RegistryPlanningAdapter(_FakeDB(), owner_id=owner_id)

    response = asyncio.run(
        adapter.propose([], timeout_seconds=5, max_output_bytes=100)
    )

    assert response.provider == "openai"
    assert captured["owner_id"] == owner_id


def test_valid_worker_lease_authorizes_external_egress(monkeypatch):
    owner_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    job = _FakeJob(owner_id=owner_id, worker_id=worker_id)
    lease = WorkerLease(
        job_id=job.id,
        owner_id=owner_id,
        worker_id=worker_id,
        lease_generation=1,
    )
    provider = _FakeProvider()
    _install_chain(monkeypatch, [provider])
    egress_calls = _install_egress_spy(monkeypatch)

    result, _attempted = asyncio.run(
        chat_with_fallback(_FakeDB(job), [_message()], worker_lease=lease)
    )

    assert result.content == "ok"
    assert provider.calls == 1
    assert len(egress_calls) == 1
    assert egress_calls[0]["owner_id"] == owner_id
