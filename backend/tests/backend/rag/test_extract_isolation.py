from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.rag import library_import
from app.rag.extract_isolated import ExtractionIsolationError, extract_text_isolated


def test_runtime_import_path_is_bound_to_isolated_extractor() -> None:
    assert library_import.extract_text is extract_text_isolated


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
        stdin = None
        stdout = None

        def poll(self):
            return self.returncode

        def wait(self, *, timeout: float | None = None):
            return self.returncode

        def communicate(self, *, input: bytes | None = None, timeout: float | None = None):
            captured["input"] = input
            captured["timeout"] = timeout
            return b"safe", b""

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        captured["shell"] = kwargs.get("shell")
        captured["cwd"] = kwargs["cwd"]
        captured["stderr"] = kwargs["stderr"]
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
    assert captured["stderr"] is subprocess.DEVNULL
    assert Path(captured["cwd"]).name == "backend"


@pytest.mark.xfail(
    strict=True,
    reason="Same-UID parser child can read parent process environment via procfs.",
)
def test_parser_child_cannot_read_parent_environment_through_procfs() -> None:
    backend_root = Path(__file__).resolve().parents[3]
    canary = "PARSER_PARENT_CANARY=synthetic-value"
    probe = (
        "import os, sys\n"
        "try:\n"
        "    parent_env = open(f'/proc/{os.getppid()}/environ', 'rb').read()\n"
        "except OSError:\n"
        "    sys.stdout.write('denied')\n"
        "else:\n"
        f"    sys.stdout.write({'visible'!r} if {canary.encode()!r} in parent_env else 'denied')\n"
    )
    harness = (
        "import subprocess, sys\n"
        "real_popen = subprocess.Popen\n"
        "def simulated_parser(_command, **kwargs):\n"
        f"    return real_popen([sys.executable, '-c', {probe!r}], **kwargs)\n"
        "subprocess.Popen = simulated_parser\n"
        "from app.rag.extract_isolated import extract_text_isolated\n"
        "sys.stdout.write(extract_text_isolated('probe.txt', b'synthetic input'))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", harness],
        cwd=backend_root,
        env={
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(backend_root),
            canary.partition("=")[0]: canary.partition("=")[2],
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "denied"


def test_stderr_warning_flood_is_discarded_instead_of_buffered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_popen = subprocess.Popen

    def noisy_popen(_command, **kwargs):
        return real_popen(
            [
                sys.executable,
                "-c",
                "import os; "
                "[os.write(2, b'x' * 4096) for _ in range(2048)]; "
                "os.write(1, b'parsed')",
            ],
            **kwargs,
        )

    monkeypatch.setattr(subprocess, "Popen", noisy_popen)

    assert extract_text_isolated("warning-flood.pdf", b"%PDF-", timeout_seconds=10) == "parsed"


def test_timeout_kills_the_whole_child_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[tuple[int, int]] = []

    class HungProcess:
        pid = 456
        returncode = None
        stdin = None
        stdout = None
        calls = 0

        def poll(self):
            return self.returncode

        def communicate(self, *, input: bytes | None = None, timeout: float | None = None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(["python"], timeout)
            self.returncode = -9
            return b"", b""

        def wait(self, *, timeout: float | None = None):
            self.returncode = -9
            return self.returncode

        def kill(self):
            self.returncode = -9

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


def test_production_fails_closed_when_isolated_parser_is_unconfigured(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("PARSER_SOCKET_PATH", raising=False)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("production must not run a same-container parser"),
    )

    with pytest.raises(ExtractionIsolationError, match="not configured"):
        extract_text_isolated("input.txt", b"secret document")
