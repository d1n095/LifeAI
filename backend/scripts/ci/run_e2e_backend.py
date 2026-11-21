"""Runs the REAL backend as a live HTTP server for browser-based E2E testing (Playwright,
frontend/e2e/). Only the outbound AI-provider calls and outbound email are faked — no
network calls to a real LLM API or a real mail server. Everything else (auth, cookies, CSRF,
RLS, rate limiting, the account lifecycle) is the genuine application code, exercised
end-to-end through a real Chromium browser driving the real Next.js frontend.

Expects the schema to already be migrated (`alembic upgrade head` — see
.github/workflows/ci.yml and docs/OPERATIONS.md). Deliberately does NOT create tables
itself; that's the whole point of no longer using Base.metadata.create_all in this app.
"""
import asyncio
import hashlib
import json
import math
import os
import re
import sys

# Was `dirname(dirname(...))` (two levels) when this file lived at backend/scripts/ directly;
# now backend/scripts/ci/, one level deeper, so one more dirname() to still land on backend/ —
# this must keep resolving to the backend/ directory (where the `app` package lives) exactly
# like before the move, not a functional change, just preserving the same target after the move.
BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BACKEND_ROOT)

EMAIL_LOG_PATH = os.environ.get("E2E_EMAIL_LOG_PATH", os.path.join(BACKEND_ROOT, "e2e_test_emails.jsonl"))
HOST = os.environ.get("E2E_BACKEND_HOST", "127.0.0.1")
PORT = int(os.environ.get("E2E_BACKEND_PORT", "8010"))

# Truncate any previous run's captured emails.
open(EMAIL_LOG_PATH, "w").close()

from app.config import get_settings
from app.providers.base import ChatResult, ChatStreamChunk
from app.providers.openai_provider import OpenAIProvider
from app.providers.ollama_provider import OllamaProvider
from app.rag.claims import CLAIM_EXTRACTION_SYSTEM_PROMPT
import app.rag.vector_store as vector_store
import app.rag.retrieve as retrieve_mod
import app.routers.auth as auth_router

_EMBEDDING_DIM = get_settings().embedding_dim
# Guarded chat permits local query embedding; its transport alone is faked here.
get_settings().default_embedding_provider = "ollama"
get_settings().default_embedding_model = "nomic-embed-text"


async def fake_chat(self, messages, model, **kwargs):
    # V6 test lab: the planner is a real API path (POST /goals/{id}/plan). The deterministic
    # E2E provider must return a valid plan so the founder-journey acceptance can exercise the
    # approval -> envelope -> execution chain without depending on a model's JSON formatting.
    # This branch is test-only and is never imported by production code.
    if messages and messages[0].role == "system" and messages[0].content.startswith("Du är MainAI:s planerare"):
        return ChatResult(
            content=json.dumps(
                {
                    "tasks": [
                        {
                            "description": "Perform one isolated read-only audit; change no files.",
                            "task_type": "read_only_audit",
                            "depends_on": [],
                            "risk_level": "low",
                            "approval_required": True,
                            "verification_plan": [],
                        }
                    ]
                }
            ),
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 60, "completion_tokens": 20},
        )

    # V6 test lab: the production Supervisor uses provider-assisted planning for an ordinary
    # candidate=None task. Return a valid Safe Planner envelope for the exact read-only scope
    # the acceptance authorized, so the real Supervisor -> Safe Planner -> Driver chain runs
    # instead of parsing a generic conversational reply. Test-only; never imported by app code.
    if messages and messages[0].role == "system" and messages[0].content.startswith(
        "You are a planning specialist"
    ):
        return ChatResult(
            content=json.dumps(
                {
                    "candidate": {
                        "interpretation": "Read the authorized README as a bounded read-only audit.",
                        "requested_outcome": "A read-only inspection with no file changes.",
                        "rationale": "The active envelope authorizes read_file on README.md.",
                        "facts": ["Only README.md is inside the authorized path scope."],
                        "assumptions": [],
                        "unknowns": [],
                        "exclusions": ["No writes, commands, network calls, merges or deploys."],
                        "steps": [
                            {
                                "step_id": "inspect-readme",
                                "purpose": "Read the authorized README as the isolated audit.",
                                "expected_result": "The README content is read without mutation.",
                                "capability": "read_file",
                                "arguments": {"path": "README.md"},
                                "required_risk": "READ_ONLY",
                            }
                        ],
                    },
                    "clarification_required": False,
                    "clarification_question": None,
                    "capability_gaps": [],
                    "useful_components": [],
                    "confidence": 0.9,
                }
            ),
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 90, "completion_tokens": 45},
        )

    # STEG 14: without this branch, EVERY claim extraction call during E2E (app/rag/claims.py's
    # extract_claims_for_document, which runs automatically after every successful import)
    # silently produced zero claims — the generic conversational reply below has no `[...]`
    # JSON array in it, so app/rag/claims.py's _parse_claims() always returned []. That made
    # STEG 10's claim-level trust layer untestable end-to-end through a real browser, only
    # provable at the pytest layer. Detected by the exact system prompt claims.py sends (not
    # a heuristic guess), so the branch can never accidentally misfire against a real chat
    # turn. Echoes the chunk text back as a single claim — deterministic and directly
    # traceable to what was actually indexed, not an invented fact.
    if messages and messages[0].role == "system" and messages[0].content == CLAIM_EXTRACTION_SYSTEM_PROMPT:
        chunk_text = messages[-1].content.strip()
        claim = chunk_text[:200] if chunk_text else ""
        content = json.dumps([claim]) if claim else "[]"
        return ChatResult(content=content, provider="openai", model=model, raw_usage={"prompt_tokens": 40, "completion_tokens": 10})

    return ChatResult(
        content="Hej! Detta ar ett riktigt svar fran den simulerade AI-motorn under E2E-testet. "
        "Kostnadsloggning, Trust Engine och konversationshistorik gar via de riktiga API-vagarna.",
        provider="openai",
        model=model,
        raw_usage={"prompt_tokens": 180, "completion_tokens": 55},
    )


