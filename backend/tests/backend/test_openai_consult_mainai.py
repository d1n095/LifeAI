"""MainAI-invoked consultation gates — executable proof that refused consultations can
never reach OpenAI.

Every refusal test asserts ZERO transport invocations (the counting MockTransport is the
network witness): unauthorized, disabled, over-budget and egress-denied consultations do
not leave the process. Recursion guards, credential scrubbing and the structural
no-retrieval invariant of the openai_consult package are proven here too.

The SQLite-backed orchestration integration (consultation_mode through
execute_workforce_assignment) lives in test_openai_consult_workforce.py.
"""

import asyncio
import uuid
from pathlib import Path

import httpx
import pytest

import app.openai_consult.mainai as mainai_module
from app.config import Settings
from app.openai_consult.budgets import BudgetExceededError, DailyBudgetStore
from app.openai_consult.mainai import consult_for_mainai, consult_for_mainai_sync
from app.openai_consult.service import ConsultationConfigError
from app.workforce.activation_gates import (
    REQUIRED_ACTIVATION_GATES,
    ProviderActivationBlocked,
    mark_safety_gate,
    reset_safety_gates_for_tests,
)
from app.egress_policy import EgressDeniedError
from app.workforce.broker import DelegationBrokerError

OWNER = uuid.UUID("22222222-2222-2222-2222-222222222222")
TEST_KEY = "sk-live-real-key-shaped-for-tests-0000000000"


def make_settings(**over) -> Settings:
    base = dict(
        environment="development",
        openai_consult_enabled=True,
        openai_consult_mainai_enabled=True,
        openai_api_key=TEST_KEY,
        openai_consult_allowed_models="gpt-4o-mini,gpt-4o",
        openai_consult_max_output_tokens=1024,
        openai_consult_daily_token_budget=100_000,
        openai_consult_daily_cost_budget_usd="5.00",
    )
    base.update(over)
    return Settings(**base)


class RecordingDB:
    def __init__(self):
        self.events = []

    def add(self, obj):
        self.events.append(obj)

    def flush(self):
        pass


def responses_api_body(text="Consultation answer") -> dict:
    return {
        "id": "resp_mainai_1",
        "object": "response",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 12, "output_tokens": 30},
    }


@pytest.fixture
def gates_reset():
    reset_safety_gates_for_tests()
    yield
    reset_safety_gates_for_tests()


def install_counting_transport(monkeypatch, handler) -> list[int]:
    """Returns a list whose LENGTH is the network witness: any refusal must leave it empty.
    The transport is patched where consult_openai actually creates its client."""
    from app.openai_consult import service as consult_service_module

    calls: list[int] = []

    def wrapping_handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return handler(request)

    def factory(timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(wrapping_handler), timeout=timeout)

    monkeypatch.setattr(consult_service_module, "_create_client", factory)
    return calls


def budget_store(**over) -> DailyBudgetStore:
    base = dict(token_budget=100_000, cost_budget_usd="5.00", redis_url=None)
    base.update(over)
    return DailyBudgetStore(**base)


def verify_all_activation_gates() -> None:
    """Mark every required workforce activation gate VERIFIED (synthetic test evidence ref
    via the legacy shim). Used by tests that are NOT about the activation gate itself."""
    for gate_key in REQUIRED_ACTIVATION_GATES:
        mark_safety_gate(gate_key)


async def consult(settings=None, goal_text="classify this note", verify_gates=True, **over):
    settings = settings or make_settings()
    if verify_gates:
        verify_all_activation_gates()
    return await consult_for_mainai(
        RecordingDB(),
        owner_id=OWNER,
        goal_text=goal_text,
        settings=settings,
        budget_store=over.pop("budget_store", budget_store()),
        **over,
    )


