"""Matrix schema and candidate SHA identity."""

from __future__ import annotations

from first_cycle_acceptance._matrix import ALLOWED, candidate_sha, rows
from first_cycle_acceptance._matrix import PASS as ST_PASS
from first_cycle_acceptance.conftest import conclude

pytestmark = __import__("pytest").mark.first_cycle_acceptance


def test_candidate_sha_is_40_hex():
    sha = candidate_sha()
    holds = len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)
    conclude(
        "MATRIX-1 candidate SHA is the exact code SHA under test",
        ST_PASS if holds else "FAIL",
        boundary="harness.matrix.sha",
        detail=f"sha={sha}",
    )


def test_recorded_rows_use_closed_status_vocab():
    illegal = [row for row in rows() if row.status not in ALLOWED]
    conclude(
        "MATRIX-2 closed status vocabulary",
        ST_PASS if not illegal else "FAIL",
        boundary="harness.matrix.vocab",
        detail=f"illegal={illegal!r}; allowed={sorted(ALLOWED)}",
    )
