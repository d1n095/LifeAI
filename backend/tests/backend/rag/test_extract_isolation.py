from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from app.rag.extract_isolated import ExtractionIsolationError, extract_text_isolated


def test_plain_text_round_trips_through_real_child_process() -> None:
    raw = b"Founder decision: preserve every source.\n"
    assert extract_text_isolated("../../private/founder-note.txt", raw) == raw.decode()


def test_invalid_docx_fails_closed_without_parser_detail() -> None:
    with pytest.raises(ExtractionIsolationError, match="could not be parsed"):
        extract_text_isolated("broken.docx", b"not-a-docx")


def test_child_receives_no_parent_secrets_or_attacker_filename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 123
        returncode = 0

        def communicate(self, *, input: bytes | None = None, timeout: float | None = None):
            captured["input"] = input
            captured["timeout"] = timeout
            return b"safe", b""

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        captured["shell"] = kwargs.get("shell")
        captured["cwd"] = kwargs["cwd"]
        return FakeProcess()

    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("GIT_ASKPASS", "/tmp/credential-helper")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    assert extract_text_isolated("../../secret-name.txt", b"safe") == "safe"
    assert captured["env"] == {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    assert captured["command"][-1] == "input.txt"
    assert captured["shell"] is None
    assert Path(captured["cwd"]).name == "backend"


def test_timeout_kills_the_whole_child_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[tuple[int, int]] = []

    class HungProcess:
        pid = 456
        returncode = None
        calls = 0

        def communicate(self, *, input: bytes | None = None, timeout: float | None = None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(["python"], timeout)
            self.returncode = -9
            return b"", b""

    process = HungProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    with pytest.raises(ExtractionIsolationError, match="exceeded its time limit"):
        extract_text_isolated("slow.pdf", b"%PDF-", timeout_seconds=0.01)
    assert killed == [(456, 9)]


def test_oversized_input_is_rejected_before_starting_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("child must not start"),
    )
    with pytest.raises(ExtractionIsolationError, match="input limit"):
        extract_text_isolated("large.txt", b"x" * (25 * 1024 * 1024 + 1))
