"""Shared AI-provider (and email) fakes for the browser-based E2E harness.

Applied by BOTH E2E harness processes:
  - run_e2e_backend.py  — the FastAPI/uvicorn web app.
  - run_e2e_worker.py   — the durable worker (app/worker.py).

The worker runs in a SEPARATE process from the web app and is what actually processes Life
Library imports (chunk -> claim-extract -> embed -> index). Without these same fakes in the
worker process, every import job makes a REAL outbound OpenAI call and fails with 401 under
the fake E2E key, so nothing ever finishes importing and every library/knowledge-studio spec
that imports a document then reads it back (library rows, search hits, chat citations) fails.
Keeping the fakes in one module is what stops the two harnesses from silently diverging.

Only outbound AI-provider calls and outbound email are faked — auth, cookies, CSRF, RLS,
rate limiting, the account lifecycle, chunking, storage and the DB are all the genuine
application code, exercised end-to-end.
"""
import json

from app.config import get_settings
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.rag.claims import CLAIM_EXTRACTION_SYSTEM_PROMPT
import app.rag.vector_store as vector_store
import app.rag.retrieve as retrieve_mod

_EMBEDDING_DIM = get_settings().embedding_dim


async def fake_chat(self, messages, model, **kwargs):
    # Claim extraction (app/rag/claims.py's extract_claims_for_document) runs automatically
    # after every successful import — in the WORKER process, not the web app. Detected by the
    # exact system prompt claims.py sends (not a heuristic), so this branch can never misfire
    # against a real chat turn. Echoes the chunk text back as a single claim — deterministic
    # and directly traceable to what was actually indexed, not an invented fact.
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


async def fake_embed(self, texts, model, *, timeout=None):
    # Signature MUST match app/providers/base.py's `embed(self, texts, model, *, timeout=None)`.
    # P1 provider pre-flight verification (app/providers/verification.py) calls
    # `provider.embed([...], model=model, timeout=timeout)`; a fake without the keyword-only
    # `timeout` raised TypeError there, which verify_provider classified as `unreachable`, so
    # every import paused on blocked_provider and never finished — invisible until a spec that
    # actually imports a document (the Library / Founder Knowledge Studio specs, not in CI) ran.
    #
    # Must also match app/config.py's embedding_dim exactly — pgvector's document_chunks.embedding
    # column is a fixed vector(1536) (see the 0004 migration). A shorter fake vector here would
    # raise a dimension mismatch the moment anything actually tried to insert it.
    return [[0.05] * _EMBEDDING_DIM for _ in texts]


_real_search = vector_store.search


def fake_search(db, owner_id, vector, top_k=5, **kwargs):
    """Prefers the REAL search (real DB query, real RLS, real deleted-source exclusion — only
    the embedding vector itself is fake, from fake_embed above) whenever the founder actually
    has real indexed material; only falls back to the fixed fixture below when the library is
    empty. This is what lets a Founder Knowledge Studio E2E test (import a real document, then
    ask MainAI about it) prove a genuine cited answer, not just a UI round-trip against a canned
    response — while auth.spec.ts's baseline flow (which never imports anything) keeps getting
    the same deterministic fixture it always has, since real search legitimately returns nothing
    for an empty library."""
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


def install_provider_fakes() -> None:
    """Patch the outbound AI-provider surface. Safe to call in either harness process."""
    OpenAIProvider.chat = fake_chat
    OpenAIProvider.embed = fake_embed
    vector_store.search = fake_search
    retrieve_mod.search = fake_search


def install_email_fake(email_log_path: str, *, truncate: bool) -> None:
    """Replace app/email.py's real send path with a JSONL capture the Playwright helpers read
    to extract verification/reset links. Only the web-app harness needs this (the worker never
    sends account email); `truncate` clears any previous run's captured emails and must only be
    done by the single process that owns the log, never by a second process started later."""
    import app.routers.auth as auth_router

    if truncate:
        open(email_log_path, "w").close()

    def fake_send_email(to: str, subject: str, body_text: str) -> None:
        with open(email_log_path, "a") as f:
            f.write(json.dumps({"to": to, "subject": subject, "body": body_text}) + "\n")

    auth_router.send_email = fake_send_email
