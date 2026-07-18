"""Runs the REAL backend as a live HTTP server for browser-based E2E testing (Playwright,
frontend/e2e/). Only the outbound AI-provider calls and outbound email are faked — no
network calls to a real LLM API or a real mail server. Everything else (auth, cookies, CSRF,
RLS, rate limiting, the account lifecycle) is the genuine application code, exercised
end-to-end through a real Chromium browser driving the real Next.js frontend.

Expects the schema to already be migrated (`alembic upgrade head` — see
.github/workflows/ci.yml and docs/OPERATIONS.md). Deliberately does NOT create tables
itself; that's the whole point of no longer using Base.metadata.create_all in this app.
"""
import json
import os
import sys

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_ROOT)

EMAIL_LOG_PATH = os.environ.get("E2E_EMAIL_LOG_PATH", os.path.join(BACKEND_ROOT, "e2e_test_emails.jsonl"))
HOST = os.environ.get("E2E_BACKEND_HOST", "127.0.0.1")
PORT = int(os.environ.get("E2E_BACKEND_PORT", "8010"))

# Truncate any previous run's captured emails.
open(EMAIL_LOG_PATH, "w").close()

from app.config import get_settings
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
import app.rag.vector_store as vector_store
import app.rag.retrieve as retrieve_mod
import app.routers.auth as auth_router

_EMBEDDING_DIM = get_settings().embedding_dim


async def fake_chat(self, messages, model, **kwargs):
    return ChatResult(
        content="Hej! Detta ar ett riktigt svar fran den simulerade AI-motorn under E2E-testet. "
        "Kostnadsloggning, Trust Engine och konversationshistorik gar via de riktiga API-vagarna.",
        provider="openai",
        model=model,
        raw_usage={"prompt_tokens": 180, "completion_tokens": 55},
    )


async def fake_embed(self, texts, model):
    # Must match app/config.py's embedding_dim exactly — pgvector's document_chunks.embedding
    # column is a fixed vector(1536) (see the 0004 migration), unlike Qdrant's old
    # dynamically-sized collection. A shorter fake vector here would raise a dimension
    # mismatch the moment anything actually tried to insert it.
    return [[0.05] * _EMBEDDING_DIM for _ in texts]


def fake_search(db, owner_id, vector, top_k=5):
    return [
        {
            "document_id": "22222222-2222-2222-2222-222222222222",
            "title": "Personalhandbok.pdf",
            "text": "Semantiskt relevant testinnehall for E2E-testet.",
            "score": 0.88,
        }
    ]


def fake_send_email(to: str, subject: str, body_text: str) -> None:
    """Replaces app/email.py's real send path entirely for this test run — captured to a
    JSONL file that frontend/e2e/helpers.ts reads to extract verification/reset links."""
    with open(EMAIL_LOG_PATH, "a") as f:
        f.write(json.dumps({"to": to, "subject": subject, "body": body_text}) + "\n")


OpenAIProvider.chat = fake_chat
OpenAIProvider.embed = fake_embed
vector_store.search = fake_search
retrieve_mod.search = fake_search
auth_router.send_email = fake_send_email

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="warning")
