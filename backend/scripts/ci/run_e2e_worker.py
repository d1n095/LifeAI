"""Runs the REAL durable worker (app/worker.py) for browser-based E2E testing, with the same
outbound-AI/email fakes run_e2e_backend.py applies to the web app (see _e2e_fakes.py).

The worker — not the web app — is what actually processes Life Library imports (chunk ->
claim-extract -> embed -> index) and the storage/MainAI job queues. It runs in its own
process, so run_e2e_backend.py's in-process monkeypatches do NOT reach it: without this
harness every import job makes a real outbound OpenAI embedding/chat call and fails with 401
under the fake E2E key, leaving imports stuck in the queue forever. That is exactly why the
Life Library / Founder Knowledge Studio specs (import a document, then read it back as library
rows / search hits / chat citations) require this worker alongside run_e2e_backend.py.

Expects the schema to already be migrated and the restricted role provisioned, exactly like
run_e2e_backend.py, and must be pointed at the SAME database and the SAME STORAGE_ROOT as the
web-app harness (the blob written by the API must be readable by the worker — see
app/storage/local_fs.py and app/config.py's storage_root).
"""
import asyncio
import os
import sys

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BACKEND_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _e2e_fakes import install_provider_fakes

# Only the provider fakes — the worker never sends account email, and must NEVER truncate the
# captured-email log the web-app harness owns.
install_provider_fakes()

from app.worker import _main

if __name__ == "__main__":
    asyncio.run(_main())
