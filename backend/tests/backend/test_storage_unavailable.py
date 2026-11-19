"""A misconfigured STORAGE_ROOT is a dependency outage, not a request bug.

Observed in a real local run: the default /var/lib/lifeai is not writable on macOS, so the first
upload raised PermissionError out of the handler as an opaque 500 while /api/health still said ok.
These tests keep the translation honest and keep the path out of the caller-visible message.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.storage as storage_module
from app.storage import StorageError, get_storage


@pytest.fixture(autouse=True)
def _reset_storage_singleton():
    storage_module._storage = None
    yield
    storage_module._storage = None


def test_unwritable_root_raises_storage_error_without_leaking_path(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a file where the root's parent should be", encoding="utf-8")
    root = blocker / "uploads"
    monkeypatch.setattr(
        "app.config.get_settings", lambda: SimpleNamespace(storage_root=str(root))
    )

    with pytest.raises(StorageError) as excinfo:
        get_storage()

    message = str(excinfo.value)
    assert "STORAGE_ROOT" in message
    assert str(tmp_path) not in message
    assert "PermissionError" not in message


def test_writable_root_is_created_and_cached(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    monkeypatch.setattr(
        "app.config.get_settings", lambda: SimpleNamespace(storage_root=str(root))
    )

    first = get_storage()
    second = get_storage()

    assert root.is_dir()
    assert first is second
