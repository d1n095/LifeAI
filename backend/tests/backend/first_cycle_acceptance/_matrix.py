"""Machine-readable GO/NO-GO matrix. Status vocabulary is closed:

PASS — production (or bound production helper) met the acceptance bar.
FAIL — production was exercised and did not meet the bar.
BLOCKED — required production interface is not present (do not invent compatibility).
UNKNOWN — Founder/GitHub evidence outside this process is required.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

# Never persist raw credential fill output or GitHub token prefixes into the matrix.
_SECRETISH = re.compile(
    r"(?i)(password=)\S+|(authorization=)\S+|\b(gho_|ghp_|ghu_|ghs_|github_pat_)[A-Za-z0-9_]+\b"
)


def sanitize_matrix_text(text: str) -> str:
    if not text:
        return ""
    return _SECRETISH.sub(lambda m: (m.group(1) or "") + "<redacted>" if m.group(1) else "<redacted>", text)

PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
UNKNOWN = "UNKNOWN"
ALLOWED = frozenset({PASS, FAIL, BLOCKED, UNKNOWN})

_LOCK = Lock()
_ROWS: list["MatrixRow"] = []


def candidate_sha() -> str:
    override = os.environ.get("FIRST_CYCLE_ACCEPTANCE_SHA")
    if override:
        return override.strip()
    root = Path(__file__).resolve().parents[4]
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return (out.stdout or "unknown").strip()


@dataclass
class MatrixRow:
    test_id: str
    status: str
    boundary: str
    candidate_sha: str
    detail: str
    production_module: str | None = None
    recorded_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        if self.status not in ALLOWED:
            raise ValueError(f"illegal matrix status {self.status!r}")


def record(
    test_id: str,
    status: str,
    *,
    boundary: str,
    detail: str,
    production_module: str | None = None,
    sha: str | None = None,
) -> MatrixRow:
    row = MatrixRow(
        test_id=test_id,
        status=status,
        boundary=boundary,
        candidate_sha=sha or candidate_sha(),
        detail=sanitize_matrix_text(detail),
        production_module=production_module,
    )
    with _LOCK:
        _ROWS.append(row)
    return row


def rows() -> list[MatrixRow]:
    with _LOCK:
        return list(_ROWS)


def as_payload() -> dict:
    collected = rows()
    counts = {key: 0 for key in (PASS, FAIL, BLOCKED, UNKNOWN)}
    for row in collected:
        counts[row.status] += 1
    go = counts[FAIL] == 0 and counts[BLOCKED] == 0 and counts[UNKNOWN] == 0 and counts[PASS] > 0
    return {
        "schema": "mainai.first_cycle.acceptance_matrix/v1",
        "candidate_sha": candidate_sha(),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "go": go,
        "counts": counts,
        "rows": [asdict(row) for row in collected],
    }


def write_payload(path: Path | None = None) -> Path:
    target = path or Path(os.environ.get(
        "FIRST_CYCLE_ACCEPTANCE_MATRIX",
        "/opt/cursor/artifacts/first_cycle_acceptance_matrix.json",
    ))
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = as_payload()
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # Never write the matrix into the source tree by default. A previous copy next to the
    # harness docs recorded live `git credential fill` output from developer machines.
    if os.environ.get("FIRST_CYCLE_ACCEPTANCE_WRITE_REPO_COPY") == "1":
        repo_copy = Path(__file__).resolve().parents[4] / "docs" / "examiner" / "first_cycle_acceptance_matrix.json"
        repo_copy.parent.mkdir(parents=True, exist_ok=True)
        repo_copy.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target
