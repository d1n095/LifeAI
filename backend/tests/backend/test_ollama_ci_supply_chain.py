"""Regression guard for the real-local Ollama CI installation boundary."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALLER = REPO_ROOT / "backend/scripts/ci/install_pinned_ollama.sh"
REAL_LOCAL_WORKFLOW = REPO_ROOT / ".github/workflows/real-local-ai-verification.yml"
FOUNDER_ALPHA_WORKFLOW = REPO_ROOT / ".github/workflows/founder-alpha-verification.yml"
WORKFLOWS = (REAL_LOCAL_WORKFLOW, FOUNDER_ALPHA_WORKFLOW)
EXPECTED_VERSION = "v0.40.15"
EXPECTED_SHA256 = "628c018404a4206493efd38e46251c154ede5141b8b8dc85c659b3b027c47379"


def test_real_local_workflows_use_only_checksum_pinned_ollama_artifact():
    installer = INSTALLER.read_text()
    assert f'OLLAMA_VERSION="{EXPECTED_VERSION}"' in installer
    assert f'OLLAMA_ARCHIVE_SHA256="{EXPECTED_SHA256}"' in installer
    assert "https://github.com/ollama/ollama/releases/download/${OLLAMA_VERSION}/${OLLAMA_ARCHIVE}" in installer
    assert "sha256sum --check --strict" in installer
    assert "ollama.com/install.sh" not in installer

    for workflow_path in WORKFLOWS:
        workflow = workflow_path.read_text()
        assert "ollama.com/install.sh" not in workflow
        assert "curl -fsSL" not in workflow

    founder_alpha = FOUNDER_ALPHA_WORKFLOW.read_text()
    assert "bash candidate/backend/scripts/ci/install_pinned_ollama.sh" in founder_alpha

    real_local = REAL_LOCAL_WORKFLOW.read_text()
    assert "Check out the exact CI policy SHA" in real_local
    assert "ref: ${{ github.sha }}" in real_local
    assert 'test "$(git -C ci-policy rev-parse HEAD)" = "$GITHUB_SHA"' in real_local
    assert "bash ci-policy/backend/scripts/ci/install_pinned_ollama.sh" in real_local
