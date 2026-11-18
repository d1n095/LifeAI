"""Auditability and observability (P5) — disclosure-ledger correctness, correlation ids,
secret-free audit rows, evidence for failed/denied calls, bounded metrics.

The egress disclosure ledger is the audit backbone: every gated call — allowed OR denied —
writes exactly one ProviderDisclosureEvent with content HASHES (sha256) only, never the
prompt itself. These tests pin that contract, including the fail-closed behavior when the
ledger write itself fails.
"""

import asyncio
import uuid

import httpx
import pytest

import app.openai_consult.mainai as mainai_module
from app.config import Settings
from app.openai_consult.metrics import consultation_metrics
from app.openai_consult.schemas import ConsultIn
from app.openai_consult.service import ConsultationServiceError, consult_openai
from app.workforce.activation_gates import (
    REQUIRED_ACTIVATION_GATES,
    mark_safety_gate,
    reset_safety_gates_for_tests,
)

OWNER = "audit-owner-with-letters"
TEST_KEY = "sk-live-real-key-shaped-for-tests-0000000000"
SECRET_SENTINEL = "XSECRETSENTINELX"

GOAL_ID = uuid.UUID("4a44b55c-4333-4333-8333-33333333aa11")
TASK_ID = uuid.UUID("4a44b55c-4333-4333-8333-33333333aa22")
JOB_ID = uuid.UUID("4a44b55c-4333-4333-8333-33333333aa33")


def make_settings(**over) -> Settings:
    base = dict(
        environment="development",
        openai_consult_enabled=True,
        openai_consult_mainai_enabled=True,
        openai_api_key=TEST_KEY,
        openai_consult_allowed_models="gpt-4o-mini",
    )
    base.update(over)
    return Settings(**base)


class _StubResult:
    """Minimal SQLAlchemy Result stand-in for the platform-ledger writes the consultation
    settle path now performs (service.py's `_record_usage` → app/billing/accounting.py:
    tenant derivation SELECT + UPDATE-first rollup). first()/scalar() → None reads as "no
    membership row / no existing rollup", rowcount 0 sends the rollup to its INSERT-on-miss
    branch — the accounting layer then db.add()s the UsageLog, which LedgerDB captures."""

    def first(self):
        return None

    def scalar(self):
        return None

    @property
    def rowcount(self):
        return 0


class LedgerDB:
    """Captures exactly what the egress gate would persist, without a database."""

    def __init__(self, fail_flush: bool = False):
        self.events = []
        self.fail_flush = fail_flush

    def add(self, obj):
        self.events.append(obj)

    def flush(self):
        if self.fail_flush:
            raise RuntimeError("simulated ledger write failure")

    # The settle path unconditionally routes through app/billing/accounting.record_usage
    # ("one accounting pipeline, no dark spend") — that layer runs plain SQL via
    # db.execute; the fake reports an empty ledger instead of a database.
    def execute(self, statement, params=None):
        return _StubResult()


def responses_api_body(text="Consultation answer") -> dict:
    return {
        "id": "resp_audit_1",
        "object": "response",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }


def disclosure_events(db):
    """The egress-disclosure rows. Since the Revenue Engine wiring, a successful
    consultation also writes a UsageLog into the same ledger — filter it out here."""
    from app.models.provider_disclosure import ProviderDisclosureEvent

    return [e for e in db.events if isinstance(e, ProviderDisclosureEvent)]


def patch_transport(monkeypatch, handler) -> list[int]:
    from app.openai_consult import service as consult_service_module

    calls: list[int] = []

    def wrapping_handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return handler(request)

    def factory(timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(wrapping_handler), timeout=timeout)

    monkeypatch.setattr(consult_service_module, "_create_client", factory)
    return calls


@pytest.fixture(autouse=True)
def _clean_safety_state():
    reset_safety_gates_for_tests()
    for gate_key in REQUIRED_ACTIVATION_GATES:
        mark_safety_gate(gate_key)
    consultation_metrics.reset_for_tests()
    yield
    reset_safety_gates_for_tests()
    consultation_metrics.reset_for_tests()


async def mainai_consult(db, prompt="classify this note", **over):
    return await mainai_module.consult_for_mainai(
        db,
        owner_id=OWNER,
        goal_text=prompt,
        settings=make_settings(),  # explicit — never depend on the process-global config
        goal_id=over.pop("goal_id", GOAL_ID),
        task_id=over.pop("task_id", TASK_ID),
        job_id=over.pop("job_id", JOB_ID),
    )


# ---------------------------------------------------------------------------
# Ledger correctness + correlation ids (P5.1/P5.2)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_allowed_consultation_writes_one_correlated_event(monkeypatch):
    patch_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))
    db = LedgerDB()
    result, _stats = await mainai_consult(db)
    assert result.output_text == "Consultation answer"

    disclosures = disclosure_events(db)
    assert len(disclosures) == 1
    # The Revenue Engine also records the platform usage row for the same call.
    assert any(type(e).__name__ == "UsageLog" for e in db.events)
    event = disclosures[0]
    assert event.provider == "openai"
    assert event.model == "gpt-4o-mini"
    assert event.purpose == "mainai_consult"
    assert event.decision == "allowed"
    assert event.requested_by.startswith("app.openai_consult.mainai.consult_for_mainai")
    # P5.2 — goal/task/job correlation survives into the audit row:
    assert event.goal_id == GOAL_ID
    assert event.task_id == TASK_ID
    assert event.job_id == JOB_ID
    assert event.sent_content_hash is not None


