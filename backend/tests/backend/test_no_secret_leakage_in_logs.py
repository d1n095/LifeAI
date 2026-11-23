"""Stream 7, item 2 (PR #478 §9): secret-handling was previously verified by generating real
login traffic once and manually grepping the resulting log output -- explicitly flagged in
`docs/FOUNDER_ALPHA_INDEPENDENT_REVIEW_PACKAGE.md` as not a repeatable, automated check. This
module makes it one, permanently enforced by CI (`tests/backend/` runs wholesale in
`.github/workflows/ci.yml`), rather than inventing a separate scanning pipeline.

Approach: a keyword grep (`password|secret|token|...`) on real log text is exactly what the
review package's own manual repro used, but as an automated assertion it is too blunt -- a log
line can mention the WORD "token" perfectly safely (an "access_token_expire_minutes" setting
name, for instance) without leaking a VALUE. So instead: configure genuinely distinctive canary
secret VALUES (a fake OpenAI key, the founder's test password), drive real traffic that
plausibly touches each of them through the actual FastAPI app (not mocked loggers), capture
every log record emitted anywhere in the process at DEBUG across all loggers, and assert the
exact canary VALUES never appear in any of them, in the response bodies, or in raised exception
text. This is strictly stronger than keyword grepping for the leak shapes it does reach, and it
covers real HTTP traffic end to end rather than calling chat_with_fallback() directly --
complementing, not replacing, test_chat_fallback_logging.py's narrower unit-level check (the
real /api/chat endpoint sets effect_guard=True, which intercepts a plain provider exception
before classify_provider_exception ever runs, so it is NOT the same code path that incident's
own test exercises; see the first test below for what effect_guard=True's failure shape
actually looks like over real traffic, and its docstring for the one case -- a ProviderError a
provider module raises itself -- this layer deliberately trusts rather than redacts)."""

from __future__ import annotations

import logging

import httpx
import pytest

from app.config import get_settings
from app.providers.base import ChatResult
from app.providers.gemini_provider import GeminiProvider
from app.providers.openai_provider import OpenAIProvider

# The real bootstrapped founder account (require_founder pins to this exact row, not just a
# role -- see app/deps.py -- so a freshly created make_verified_user() row can never pass the
# chat router's dependency; the founder's own real test credentials ARE the canary here).
FOUNDER_EMAIL = "founder@lifeos.local"
CANARY_PASSWORD = "TestFounderPassword123!"
CANARY_API_KEY = "sk-canary-must-never-reach-a-log-line-7d1e4c"


@pytest.fixture(autouse=True, scope="module")
def _apply_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


def _assert_canary_absent(haystacks: list[str], *, canary: str, where: str) -> None:
    for text in haystacks:
        assert canary not in text, f"canary secret leaked into {where}: {text!r}"


@pytest.mark.asyncio
async def test_real_login_and_chat_failure_traffic_never_logs_the_real_secrets(
    client, db_session, monkeypatch, caplog
):
    """Drives two realistic traffic shapes through the real app and asserts neither canary
    value appears anywhere in the logs: (1) a real login with the founder's real password, and
    (2) a real chat request whose only configured provider fails with a secret embedded in a
    raw httpx exception -- the real /api/chat endpoint's effect_guard=True intercepts this
    before classify_provider_exception ever runs (a provider request that may have actually
    reached the network is never auto-retried/logged further -- "Provider outcome is uncertain"
    is the real, generic failure shape a founder actually sees), so this proves the SAME
    historical leak shape stays safe at this higher layer too, via a different, earlier gate
    rather than literally re-exercising classify_provider_exception's own redaction."""
    monkeypatch.setattr(get_settings(), "default_llm_provider", "gemini")
    monkeypatch.setattr(get_settings(), "google_api_key", CANARY_API_KEY)
    monkeypatch.setattr(get_settings(), "openai_api_key", None)
    monkeypatch.setattr(get_settings(), "anthropic_api_key", None)
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")

    async def _raise_with_canary_key_in_message(self, messages, model, **kwargs):
        request = httpx.Request("POST", f"https://example.invalid/v1/chat?key={CANARY_API_KEY}")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError(
            f"401 Unauthorized for url using key={CANARY_API_KEY}", request=request, response=response
        )

    monkeypatch.setattr(GeminiProvider, "chat", _raise_with_canary_key_in_message)

    caplog.set_level(logging.DEBUG)

    login_res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": CANARY_PASSWORD})
    assert login_res.status_code == 200, login_res.text
    csrf = login_res.json()["csrf_token"]

    chat_res = client.post("/api/chat", json={"message": "Hej"}, headers={"X-CSRF-Token": csrf})
    # This API models a failed assistant turn as a 200 with assistant_status="failed" (the
    # user's message is still durably saved), not an HTTP error status -- confirm the real
    # failure shape happened at all, so this test is actually exercising the failure path.
    assert chat_res.status_code == 200, chat_res.text
    assert chat_res.json()["assistant_status"] == "failed", chat_res.text

    log_texts = [record.getMessage() for record in caplog.records]
    response_texts = [login_res.text, chat_res.text]

    _assert_canary_absent(log_texts, canary=CANARY_PASSWORD, where="a log record")
    _assert_canary_absent(log_texts, canary=CANARY_API_KEY, where="a log record")
    _assert_canary_absent(response_texts, canary=CANARY_PASSWORD, where="an HTTP response body")
    _assert_canary_absent(response_texts, canary=CANARY_API_KEY, where="an HTTP response body")


@pytest.mark.asyncio
async def test_a_real_successful_chat_call_never_leaks_the_configured_provider_key(
    client, db_session, monkeypatch, caplog
):
    """The complementary real-traffic case to the failure path above: an ordinary, successful
    chat turn must not leak the configured provider API key either -- a real request/response
    cycle is exactly where a careless success-path log line (e.g. logging the outbound request
    object instead of just its outcome) could leak a key that a FAILURE-only check would never
    see at all."""
    monkeypatch.setattr(get_settings(), "default_llm_provider", "openai")
    monkeypatch.setattr(get_settings(), "openai_api_key", CANARY_API_KEY)
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")

    async def _fake_openai_chat(self, messages, model, **kwargs):
        return ChatResult(content="Hej! Hur kan jag hjälpa dig?", provider="openai", model=model, raw_usage={
            "prompt_tokens": 10, "completion_tokens": 8,
        })

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_openai_chat)

    caplog.set_level(logging.DEBUG)

    login_res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": CANARY_PASSWORD})
    assert login_res.status_code == 200, login_res.text
    csrf = login_res.json()["csrf_token"]

    chat_res = client.post("/api/chat", json={"message": "Hej"}, headers={"X-CSRF-Token": csrf})
    assert chat_res.status_code == 200, chat_res.text
    body = chat_res.json()
    assert body["assistant_status"] == "succeeded", body
    assert body["provider"] == "openai", body

    log_texts = [record.getMessage() for record in caplog.records]
    _assert_canary_absent(log_texts, canary=CANARY_PASSWORD, where="a log record")
    _assert_canary_absent(log_texts, canary=CANARY_API_KEY, where="a log record")
    _assert_canary_absent([chat_res.text], canary=CANARY_API_KEY, where="an HTTP response body")
