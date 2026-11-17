"""Orchestration integration: MainAI consultation through the EXISTING workforce harness
(app/workforce/provider_worker.execute_workforce_assignment with consultation_mode=True).

Runs the REAL orchestration stack — registry, broker, authority, activation gates,
provider worker, receipts — on an in-memory SQLite database (JSONB→JSON compiler shim;
test-only, production stays PostgreSQL). The local in-process worker remains the primary
path (consultation_mode=False is byte-identical behavior), and the consultation branch is
proven blocked by every gate BEFORE any network call, with the counting MockTransport as
the network witness.
"""

import uuid

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

import app.openai_consult.mainai as mainai_module
from app.config import Settings
from app.db import Base
from app.openai_consult.service import ConsultationConfigError
from app.workforce.activation_gates import (
    REQUIRED_ACTIVATION_GATES,
    ProviderActivationBlocked,
    mark_safety_gate,
    reset_safety_gates_for_tests,
)
from app.workforce.broker import resolve_delegation, submit_delegation_request
from app.workforce.provider_worker import execute_workforce_assignment
from app.workforce.registry import register_workforce_agent


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"  # test-only shim: JSONB is Postgres-only; SQLite stores JSON text


# Fixed test owner — MUST contain non-digit characters: SQLite's NUMERIC affinity coerces
# an all-digit UUID hex string (e.g. "33333333-3333-...") into a REAL on storage, which
# then explodes on read. Real uuid4() values always contain letters; this fixed one does too.
OWNER = uuid.UUID("3a33b3c3-4333-8333-b333-33333333abcd")
TEST_KEY = "sk-live-real-key-shaped-for-tests-0000000000"

GOAL_TEXT = "classify this note for the founder"
MOCK_ANSWER = "Consultation answer from the Responses API"


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


def responses_api_body(text=MOCK_ANSWER) -> dict:
    return {
        "id": "resp_workforce_1",
        "object": "response",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 15, "output_tokens": 25},
    }


def install_counting_transport(monkeypatch, handler) -> list[int]:
    from app.openai_consult import service as consult_service_module

    calls: list[int] = []

    def wrapping_handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return handler(request)

    def factory(timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(wrapping_handler), timeout=timeout)

    monkeypatch.setattr(consult_service_module, "_create_client", factory)
    return calls


@pytest.fixture
def workforce_env(monkeypatch):
    """Fresh in-memory DB + full workforce stack + verified activation gates + injected
    settings. Yields (db, assignment)."""
    reset_safety_gates_for_tests()
    for gate_key in REQUIRED_ACTIVATION_GATES:
        mark_safety_gate(gate_key)

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()

    researcher = register_workforce_agent(
        db, owner_id=OWNER, agent_key="consult-researcher", name="Consult Researcher",
        role="researcher", agent_type="RESEARCH", capability_tags=["classification"],
        trust_zone="LOCAL_INTERNAL", allowed_tool_classes=["read_excerpt"],
        cost_class="low", status="active",
    )
    verifier = register_workforce_agent(
        db, owner_id=OWNER, agent_key="consult-verifier", name="Consult Verifier",
        role="verifier", agent_type="VERIFIER", capability_tags=["verification"],
        trust_zone="LOCAL_INTERNAL", allowed_tool_classes=["read_excerpt"],
        cost_class="low", status="active",
    )
    request = submit_delegation_request(
        db, owner_id=OWNER, goal_text=GOAL_TEXT, required_capability="classification",
    )
    assignment = resolve_delegation(db, owner_id=OWNER, request=request, verifier_profile_id=verifier.id)

    settings = make_settings()
    monkeypatch.setattr(mainai_module, "get_settings", lambda: settings)

    yield db, assignment, settings

    db.close()
    engine.dispose()
    reset_safety_gates_for_tests()


# ---------------------------------------------------------------------------
# The local in-process worker is the PRIMARY path — consultation_mode=False is unchanged
# ---------------------------------------------------------------------------

def test_consultation_mode_false_uses_local_in_process_worker(workforce_env):
    db, assignment, _settings = workforce_env
    receipt = execute_workforce_assignment(
        db, owner_id=OWNER, assignment=assignment, goal_text=GOAL_TEXT, capability="classification",
    )
    assert receipt.provider_invoked is False
    assert receipt.provider_name is None
    assert receipt.raw_output_scrubbed["worker"] == "in_process"
    assert receipt.spend_settled is False
    assert all("consultation" not in note for note in receipt.notes)


# ---------------------------------------------------------------------------
# Authorized consultation through the real orchestration interface
# ---------------------------------------------------------------------------

