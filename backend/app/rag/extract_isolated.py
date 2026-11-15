"""Process-isolated document extraction for untrusted Founder files.

The durable import worker is long-lived and security-sensitive. PDF/DOCX/HTML parsers must
therefore not run in that process: malformed input, parser memory growth, or a stuck parser
must terminate only a short-lived child. The child receives no application secrets and is
bounded by both parent wall-clock timeout and child OS resource limits.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import httpx


_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SOCKET_PATH = "/run/lifeai-parser/parser.sock"
_MAX_INPUT_BYTES = 25 * 1024 * 1024
_MAX_OUTPUT_BYTES = 50 * 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS = 30.0


class ExtractionIsolationError(RuntimeError):
    """The isolated parser failed without exposing parser internals or host paths."""


class ParserServiceUnavailableError(ExtractionIsolationError, ConnectionError):
    """The configured parser transport is unavailable and may recover on retry."""


def _child_environment() -> dict[str, str]:
    # Intentionally do not inherit database URLs, provider keys, Founder credentials,
    # proxy variables, Python paths, or credential-helper configuration.
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _safe_child_filename(filename: str) -> str:
    # The parser needs only the extension. Keeping attacker-controlled directory/name text
    # out of argv also prevents it appearing in process listings and diagnostics.
    return f"input{Path(filename).suffix.lower()}"


def _extract_via_socket(filename: str, raw: bytes, socket_path: str, timeout_seconds: float) -> str:
    suffix = Path(filename).suffix.lower() or ".txt"
    transport = httpx.HTTPTransport(uds=socket_path)
    try:
        with httpx.Client(transport=transport, timeout=timeout_seconds) as client:
            with client.stream(
                "POST",
                "http://parser/v1/extract",
                headers={"x-document-suffix": suffix},
                content=raw,
            ) as response:
                if response.status_code != 200:
                    raise ExtractionIsolationError("Document could not be parsed in the isolated service.")
                output = bytearray()
                for chunk in response.iter_bytes():
                    if len(output) + len(chunk) > _MAX_OUTPUT_BYTES:
                        raise ExtractionIsolationError("Parser output exceeds the hard size limit.")
                    output.extend(chunk)
    except httpx.HTTPError as exc:
        raise ParserServiceUnavailableError("Isolated parser service is unavailable.") from exc
    try:
        return output.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ExtractionIsolationError("Parser returned invalid text encoding.") from exc


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    for pipe in (process.stdin, process.stdout):
        if pipe is not None:
            pipe.close()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def extract_text_in_subprocess(
    filename: str,
    raw: bytes,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Run the parser process locally; used only inside the secretless parser service."""
    if len(raw) > _MAX_INPUT_BYTES:
        raise ExtractionIsolationError("Document exceeds the parser input limit.")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    process = subprocess.Popen(  # noqa: S603 - fixed interpreter/module, no shell or user path
        [sys.executable, "-m", "app.rag.extract_subprocess", _safe_child_filename(filename)],
        cwd=_BACKEND_ROOT,
        env=_child_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    try:
        stdout, _ = process.communicate(input=raw, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(process)
        raise ExtractionIsolationError("Document parser exceeded its time limit.") from exc
    finally:
        if process.poll() is None:
            _kill_process_group(process)

    if process.returncode != 0:
        raise ExtractionIsolationError("Document could not be parsed in the isolated process.")
    if len(stdout) > _MAX_OUTPUT_BYTES:
        raise ExtractionIsolationError("Parser output exceeds the hard size limit.")
    try:
        return stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ExtractionIsolationError("Parser returned invalid text encoding.") from exc


def extract_text_isolated(
    filename: str,
    raw: bytes,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Use the secretless parser service when configured, otherwise use local isolation in dev.

    Production deliberately fails closed if the dedicated service is unavailable or
    unconfigured. No shell is used and source bytes are passed only over stdin or a UDS.
    """
    if len(raw) > _MAX_INPUT_BYTES:
        raise ExtractionIsolationError("Document exceeds the parser input limit.")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    socket_path = os.environ.get("PARSER_SOCKET_PATH")
    if socket_path:
        return _extract_via_socket(filename, raw, socket_path, timeout_seconds)
    if os.environ.get("ENVIRONMENT") == "production":
        raise ExtractionIsolationError("Isolated parser service is not configured.")
    return extract_text_in_subprocess(filename, raw, timeout_seconds=timeout_seconds)
