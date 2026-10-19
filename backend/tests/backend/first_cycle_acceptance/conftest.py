"""Package fixtures. Writes the machine-readable matrix even when tests fail."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PKG_PARENT = str(Path(__file__).resolve().parent.parent)
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from first_cycle_acceptance._git import IsolatedRepo, seed_isolated_repo  # noqa: E402
from first_cycle_acceptance._matrix import (  # noqa: E402
    FAIL,
    write_payload,
    record,
    candidate_sha,
)


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "first_cycle_acceptance: first-cycle adversarial GO/NO-GO harness (no live network)",
    )


# Known remaining gaps this batch must not implement (Codex transport/grants/credentials,
# leftover legacy evaluate_check_runs, TA-12/13 publication). Tests still run and still
# record FAIL in the matrix via conclude(); pytest.fail is an expected xfail so default
# backend CI can stay green without turning those rows into PASS.
REMAINING_OUT_OF_SCOPE = pytest.mark.xfail(
    reason=(
        "Out of this batch: Codex Git transport/grants/worktree credentials, "
        "legacy evaluate_check_runs skipped-as-pass, or TA-12/13 publication"
    ),
    strict=True,
)


def pytest_sessionfinish(session, exitstatus) -> None:
    write_payload()


@pytest.fixture
def isolated_repo(tmp_path) -> IsolatedRepo:
    return seed_isolated_repo(tmp_path)


def conclude(test_id: str, status: str, *, boundary: str, detail: str, production_module: str | None = None):
    row = record(
        test_id,
        status,
        boundary=boundary,
        detail=detail,
        production_module=production_module,
        sha=candidate_sha(),
    )
    if status == FAIL:
        pytest.fail(f"{test_id} [{boundary}] {detail}")
    return row