# ---------------------------------------------------------------------------
# Zero-network proofs: unauthorized / disabled / over-budget / egress-denied
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unauthorized_mainai_flag_off_never_reaches_openai(monkeypatch, gates_reset):
    calls = install_counting_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))
    with pytest.raises(ConsultationConfigError):
        await consult(settings=make_settings(openai_consult_mainai_enabled=False))
    assert calls == []


@pytest.mark.asyncio
async def test_disabled_master_switch_never_reaches_openai(monkeypatch, gates_reset):
    calls = install_counting_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))
    with pytest.raises(ConsultationConfigError):
        await consult(settings=make_settings(openai_consult_enabled=False))
    assert calls == []


@pytest.mark.asyncio
async def test_unconfigured_allowlist_never_reaches_openai(monkeypatch, gates_reset):
    calls = install_counting_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))
    with pytest.raises(ConsultationConfigError):
        await consult(settings=make_settings(openai_consult_allowed_models=""))
    assert calls == []


@pytest.mark.asyncio
async def test_activation_gates_unmet_never_reaches_openai(monkeypatch, gates_reset):
    calls = install_counting_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))
    with pytest.raises(ProviderActivationBlocked):
        await consult(verify_gates=False)  # no gates marked — UNKNOWN != VERIFIED, fail-closed
    assert calls == []


@pytest.mark.asyncio
async def test_partially_verified_gates_still_block(monkeypatch, gates_reset):
    calls = install_counting_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))
    mark_safety_gate(REQUIRED_ACTIVATION_GATES[0])  # one verified, rest UNKNOWN
    with pytest.raises(ProviderActivationBlocked):
        await consult(verify_gates=False)
    assert calls == []


@pytest.mark.asyncio
async def test_over_budget_never_reaches_openai(monkeypatch, gates_reset):
    calls = install_counting_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))
    store = budget_store(token_budget=10)  # the 1024-token reservation alone busts this
    with pytest.raises(BudgetExceededError):
        await consult(budget_store=store)
    assert calls == []


@pytest.mark.asyncio
async def test_egress_denied_never_reaches_openai(monkeypatch, gates_reset):
    calls = install_counting_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))
    with pytest.raises(EgressDeniedError):
        await consult(goal_text="summarize NEVER_EGRESS: the recovery manifest")
    assert calls == []


@pytest.mark.asyncio
async def test_egress_denial_records_denied_disclosure_event(monkeypatch, gates_reset):
    """The denial is durably auditable: exactly one ProviderDisclosureEvent row, decision
    denied — even though nothing reached OpenAI."""
    install_counting_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))
    verify_all_activation_gates()
    events = []

    class LedgerDB:
        def add(self, obj):
            events.append(obj)

        def flush(self):
            pass

    with pytest.raises(EgressDeniedError):
        await consult_for_mainai(
            LedgerDB(), owner_id=OWNER, goal_text="summarize NEVER_EGRESS: x",
            settings=make_settings(), budget_store=budget_store(),
        )
    assert len(events) == 1
    assert events[0].decision == "denied"
    assert events[0].purpose == "mainai_consult"


@pytest.mark.asyncio
async def test_credential_shaped_goal_text_refused_before_egress(monkeypatch, gates_reset):
    """Input that IS a credential (not merely mentions one — embedded secrets are the
    egress gate's redaction job) is refused before the egress gate and any network call."""
    calls = install_counting_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))
    with pytest.raises(DelegationBrokerError):
        await consult(goal_text="sk-very-secret-credential-value-123456")
    assert calls == []


