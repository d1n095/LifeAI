#!/usr/bin/env python3
"""Verify the Founder Alpha composition against its immutable source manifest.

This validates composition identity, not security certification. A correctly composed branch
can pass while remaining release-blocked; `release_ready` is false until every external review
and Founder gate recorded in the manifest has been satisfied.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "docs" / "founder_alpha_integration_manifest.json"


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=False
    )


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    head = _git("rev-parse", "HEAD")
    failures: list[str] = []
    if head.returncode:
        failures.append(f"cannot resolve HEAD: {head.stderr.strip()}")
        head_sha = "unknown"
    else:
        head_sha = head.stdout.strip()

    ancestry: list[dict[str, object]] = []
    for component in manifest["components"]:
        sha = component["sha"]
        result = _git("merge-base", "--is-ancestor", sha, "HEAD")
        present = result.returncode == 0
        ancestry.append(
            {
                "name": component["name"],
                "pr": component["pr"],
                "sha": sha,
                "present": present,
            }
        )
        if not present:
            failures.append(f"missing required source PR #{component['pr']} @ {sha}")

    alembic = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=ROOT / "backend",
        text=True,
        capture_output=True,
        check=False,
    )
    expected_head = manifest["expected_migration_head"]
    actual_heads = [line.split()[0] for line in alembic.stdout.splitlines() if line.strip()]
    if alembic.returncode:
        failures.append(f"alembic heads failed: {alembic.stderr.strip()}")
    elif actual_heads != [expected_head]:
        failures.append(
            f"migration heads {actual_heads!r} do not equal the required single head "
            f"{expected_head!r}"
        )

    if manifest.get("first_cycle_go") is not False:
        failures.append("manifest must remain fail-closed with first_cycle_go=false")
    if manifest.get("release_candidate") is not False:
        failures.append("this rehearsal must not claim release-candidate status")
    if not manifest.get("release_blockers"):
        failures.append("release blockers must remain explicit until external gates close")

    report = {
        "schema_version": manifest["schema_version"],
        "head": head_sha,
        "composition_valid": not failures,
        "release_ready": False,
        "first_cycle_go": False,
        "expected_migration_head": expected_head,
        "actual_migration_heads": actual_heads,
        "ancestry": ancestry,
        "release_blockers": manifest["release_blockers"],
        "failures": failures,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
