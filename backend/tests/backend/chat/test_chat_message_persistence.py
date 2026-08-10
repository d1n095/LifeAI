"""MainAI chat — message persistence / failure-boundary fix (see app/routers/chat.py's module
docstring and docs/BRANCH_REGISTRY.md's 2026-07-26 LLM Coupling & Failure-Boundary Audit,
finding #1: the founder's own chat message used to be lost whenever the AI provider failed,
because it was only ever committed in the same transaction as a successful reply).

Covers exactly the required scenarios:
  A. provider succeeds — normal contract, assistant_status="succeeded".
  B. provider fails before producing output (e.g. missing key) — user message still saved,
     assistant_status="failed", safe (non-leaking) error_category.
  C. provider times out — same shape, classified "unreachable", retryable=True.
  D. retry does not duplicate the user message.
  E. reload (a fresh GET /api/conversations/{id}) confirms the user message remains after a
     provider failure.
  F. idempotent retry: retrying an already-succeeded reply returns it as-is, without a second
     provider call or a duplicate assistant row.
  G. the EMBEDDING provider (retrieve_context(), called before the chat provider — see
     app/routers/chat.py's 2026-07-26 incident note) failing must never crash the request with
     an unhandled 500, whether the chat provider then succeeds (degrades to an ungrounded
     answer) or also fails (still the normal, clean failed-contract response).
"""

import httpx
import pytest

from app.config import get_settings
from app.models.conversation import Message as MessageModel, MessageRole, MessageStatus
from app.providers.base import ChatResult, ProviderError
from app.providers.openai_provider import OpenAIProvider

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
DIM = get_settings().embedding_dim


@pytest.fixture(autouse=True)
def _fake_embed(monkeypatch):
    """Every /api/chat call runs retrieve_context() first, which embeds the query — fake it
    everywhere in this file so tests never make a real network call just to reach the part of
    chat() this file actually exercises (the persistence/failure-boundary behavior)."""

    async def _embed(self, texts, model, **kwargs):
        return [[0.1] * DIM for _ in texts]

    monkeypatch.setattr(OpenAIProvider, "embed", _embed)


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200
    return res.json()["csrf_token"]


def _latest_message(superuser_db, role: MessageRole) -> MessageModel | None:
    """Reads back what the endpoint actually persisted, on the SUPERUSER connection.

    These assertions used the restricted `db_session` fixture until migration 0031 gave
    `messages` an RLS policy of its own. They cannot any more, and should not have: this
    ad-hoc test session never goes through app/deps.py, so it has no `app.current_user_id`
    bound, and a restricted read here now correctly returns nothing. That is not a change in
    what the endpoint stores — the rows are there either way, as the superuser read below
    proves — only in what an unscoped connection is permitted to see.

    Using superuser_db is what conftest.py's own fixture docstring prescribes for exactly this
    situation: verifying that a row genuinely exists is ambiguous on the restricted role,
    because zero rows could mean "never written" or "written and hidden". Keeping these on
    db_session would have meant weakening the policy to keep a test convenient."""
    return (
        superuser_db.query(MessageModel)
        .filter_by(role=role)
        .order_by(MessageModel.created_at.desc(), MessageModel.id.desc())
        .first()
    )


def _fake_chat_ok(content: str = "Testsvar."):
    async def _chat(self, messages, model, **kwargs):
        return ChatResult(content=content, provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 3})

    return _chat


async def _fake_chat_missing_key(self, messages, model, **kwargs):
    raise ProviderError("OpenAI API-nyckel saknas.")


async def _fake_chat_timeout(self, messages, model, **kwargs):
    raise httpx.TimeoutException("timed out")


# --- A/B/C: the response contract ------------------------------------------------------------