@pytest.mark.asyncio
async def test_audit_rows_carry_hashes_never_content(monkeypatch):
    """P5.3 — the audit row must not expose the prompt, secrets, or the API key. Content
    appears ONLY as a sha256 hex hash."""
    patch_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))
    db = LedgerDB()
    secret_prompt = (
        f"my key is {TEST_KEY} and the vault secret is {SECRET_SENTINEL} — summarize"
    )
    await mainai_consult(db, prompt=secret_prompt)

    event = db.events[0]
    rendered = f"{event.reason} {event.redaction_categories} {event.attempted_content_hash} {event.sent_content_hash}"
    assert TEST_KEY not in rendered
    assert SECRET_SENTINEL not in rendered
    assert "vault secret" not in rendered
    # hashes are sha256 hex digests — 64 hex chars, not the content itself
    import re

    for h in (event.attempted_content_hash, event.sent_content_hash):
        assert h is not None and re.fullmatch(r"[0-9a-f]{64}", h)


@pytest.mark.asyncio
async def test_hash_is_tamper_evident(monkeypatch):
    """Different payloads → different hashes; the same payload → the same hash. Audit
    rows therefore prove exactly what was attempted/sent."""
    patch_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))
    db = LedgerDB()
    await mainai_consult(db, prompt="prompt A")
    await mainai_consult(db, prompt="prompt A")
    await mainai_consult(db, prompt="prompt B")

    hashes = [e.sent_content_hash for e in disclosure_events(db)]
    assert hashes[0] == hashes[1]  # identical payload → identical hash
    assert hashes[0] != hashes[2]  # different payload → different hash


@pytest.mark.asyncio
async def test_denied_call_audited_with_denied_decision(monkeypatch):
    calls = patch_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))
    db = LedgerDB()
    from app.egress_policy import EgressDeniedError

    with pytest.raises(EgressDeniedError):
        await mainai_consult(db, prompt="summarize NEVER_EGRESS: the recovery manifest")
    assert calls == []
    assert len(disclosure_events(db)) == 1 and disclosure_events(db)[0].decision == "denied"
    assert disclosure_events(db)[0].goal_id == GOAL_ID  # correlation preserved even on denial


# ---------------------------------------------------------------------------
# Evidence for FAILED calls (P5.4)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_provider_failure_still_leaves_audit_evidence(monkeypatch):
    """The egress gate runs BEFORE the HTTP call: even when the provider then fails, the
    attempted consultation is auditable (allowed decision + provider error metric)."""
    patch_transport(monkeypatch, lambda request: httpx.Response(500, json={"error": "boom"}))
    db = LedgerDB()
    with pytest.raises(ConsultationServiceError):
        await mainai_consult(db)

    assert len(disclosure_events(db)) == 1 and disclosure_events(db)[0].decision == "allowed"
    snap = consultation_metrics.snapshot()
    assert snap["provider_errors"] == 1 and snap["admitted"] == 0


# ---------------------------------------------------------------------------
# Audit-write failure must fail closed (P5.9)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ledger_write_failure_fails_closed(monkeypatch):
    """If the disclosure event cannot be persisted, the consultation is refused — audit
    availability is a precondition for egress, not a best-effort side effect."""
    calls = patch_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))
    db = LedgerDB(fail_flush=True)
    with pytest.raises(RuntimeError, match="ledger write failure"):
        await mainai_consult(db)
    assert calls == []  # nothing reached OpenAI without its audit row


# ---------------------------------------------------------------------------
# Bounded metrics (P5.5/P5.6)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_metrics_snapshot_is_fixed_keys_and_counts_correctly(monkeypatch):
    patch_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))
    db = LedgerDB()
    from app.egress_policy import EgressDeniedError

    await mainai_consult(db)  # admitted
    with pytest.raises(EgressDeniedError):
        await mainai_consult(db, prompt="summarize NEVER_EGRESS: x")  # refused (egress)
    snap = consultation_metrics.snapshot()

    assert set(snap.keys()) == {"admitted", "refused", "provider_errors", "last_latency_ms", "max_latency_ms"}
    assert snap["admitted"] == 1
    assert snap["refused"] == 1
    assert snap["provider_errors"] == 0
    assert isinstance(snap["last_latency_ms"], int) and snap["last_latency_ms"] >= 0
    assert snap["max_latency_ms"] >= snap["last_latency_ms"]

    # P5.6 — the snapshot cannot carry content: it is ints only.
    assert all(isinstance(v, int) for v in snap.values())
    assert OWNER not in str(snap)
    assert "classify" not in str(snap)