async def fake_stream_chat(self, messages, model, **kwargs):
    """Chunked transport for the streaming E2E path.

    The ordinary browser suite only needs a completed reply. These tests need to observe
    the SSE path itself, so emit the same deterministic answer in three delayed chunks —
    and a deliberately slow answer when the message asks for the cancellation scenario.
    """
    result = await fake_chat(self, messages, model, **kwargs)
    prompt = messages[-1].content if messages else ""
    if "E2E-avbryt" in prompt:
        text = " ".join(["Detta är ett långsamt strömmande svar."] * 24)
        chunks = [text[i : i + 18] for i in range(0, len(text), 18)]
        delay = 0.35
    else:
        text = result.content
        third = max(1, len(text) // 3)
        chunks = [text[:third], text[third : 2 * third], text[2 * third :]]
        delay = 0.25

    for chunk in chunks:
        if not chunk:
            continue
        await asyncio.sleep(delay)
        yield ChatStreamChunk(delta=chunk, provider=result.provider, model=result.model)
    yield ChatStreamChunk(
        provider=result.provider,
        model=result.model,
        raw_usage=result.raw_usage,
        done=True,
    )


def _fake_embedding(text: str) -> list[float]:
    """Deterministic, text-sensitive stand-in for a real embedding model.

    The previous constant vector made every document identical under cosine distance. Once
    the founder library had more than top_k test documents, pgvector's tie order was
    arbitrary and the newest uploaded source could not be cited reliably. Hashing tokens
    into the same fixed-width space preserves the real DB/RLS/pgvector path while making a
    query that shares a unique marker rank that marker's document above unrelated sources.
    Test-only: never imported by production code.
    """
    vector = [0.0] * _EMBEDDING_DIM
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        digest = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(digest[:4], "big") % _EMBEDDING_DIM
        vector[index] += 1.0 if digest[4] & 1 else -1.0
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        vector[0] = 1.0
        return vector
    return [value / norm for value in vector]


async def fake_embed(self, texts, model, **kwargs):
    # Must match app/config.py's embedding_dim exactly — pgvector's document_chunks.embedding
    # column is a fixed vector(1536) (see the 0004 migration), unlike Qdrant's old
    # dynamically-sized collection. A shorter fake vector here would raise a dimension
    # mismatch the moment anything actually tried to insert it.
    #
    # `**kwargs` is required: ensure_verified() calls embed(..., timeout=...) before an import
    # is allowed to proceed. A narrower fake made that pre-flight fail with TypeError and
    # paused every E2E import as blocked_provider.
    return [_fake_embedding(text) for text in texts]


_real_search = vector_store.search


def fake_search(db, owner_id, vector, top_k=5, **kwargs):
    """Prefers the REAL search (real DB query, real RLS, real deleted-source exclusion —
    only the embedding vector itself is fake, from fake_embed above) whenever the founder
    actually has real indexed material; only falls back to the fixed fixture below when the
    library is empty. This is what lets a Founder Knowledge Studio E2E test (import a real
    document, then ask MainAI about it) prove a genuine cited answer, not just a UI
    round-trip against a canned response — while auth.spec.ts's baseline flow (which never
    imports anything) keeps getting the same deterministic fixture it always has, since real
    search legitimately returns nothing for an empty library."""
    real_hits = _real_search(db, owner_id, vector, top_k=top_k, **kwargs)
    if real_hits:
        return real_hits
    return [
        {
            "document_id": "22222222-2222-2222-2222-222222222222",
            "title": "Personalhandbok.pdf",
            "text": "Semantiskt relevant testinnehall for E2E-testet.",
            "score": 0.88,
            "classification": "general",
            "active_truth_status": "active",
            "media_type": None,
            "project_id": None,
        }
    ]


def fake_send_email(to: str, subject: str, body_text: str) -> None:
    """Replaces app/email.py's real send path entirely for this test run — captured to a
    JSONL file that frontend/e2e/helpers.ts reads to extract verification/reset links."""
    with open(EMAIL_LOG_PATH, "a") as f:
        f.write(json.dumps({"to": to, "subject": subject, "body": body_text}) + "\n")


OpenAIProvider.chat = fake_chat
OpenAIProvider.stream_chat = fake_stream_chat
OllamaProvider.embed = fake_embed
vector_store.search = fake_search
retrieve_mod.search = fake_search
auth_router.send_email = fake_send_email

import uvicorn
from app.worker import Worker


async def _serve() -> None:
    """Serve the API and run the durable worker in the same E2E process.

    Uploads intentionally only create a pending `knowledge_import_jobs` row; a separate
    production worker service does the indexing. The browser E2E harness must therefore
    start that worker too, otherwise every import test can only ever observe `pending`.
    This is test-only wiring and does not change the production process split.
    """
    worker = Worker()
    worker_task = asyncio.create_task(worker.run())
    server = uvicorn.Server(
        uvicorn.Config("app.main:app", host=HOST, port=PORT, log_level="warning")
    )
    try:
        await server.serve()
    finally:
        worker.request_shutdown()
        await worker_task


if __name__ == "__main__":
    asyncio.run(_serve())
