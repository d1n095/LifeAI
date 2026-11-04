"""Regression: backend-tests CI must supply Git identity + frozen supervision SHA.

Level-2 production-flow tests commit into throwaway repos (`git commit -qm base`).
`test_external_frozen_worktree_probe_fails_closed_on_unavailable_seam` needs
immutable commit a7df7f90dba9f8bc993005b2cce1d4c8cb7dcec4, which is NOT an
ancestor of this branch (it lives on origin/codex/mainai-continuous-supervision).
A deeper checkout of HEAD is therefore not enough; the job must fetch that SHA.

This is a file-level contract on `.github/workflows/ci.yml` plus the Level-2
conftest harness. The live proof is the backend-tests job.
"""

from pathlib import Path
import re

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
LEVEL2_CONFTEST = (
    REPO_ROOT / "backend" / "tests" / "backend" / "mainai_level2" / "conftest.py"
)
FROZEN_SUPERVISION_SHA = "a7df7f90dba9f8bc993005b2cce1d4c8cb7dcec4"


def _backend_tests_block() -> str:
    text = CI_YML.read_text()
    match = re.search(
        r"(?ms)^  backend-tests:.*?(?=^  [a-z0-9-]+:|\Z)",
        text,
    )
    assert match, "backend-tests job missing from .github/workflows/ci.yml"
    return match.group(0)


def test_backend_tests_fetches_frozen_supervision_sha_after_checkout():
    block = _backend_tests_block()
    assert FROZEN_SUPERVISION_SHA in block
    assert "git fetch" in block
    checkout = block.find("actions/checkout@")
    fetch = block.find("git fetch --no-tags --depth=1 origin " + FROZEN_SUPERVISION_SHA)
    assert checkout != -1 and fetch != -1
    assert checkout < fetch, "frozen SHA fetch must run after checkout"


def test_level2_harness_sets_deterministic_nonpersonal_git_identity():
    text = LEVEL2_CONFTEST.read_text()
    assert '"GIT_AUTHOR_NAME": "lifeai-ci"' in text
    assert '"GIT_AUTHOR_EMAIL": "ci@lifeai.test"' in text
    assert '"GIT_COMMITTER_NAME": "lifeai-ci"' in text
    assert '"GIT_COMMITTER_EMAIL": "ci@lifeai.test"' in text
    assert "os.environ[key] = value" in text