def test_provider_succeeds_returns_full_contract(client, monkeypatch):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_ok("Hej!"))
    csrf = _login(client)
    res = client.post("/api/chat", json={"message": "Hej"}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["assistant_status"] == "succeeded"
    assert body["user_message_saved"] is True
    assert body["reply"] == "Hej!"
    assert body["error_category"] is None
    assert body["retryable"] is False
    assert body["assistant_message_id"] is not None


def test_provider_fails_before_producing_output_saves_user_message(client, superuser_db, monkeypatch):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_missing_key)
    csrf = _login(client)
    res = client.post("/api/chat", json={"message": "Ett meddelande som inte får försvinna."}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text  # never a misleading generic failure — the user message DID succeed
    body = res.json()
    assert body["user_message_saved"] is True
    assert body["assistant_status"] == "failed"
    assert body["reply"] is None
    # classify_provider_exception() (app/providers/verification.py) can't distinguish "missing
    # key" from any other ProviderError text without risking echoing raw provider details, so
    # a plain ProviderError classifies as "unreachable" — safe by construction, not a leak.
    assert body["error_category"] == "unreachable"
    assert body["retryable"] is True
    # The safe, classified message — never the raw "OpenAI API-nyckel saknas." exception text.
    assert body["error_message"] is not None
    assert "nyckel" not in body["error_message"].lower()

    # The actual fix: the user's message is genuinely in the database, not just in the
    # response body of this one request.
    saved = _latest_message(superuser_db, MessageRole.user)
    assert saved is not None
    assert saved.content == "Ett meddelande som inte får försvinna."
    assert saved.status == MessageStatus.succeeded  # the USER message itself always succeeded


def test_provider_timeout_is_retryable(client, monkeypatch):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_timeout)
    csrf = _login(client)
    res = client.post("/api/chat", json={"message": "hej"}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["assistant_status"] == "failed"
    assert body["error_category"] == "unreachable"
    assert body["retryable"] is True


# --- D/E: persistence survives a failure, and reload proves it -------------------------------


def test_reload_confirms_user_message_remains_after_provider_failure(client, monkeypatch):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_missing_key)
    csrf = _login(client)
    sent = client.post("/api/chat", json={"message": "Detta meddelande ska överleva ett AI-fel."}, headers={"X-CSRF-Token": csrf})
    assert sent.status_code == 200, sent.text
    conversation_id = sent.json()["conversation_id"]

    # A genuinely fresh read — the same thing a page reload would do.
    reloaded = client.get(f"/api/conversations/{conversation_id}")
    assert reloaded.status_code == 200, reloaded.text
    messages = reloaded.json()["messages"]
    user_messages = [m for m in messages if m["role"] == "user"]
    assert len(user_messages) == 1
    assert user_messages[0]["content"] == "Detta meddelande ska överleva ett AI-fel."


# --- D/F: retry never duplicates, and is idempotent once succeeded ---------------------------


def test_retry_does_not_duplicate_the_user_message_and_can_eventually_succeed(client, superuser_db, monkeypatch):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_missing_key)
    csrf = _login(client)
    sent = client.post("/api/chat", json={"message": "Fixa detta senare."}, headers={"X-CSRF-Token": csrf})
    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert body["assistant_status"] == "failed"
    user_message_id = body["user_message_id"]

    # Provider comes back online before the retry.
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_ok("Nu funkar det."))
    retried = client.post(f"/api/chat/messages/{user_message_id}/retry", headers={"X-CSRF-Token": csrf})
    assert retried.status_code == 200, retried.text
    retried_body = retried.json()
    assert retried_body["assistant_status"] == "succeeded"
    assert retried_body["reply"] == "Nu funkar det."
    assert retried_body["user_message_id"] == user_message_id

    user_rows = superuser_db.query(MessageModel).filter_by(id=user_message_id).all()
    assert len(user_rows) == 1  # never duplicated
    assistant_rows = superuser_db.query(MessageModel).filter_by(in_reply_to_id=user_message_id).all()
    assert len(assistant_rows) == 1  # updated in place, not a second row
    assert assistant_rows[0].status == MessageStatus.succeeded


def test_retry_is_idempotent_once_a_reply_has_succeeded(client, monkeypatch):
    calls = {"count": 0}

    async def _counting_chat(self, messages, model, **kwargs):
        calls["count"] += 1
        return ChatResult(content="Första svaret.", provider="openai", model=model, raw_usage={})

    monkeypatch.setattr(OpenAIProvider, "chat", _counting_chat)
    csrf = _login(client)
    sent = client.post("/api/chat", json={"message": "hej"}, headers={"X-CSRF-Token": csrf})
    assert sent.status_code == 200, sent.text
    user_message_id = sent.json()["user_message_id"]
    assert calls["count"] == 1

    # If retry were called again with a DIFFERENT fake answer, an idempotent implementation
    # must still return the ORIGINAL succeeded reply rather than generating a new one.
    async def _different_chat(self, messages, model, **kwargs):
        calls["count"] += 1
        return ChatResult(content="Ett helt annat svar.", provider="openai", model=model, raw_usage={})

    monkeypatch.setattr(OpenAIProvider, "chat", _different_chat)
    retried = client.post(f"/api/chat/messages/{user_message_id}/retry", headers={"X-CSRF-Token": csrf})
    assert retried.status_code == 200, retried.text
    assert retried.json()["reply"] == "Första svaret."
    assert calls["count"] == 1  # no second provider call was made


# --- G: the embedding provider (retrieval) failing must not crash the request ----------------


async def _failing_embed(self, texts, model, **kwargs):
    raise ProviderError("OpenAI API-nyckel saknas.")


