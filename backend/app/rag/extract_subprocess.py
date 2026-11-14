"""Short-lived entry point for process-isolated document extraction."""

from __future__ import annotations

import resource
import sys


_MAX_INPUT_BYTES = 25 * 1024 * 1024
_MAX_OUTPUT_BYTES = 50 * 1024 * 1024
_MAX_ADDRESS_SPACE_BYTES = 768 * 1024 * 1024
_MAX_CPU_SECONDS = 20
_MAX_OPEN_FILES = 64


def _apply_limits() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (_MAX_ADDRESS_SPACE_BYTES, _MAX_ADDRESS_SPACE_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (_MAX_CPU_SECONDS, _MAX_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_NOFILE, (_MAX_OPEN_FILES, _MAX_OPEN_FILES))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))


def main() -> int:
    if len(sys.argv) != 2:
        return 64
    try:
        _apply_limits()
        raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
        if len(raw) > _MAX_INPUT_BYTES:
            return 65

        # Import parser libraries only after the resource boundary is active.
        from app.rag.extract import extract_text

        output = extract_text(sys.argv[1], raw).encode("utf-8", errors="strict")
        if len(output) > _MAX_OUTPUT_BYTES:
            return 66
        sys.stdout.buffer.write(output)
        return 0
    except BaseException as exc:  # noqa: BLE001 - child must convert every parser failure
        # Only the exception type crosses the boundary; never paths, bytes, environment
        # values, or parser-controlled messages.
        sys.stderr.write(type(exc).__name__ + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
