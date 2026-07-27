"""CI-only fake LLM+embedding provider — never used on the real VPS (docker-compose.vps.yml
never references this file; only docker-compose.vps.ci.yml's `ollama` service does).

Implements just enough of Ollama's REST contract (POST /api/embeddings, POST /api/chat) for
app/providers/ollama_provider.py's OllamaProvider to succeed against it, so
.github/workflows/ci.yml's vps-compose-verify job can prove the REAL worker entrypoint
(`python -m app.worker`) AND the REAL chat endpoint (`POST /api/chat`) work end to end —
upload -> extraction -> chunks -> indexed -> retrievable -> cited in a chat reply — without
any real network egress or any real provider API key. Production's own provider choice
(Gemini for chat, OpenAI for embeddings) is untouched by this: this stub only ever runs
inside the CI-only `ollama` service, which production's docker-compose.vps.yml has no
knowledge of.

Originally named ci_embedding_stub.py (embeddings only) — renamed when the round-trip CI test
was extended to also cover chat-with-citations (2026-07-27, MainAI Core Loop v1), since it now
answers both roles.

Deliberately stdlib-only (http.server) — no image build, no dependency install, just
`python /stub/ci_provider_stub.py` against the base python image already needed for the job.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

# Matches app/config.py's Settings.embedding_dim default (1536) — the pgvector column's fixed
# dimension. The actual values are irrelevant to this test (it never asserts semantic
# similarity, only that the pipeline completes and the uploaded text is retrievable via the
# text-match channel), but the DIMENSION must match or pgvector would reject the insert.
EMBEDDING_DIM = 1536


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        if self.path == "/api/embeddings":
            self._embeddings()
        elif self.path == "/api/chat":
            self._chat(raw)
        else:
            self.send_response(404)
            self.end_headers()

    def _embeddings(self):
        # Request body content is irrelevant here — always the same fixed-size vector. Real
        # semantic similarity is never asserted by the CI test this stub serves; only that the
        # pipeline completes and the uploaded text is retrievable via the text-match channel.
        body = json.dumps({"embedding": [0.001] * EMBEDDING_DIM}).encode("utf-8")
        self._respond(body)

    def _chat(self, raw: bytes):
        # Echoes the SYSTEM message back inside the reply, prefixed, instead of returning a
        # fixed canned string. app/routers/chat.py builds the system message as
        # "{SYSTEM_PROMPT}\n\nKONTEXT:\n{context_block}\n\n...", where context_block contains
        # the actual retrieved chunk text — so grepping the reply for the CI test's unique
        # marker word proves the retrieved chunk really was threaded through retrieve_context()
        # -> the chat message list -> this HTTP call -> ChatResult.content -> the persisted
        # assistant Message row -> the API response, not just that `sources` (built
        # independently from `hits`, see chat.py) happens to be populated.
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {}
        messages = payload.get("messages", [])
        system_content = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
        reply = f"[ci-stub svar baserat på kontext] {system_content}"
        body = json.dumps(
            {
                "message": {"role": "assistant", "content": reply},
                "prompt_eval_count": 1,
                "eval_count": 1,
            }
        ).encode("utf-8")
        self._respond(body)

    def _respond(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - matches BaseHTTPRequestHandler's signature
        pass  # keep CI logs focused on the actual test assertions, not per-request access logs


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 11434), Handler).serve_forever()
