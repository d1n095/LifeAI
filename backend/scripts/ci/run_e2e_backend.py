"""Runs the REAL backend as a live HTTP server for browser-based E2E testing (Playwright,
frontend/e2e/). Only the outbound AI-provider calls and outbound email are faked — no
network calls to a real LLM API or a real mail server. Everything else (auth, cookies, CSRF,
RLS, rate limiting, the account lifecycle) is the genuine application code, exercised
end-to-end through a real Chromium browser driving the real Next.js frontend.

Expects the schema to already be migrated (`alembic upgrade head` — see
.github/workflows/ci.yml and docs/OPERATIONS.md). Deliberately does NOT create tables
itself; that's the whole point of no longer using Base.metadata.create_all in this app.

The AI-provider/email fakes live in _e2e_fakes.py and are shared with run_e2e_worker.py: the
durable worker processes Life Library imports in a SEPARATE process and needs the identical
fakes, or every import job hits the real OpenAI API and fails 401 under the fake E2E key. See
that module's docstring.
"""
import os
import sys

# Was `dirname(dirname(...))` (two levels) when this file lived at backend/scripts/ directly;
# now backend/scripts/ci/, one level deeper, so one more dirname() to still land on backend/ —
# this must keep resolving to the backend/ directory (where the `app` package lives).
BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BACKEND_ROOT)
# This script's own directory, so `import _e2e_fakes` (the sibling shared-fakes module)
# resolves whether run via `python scripts/ci/run_e2e_backend.py` or as a module.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

EMAIL_LOG_PATH = os.environ.get("E2E_EMAIL_LOG_PATH", os.path.join(BACKEND_ROOT, "e2e_test_emails.jsonl"))
HOST = os.environ.get("E2E_BACKEND_HOST", "127.0.0.1")
PORT = int(os.environ.get("E2E_BACKEND_PORT", "8010"))

from _e2e_fakes import install_email_fake, install_provider_fakes  # noqa: E402

install_provider_fakes()
# This web-app process owns the captured-email log; truncate any previous run's emails here.
# The worker harness never touches the email log (truncate would wipe emails this process is
# still capturing — see _e2e_fakes.install_email_fake).
install_email_fake(EMAIL_LOG_PATH, truncate=True)

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="warning")
