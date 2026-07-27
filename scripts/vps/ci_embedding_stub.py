"""CI-only fake embedding provider — never used on the real VPS (docker-compose.vps.yml never
references this file; only docker-compose.vps.ci.yml's `ollama` service does).

Implements just enough of Ollama's REST contract (POST /api/embeddings) for
app/providers/ollama_provider.py's OllamaProvider.embed() to succeed against it, so
.github/workflows/ci.yml's vps-compose-verify job can prove the REAL worker entrypoint
(`python -m app.worker`) actually processes a real upload end to end — pending -> extraction
-> chunks -> indexed -> retrievable — without any real network egress or any real provider
API key. Production's own provider choice (Gemini for chat, OpenAI for embeddings) is
untouched by this: this stub only ever runs inside the CI-only `ollama` service, which
production's docker-compose.vps.yml has no knowledge of.

Deliberately stdlib-only (http.server) — no image build, no dependency install, just
`python /stub/ci_embedding_stub.py` against the base python image already needed for the job.
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
        self.rfile.read(length)  # request body content is irrelevant — always the same fixed-size vector
        if self.path == "/api/embeddings":
            body = json.dumps({"embedding": [0.001] * EMBEDDING_DIM}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 - matches BaseHTTPRequestHandler's signature
        pass  # keep CI logs focused on the actual test assertions, not per-request access logs


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 11434), Handler).serve_forever()
