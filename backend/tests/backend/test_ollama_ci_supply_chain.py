"""Regression guard for the real-local Ollama CI installation boundary."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALLER = REPO_ROOT / "backend/scripts/ci/install_pinned_ollama.sh"
WORKFLOWS = (
    REPO_ROOT / ".github/workflows/real-local-ai-verification.yml",
    REPO_ROOT / ".github/workflows/founder-alpha-verification.yml",
)
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
        assert "bash backend/scripts/ci/install_pinned_ollama.sh" in workflow
        assert "ollama.com/install.sh" not in workflow
        assert "curl -fsSL" not in workflow
