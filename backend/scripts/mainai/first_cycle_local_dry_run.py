#!/usr/bin/env python3
"""Local-only first-cycle dry run wrapper.

Does not talk to GitHub, does not enable paid providers, does not claim GO.
Runs the deterministic pytest that drives production execution_job with a synthetic chat.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[2]


def main() -> int:
    if os.environ.get("GITHUB_WRITE_ENABLED", "").lower() in {"1", "true", "yes"}:
        print("Refusing: GITHUB_WRITE_ENABLED is set. Local-only dry run only.", file=sys.stderr)
        return 2
    if os.environ.get("MAINAI_ACTIVATION_MODE", "").lower() in {"1", "true", "yes"}:
        print("Refusing: MAINAI_ACTIVATION_MODE is set.", file=sys.stderr)
        return 2
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/backend/test_first_cycle_ops_readiness_p2.py::test_founder_supervised_first_cycle_dry_run_stops_before_merge",
        "-q",
    ]
    print("Running Founder-supervised first-cycle dry run (synthetic provider, STOP-before-merge, go=false)")
    result = subprocess.run(cmd, cwd=str(_BACKEND))
    if result.returncode == 0:
        print("STOP-before-merge. go=false. No merge, deploy, Recall, or paid calls.")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