def test_authorized_consultation_through_workforce_harness(monkeypatch, workforce_env):
    db, assignment, settings = workforce_env
    calls = install_counting_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))

    receipt = execute_workforce_assignment(
        db, owner_id=OWNER, assignment=assignment, goal_text=GOAL_TEXT, capability="classification",
        consultation_mode=True,
        consultation_goal_id=uuid.uuid4(),  # orchestration's own ledger linkage
    )

    assert len(calls) == 1  # exactly one outbound call
    assert receipt.provider_invoked is True
    assert receipt.provider_name == "openai"
    assert receipt.model_id == "gpt-4o-mini"
    assert receipt.raw_output_scrubbed["answer"] == MOCK_ANSWER
    assert receipt.spend_reserved is True and receipt.spend_settled is True
    assert receipt.external_effect_state == "none_proven"
    assert "consultation_completed" in receipt.notes
    assert "consultation_egress_ledger_recorded" in receipt.notes

    # egress disclosure ledger: exactly one row, purpose mainai_consult, allowed
    from app.models.provider_disclosure import ProviderDisclosureEvent

    events = db.query(ProviderDisclosureEvent).all()
    assert len(events) == 1
    assert events[0].provider == "openai"
    assert events[0].purpose == "mainai_consult"
    assert events[0].decision == "allowed"


def test_consultation_request_body_is_bounded(monkeypatch, workforce_env):
    """The outbound body carries ONLY the explicit goal text: no tools, no store, no
    schema — structural proof at the wire level."""
    db, assignment, _settings = workforce_env
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(body=__import__("json").loads(request.content))
        return httpx.Response(200, json=responses_api_body())

    install_counting_transport(monkeypatch, handler)
    receipt = execute_workforce_assignment(
        db, owner_id=OWNER, assignment=assignment, goal_text=GOAL_TEXT, capability="classification",
        consultation_mode=True,
    )
    assert receipt.provider_invoked is True
    body = captured["body"]
    assert body["input"][0]["content"] == GOAL_TEXT  # exactly the explicit input
    assert "tools" not in body
    assert body["store"] is False
    assert "instructions" not in body


# ---------------------------------------------------------------------------
# Refusals through the same orchestration interface — zero network
# ---------------------------------------------------------------------------

def test_consultation_mode_with_gates_unmet_refuses(monkeypatch, workforce_env):
    from app.workforce.activation_gates import reset_safety_gates_for_tests

    db, assignment, _settings = workforce_env
    reset_safety_gates_for_tests()  # gates back to UNKNOWN
    calls = install_counting_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))

    with pytest.raises(ProviderActivationBlocked):
        execute_workforce_assignment(
            db, owner_id=OWNER, assignment=assignment, goal_text=GOAL_TEXT, capability="classification",
            consultation_mode=True,
        )
    assert calls == []


def test_consultation_mode_with_mainai_flag_off_refuses(monkeypatch, workforce_env):
    db, assignment, _settings = workforce_env
    monkeypatch.setattr(mainai_module, "get_settings", lambda: make_settings(openai_consult_mainai_enabled=False))
    calls = install_counting_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))

    with pytest.raises(ConsultationConfigError):
        execute_workforce_assignment(
            db, owner_id=OWNER, assignment=assignment, goal_text=GOAL_TEXT, capability="classification",
            consultation_mode=True,
        )
    assert calls == []


def test_consultation_mode_with_master_switch_off_refuses(monkeypatch, workforce_env):
    db, assignment, _settings = workforce_env
    monkeypatch.setattr(mainai_module, "get_settings", lambda: make_settings(openai_consult_enabled=False))
    calls = install_counting_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))

    with pytest.raises(ConsultationConfigError):
        execute_workforce_assignment(
            db, owner_id=OWNER, assignment=assignment, goal_text=GOAL_TEXT, capability="classification",
            consultation_mode=True,
        )
    assert calls == []


def test_consultation_over_budget_refuses(monkeypatch, workforce_env):
    db, assignment, _settings = workforce_env
    calls = install_counting_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))

    # Inject a budget store whose token ceiling is already spent for the day.
    from app.openai_consult.budgets import DailyBudgetStore

    exhausted = DailyBudgetStore(token_budget=0, cost_budget_usd="0.00", redis_url=None)
    monkeypatch.setattr(
        "app.openai_consult.budgets.build_budget_store_from_settings", lambda settings: exhausted
    )
    with pytest.raises(Exception) as excinfo:
        execute_workforce_assignment(
            db, owner_id=OWNER, assignment=assignment, goal_text=GOAL_TEXT, capability="classification",
            consultation_mode=True,
        )
    assert type(excinfo.value).__name__ == "BudgetExceededError"
    assert calls == []


def test_consultation_egress_denied_refuses(monkeypatch, workforce_env):
    db, assignment, _settings = workforce_env
    calls = install_counting_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))

    with pytest.raises(Exception) as excinfo:
        execute_workforce_assignment(
            db, owner_id=OWNER, assignment=assignment,
            goal_text="review NEVER_EGRESS: the recovery manifest", capability="classification",
            consultation_mode=True,
        )
    assert type(excinfo.value).__name__ == "EgressDeniedError"
    assert calls == []

    # the refusal IS audited: one denied disclosure event on this owner
    from app.models.provider_disclosure import ProviderDisclosureEvent

    events = db.query(ProviderDisclosureEvent).all()
    assert len(events) == 1
    assert events[0].decision == "denied"
    assert events[0].purpose == "mainai_consult"
