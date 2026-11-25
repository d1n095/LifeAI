"""Captured PR metadata is a dated observation, not current remote state."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from app.context_atlas.runtime import SystemIntelligence

REPO_ROOT = Path(__file__).resolve().parents[2]


def _head_sha() -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _capture(tmp_path: Path, *, state: str) -> Path:
    path = tmp_path / "pull_requests.json"
    path.write_text(
        json.dumps(
            [
                {
                    "number": 999,
                    "state": state,
                    "head": {"ref": "agent/b", "sha": _head_sha()},
                    "base": {"ref": "main"},
                }
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_stale_capture_is_marked_and_unknown_state_is_named(tmp_path):
    captured = _capture(tmp_path, state="unknown")
    old = time.time() - 72 * 3600
    os.utime(captured, (old, old))

    status = SystemIntelligence(
        REPO_ROOT, pull_requests_path=captured
    ).integration_status()

    assert status.state == "captured_pr"
    assert status.capture_stale is True
    assert status.capture_age_hours is not None and status.capture_age_hours >= 71
    assert any("may have changed" in note for note in status.notes)
    assert any("#999" in note and "unknown" in note for note in status.notes)
    assert status.to_dict()["capture_stale"] is True
    assert status.execution_authorized is False


def test_fresh_capture_is_not_stale(tmp_path):
    captured = _capture(tmp_path, state="open")

    status = SystemIntelligence(
        REPO_ROOT, pull_requests_path=captured
    ).integration_status()

    assert status.state == "captured_pr"
    assert status.capture_stale is False
    assert not any("may have changed" in note for note in status.notes)
    assert status.execution_authorized is False
