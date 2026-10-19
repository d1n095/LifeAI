"""Matrix schema and candidate SHA identity."""

from __future__ import annotations

from pathlib import Path

from first_cycle_acceptance._matrix import ALLOWED, candidate_sha, rows, write_payload
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


def test_matrix_is_not_written_into_the_source_tree_by_default(tmp_path, monkeypatch):
    repo_copy = Path(__file__).resolve().parents[4] / "docs" / "examiner" / "first_cycle_acceptance_matrix.json"
    existed = repo_copy.exists()
    before = repo_copy.read_text(encoding="utf-8") if existed else None
    monkeypatch.delenv("FIRST_CYCLE_ACCEPTANCE_WRITE_REPO_COPY", raising=False)
    target = write_payload(tmp_path / "matrix.json")
    assert target.is_file()
    still = repo_copy.exists()
    unchanged = (not existed and not still) or (existed and repo_copy.read_text(encoding="utf-8") == before)
    conclude(
        "MATRIX-3 default matrix write stays out of the source tree",
        ST_PASS if unchanged else "FAIL",
        boundary="harness.matrix.repo_copy",
        detail=f"repo_copy_existed={existed}; unchanged={unchanged}; artifact={target}",
    )
    assert unchanged