def test_embedding_failure_degrades_to_ungrounded_reply_when_chat_still_works(client, monkeypatch):
    # Overrides this file's autouse _fake_embed fixture for just this test — the scenario is a
    # founder with a working chat provider but no embedding provider (or an embedding provider
    # that's down): retrieval must degrade to "no context found" rather than raising, so the
    # chat call below still gets a real answer instead of never being reached at all.
    monkeypatch.setattr(OpenAIProvider, "embed", _failing_embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_ok("Svar utan källor."))
    csrf = _login(client)
    res = client.post("/api/chat", json={"message": "hej"}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["assistant_status"] == "succeeded"
    assert body["reply"] == "Svar utan källor."
    assert body["sources"] == []


def test_embedding_failure_with_no_provider_at_all_returns_clean_failed_contract(client, superuser_db, monkeypatch):
    # The exact incident: no provider configured (or none reachable) for EITHER role. Before
    # the fix, retrieve_context()'s ProviderError propagated out of _attempt_assistant_reply()
    # unhandled, past chat_with_fallback()'s own try/except (never reached), producing a raw
    # 500 instead of this response.
    monkeypatch.setattr(OpenAIProvider, "embed", _failing_embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_missing_key)
    csrf = _login(client)
    res = client.post("/api/chat", json={"message": "Fungerar du utan nyckel?"}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text  # never a 500
    body = res.json()
    assert body["user_message_saved"] is True
    assert body["assistant_status"] == "failed"
    assert body["error_category"] == "unreachable"
    assert body["retryable"] is True

    saved = _latest_message(superuser_db, MessageRole.user)
    assert saved is not None
    assert saved.content == "Fungerar du utan nyckel?"


# --- H: the execution-truthfulness sanitizer fires through the real HTTP boundary ------------


def test_unverified_execution_claim_from_the_model_is_sanitized_through_the_real_endpoint(client, superuser_db, monkeypatch):
    """Founder re-review round (PR #36), item #3 (fourth pass): the model itself is untrusted —
    nothing stops an LLM from producing "jag arbetar med det i bakgrunden" as ordinary free
    text. This proves app/mainai_runtime_contract.py's sanitize_unverified_execution_claims()
    actually fires on the real /api/chat response body AND the persisted MessageModel row, not
    just in the contract module's own unit tests (see tests/backend/jobs/test_mainai_jobs.py) -- and, per the
    founder's HIGH-severity finding against the previous append-only version, that the false
    claim itself is genuinely GONE from what the founder reads, not merely followed by a
    disclaimer that leaves both the lie and the correction visible at once."""
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_ok("Visst! Jag arbetar med det i bakgrunden och återkommer snart."))
    csrf = _login(client)
    res = client.post("/api/chat", json={"message": "Kan du granska alla dokument?"}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["assistant_status"] == "succeeded"
    assert "jag arbetar med det i bakgrunden" not in body["reply"].lower()
    assert "jag återkommer" not in body["reply"].lower()
    assert "MainAI" in body["reply"]  # a visible, honest MainAI correction still present

    saved = _latest_message(superuser_db, MessageRole.assistant)
    assert saved is not None
    assert "jag arbetar med det i bakgrunden" not in saved.content.lower()


def test_unverified_execution_claim_via_english_phrasing_is_sanitized_through_the_real_endpoint(client, monkeypatch):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_ok("The job has started, I'll let you know when it's done."))
    csrf = _login(client)
    res = client.post("/api/chat", json={"message": "Can you review everything?"}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    body = res.json()
    assert "the job has started" not in body["reply"].lower()
    assert "let you know when it's done" not in body["reply"].lower()


def test_unverified_execution_claim_is_sanitized_through_the_retry_path_too(client, monkeypatch):
    """_attempt_assistant_reply() is shared between the initial send and the retry endpoint —
    this proves the sanitizer fires on BOTH, not just the first path tested above."""
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_missing_key)
    csrf = _login(client)
    sent = client.post("/api/chat", json={"message": "Granska mina dokument."}, headers={"X-CSRF-Token": csrf})
    assert sent.status_code == 200, sent.text
    user_message_id = sent.json()["user_message_id"]

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_ok("Jag övervakar situationen och hör av mig när det är klart."))
    retried = client.post(f"/api/chat/messages/{user_message_id}/retry", headers={"X-CSRF-Token": csrf})
    assert retried.status_code == 200, retried.text
    reply = retried.json()["reply"]
    assert "jag övervakar situationen" not in reply.lower()


def test_ordinary_reply_without_an_execution_claim_is_left_untouched(client, monkeypatch):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_ok("Här är svaret på din fråga."))
    csrf = _login(client)
    res = client.post("/api/chat", json={"message": "Vad är klockan?"}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["reply"] == "Här är svaret på din fråga."
    assert "MainAI-obs" not in body["reply"]


def test_retry_requires_founder_auth(client):
    res = client.post("/api/chat/messages/00000000-0000-0000-0000-000000000000/retry")
    assert res.status_code == 401


@pytest.mark.parametrize("bad_id", ["00000000-0000-0000-0000-000000000000"])
def test_retry_404s_for_unknown_message(client, bad_id, monkeypatch):
    csrf = _login(client)
    res = client.post(f"/api/chat/messages/{bad_id}/retry", headers={"X-CSRF-Token": csrf})
    assert res.status_code == 404
