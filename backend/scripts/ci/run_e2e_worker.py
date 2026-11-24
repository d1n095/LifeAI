"""Runs the REAL durable worker (app/worker.py) with the same provider/search fakes as
run_e2e_backend.py, so Playwright specs that depend on the worker actually claiming and
processing library-import jobs (e.g. founder-knowledge-studio.spec.ts) can pass against a
backend started via run_e2e_backend.py. Not part of the standard CI functional job (which
doesn't run those specs), added here only to drive this fork's own live-browser verification.
"""
import asyncio
import os
import sys

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BACKEND_ROOT)

import scripts.ci.run_e2e_backend  # noqa: F401  (applies the same monkeypatches as a side effect)
from app.worker import _main

if __name__ == "__main__":
    asyncio.run(_main())