# ---------------------------------------------------------------------------
# Recursion guards
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nested_consultation_refused(monkeypatch, gates_reset):
    """A consultation triggered from INSIDE consultation processing is refused before any
    second network call — proven by a spy that attempts the nested consult at the exact
    point where the outer pipeline has the nesting flag set."""
    calls = install_counting_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))
    real_consult_openai = mainai_module.consult_openai
    nested_errors: list[str] = []

    async def spy_wrapper(db, **kwargs):
        try:
            await consult_for_mainai(
                RecordingDB(), owner_id=OWNER, goal_text="recursive follow-up",
                settings=make_settings(), budget_store=budget_store(),
            )
            nested_errors.append("NOT-REFUSED")
        except ConsultationConfigError as exc:
            nested_errors.append(str(exc))
        return await real_consult_openai(db, **kwargs)

    monkeypatch.setattr(mainai_module, "consult_openai", spy_wrapper)
    result, _stats = await consult()
    assert result.output_text == "Consultation answer"
    assert len(nested_errors) == 1
    assert "rekursiv" in nested_errors[0].lower()
    assert len(calls) == 1  # exactly one network call — the outer consultation only


@pytest.mark.asyncio
async def test_chain_depth_must_be_zero(monkeypatch, gates_reset):
    calls = install_counting_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))
    with pytest.raises(ConsultationConfigError):
        await consult(chain_depth=1)
    assert calls == []


def test_sync_wrapper_refuses_inside_running_loop(monkeypatch, gates_reset):
    """The sync harness entry point must refuse when called from async context — the
    structural anti-nesting guard for consultations kicked off inside event loops."""
    calls = install_counting_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))

    async def attempt():
        await consult_for_mainai_sync(
            RecordingDB(), owner_id=OWNER, goal_text="x", settings=make_settings(), budget_store=budget_store()
        )

    with pytest.raises(ConsultationConfigError):
        asyncio.run(attempt())
    assert calls == []


# ---------------------------------------------------------------------------
# Structural guarantees
# ---------------------------------------------------------------------------

def test_openai_consult_package_performs_no_memory_or_document_reads():
    """RETRIEVAL AUTHORITY != EGRESS AUTHORITY: the package must never read documents,
    memories or workforce context packages. The ONLY app.models import allowed anywhere in
    the package is app.models.user (the auth identity used by the HTTP router's
    dependency typing) — every retrieval/retrieval-adjacent import is forbidden, and the
    only DB write the package causes is the egress disclosure event (owned by
    egress_policy, not by this package)."""
    pkg_dir = Path(__file__).resolve().parents[2] / "app" / "openai_consult"
    assert pkg_dir.is_dir()
    forbidden = (
        "app.models.document",
        "app.models.memory",
        "app.models.founder_memory",
        "app.models.project_memory",
        "app.models.memory_thread",
        "app.models.memory_source",
        "app.models.active_context",
        "app.models.workforce",
        "WorkforceContextPackage",
        "from app.rag",
        "import app.rag",
        "retrieve_context",
        "index_document",
    )
    for source in sorted(pkg_dir.glob("*.py")):
        text = source.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text, f"{source.name} must not reference {needle}"
        for line in text.splitlines():
            if line.strip().startswith(("import ", "from ")) and "app.models" in line:
                assert "app.models.user" in line, (
                    f"{source.name}: only app.models.user (auth identity) may be imported, got: {line}"
                )


# ---------------------------------------------------------------------------
# Success path + shared budget with the founder path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_authorized_consultation_success(monkeypatch, gates_reset):
    calls = install_counting_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))
    store = budget_store()
    result, stats = await consult(budget_store=store)
    assert result.output_text == "Consultation answer"
    assert result.model == "gpt-4o-mini"
    assert stats["settled_in"] == 12 and stats["settled_out"] == 30
    assert len(calls) == 1
    tokens_left, _cost = store.remaining(str(OWNER))
    assert tokens_left == 100_000 - 42  # settled to REAL usage, not the reservation


@pytest.mark.asyncio
async def test_mainai_shares_owner_budget_with_founder_path(monkeypatch, gates_reset):
    calls = install_counting_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))
    store = budget_store()
    # Founder-path style reservation consumes most of the day's ceiling first:
    store.reserve(str(OWNER), estimated_tokens=99_500, estimated_cost_usd="0.10")
    with pytest.raises(BudgetExceededError):
        await consult(budget_store=store)  # the 1024-token reservation busts what's left
    assert calls == []
