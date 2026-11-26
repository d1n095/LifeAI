"""Chat-suite fixtures that need to control the backend lifespan explicitly."""

from __future__ import annotations

from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def restartable_client():
    """Factory that starts a fresh backend lifespan and closes the previous one first.

    Two TestClient lifespans must never overlap: the second startup waits on resources the
    first one still owns, which deadlocks at ``TestClient.__enter__``. Closing the previous
    client before opening the next is also exactly what a real backend restart does, so this
    is the honest way to test restart survival rather than nesting two live lifespans.
    """

    stack = ExitStack()

    def _restart() -> TestClient:
        nonlocal stack
        stack.close()
        stack = ExitStack()
        return stack.enter_context(TestClient(app, base_url="https://testserver"))

    yield _restart
    stack.close()
