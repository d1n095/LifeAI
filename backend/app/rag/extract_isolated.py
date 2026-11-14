"""Process-isolated document extraction for untrusted Founder files.

The durable import worker is long-lived and security-sensitive.  PDF/DOCX/HTML parsers must
therefore not run in that process: malformed input, parser memory growth, or a stuck parser
must terminate only a short-lived child.  The child receives no application secrets and is
bounded by both parent wall-clock timeout and child OS resource limits.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path


_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_MAX_INPUT_BYTES = 25 * 1024 * 1024
_MAX_OUTPUT_BYTES = 50 * 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS = 30.0


class ExtractionIsolationError(RuntimeError):
    """The isolated parser failed without exposing parser internals or host paths."""


def _child_environment() -> dict[str, str]:
    # Intentionally do not inherit DATABASE_URL, provider keys, Founder credentials, proxy
    # variables, PYTHONPATH, or credential-helper configuration.  The absolute interpreter
    # path plus a fixed backend cwd is sufficient to import the application module.
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": "0",
    }


def _safe_child_filename(filename: str) -> str:
    # The parser needs only the extension.  Keeping attacker-controlled directory/name text
    # out of argv also prevents it appearing in process listings and exception diagnostics.
    suffix = Path(filename).suffix.lower()
    return f"input{suffix}"


def extract_text_isolated(
    filename: str,
    raw: bytes,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Extract text in a disposable, resource-bounded subprocess.

    No shell is involved.  Failure, timeout, oversized input/output, invalid UTF-8 output,
    or abnormal termination all fail closed with a generic error.  The original bytes are
    passed read-only over stdin and are never modified.
    """
    if len(raw) > _MAX_INPUT_BYTES:
        raise ExtractionIsolationError("Dokumentet överskrider parserns hårda storleksgräns.")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    command = [
        sys.executable,
        "-m",
        "app.rag.extract_subprocess",
        _safe_child_filename(filename),
    ]
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter/module, no shell or user path
        command,
        cwd=_BACKEND_ROOT,
        env=_child_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=True,
    )
    try:
        stdout, _stderr = process.communicate(input=raw, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise ExtractionIsolationError("Dokumentparsningen överskred tidsgränsen.") from exc

    if process.returncode != 0:
        raise ExtractionIsolationError("Dokumentet kunde inte parsas i den isolerade processen.")
    if len(stdout) > _MAX_OUTPUT_BYTES:
        raise ExtractionIsolationError("Parserresultatet överskrider den hårda storleksgränsen.")
    try:
        return stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ExtractionIsolationError("Parsern returnerade ogiltig textkodning.") from exc
