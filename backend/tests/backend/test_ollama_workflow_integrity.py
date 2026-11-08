"""The local-model workflows must verify the release before unpacking it."""

import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
VERSION = "v0.40.15"
DIGEST = "628c018404a4206493efd38e46251c154ede5141b8b8dc85c659b3b027c47379"


WORKFLOWS = ["founder-alpha-verification.yml", "real-local-ai-verification.yml"]


def _install_step(workflow):
    source = (ROOT / ".github" / "workflows" / workflow).read_text()
    install = source.split("- name: Install and start Ollama; pull the test models", 1)[1]
    return install.split("\n      - name:", 1)[0]


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_ollama_release_is_pinned_and_checked_before_extraction(workflow):
    install = _install_step(workflow)
    assert f"/releases/download/{VERSION}/ollama-linux-amd64.tar.zst" in install
    assert DIGEST in install
    assert install.index("sha256sum --check") < install.index("tar --zstd -xf")
    assert "ollama.com/install.sh" not in install
    assert not re.search(r"\|\s*(?:sh|bash)(?:\s|$)", install)


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_corrupt_release_stops_before_extraction(workflow, tmp_path):
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    curl = fakebin / "curl"
    curl.write_text(
        '#!/bin/sh\nwhile [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--output" ]; then shift; printf corrupt > "$1"; exit 0; fi\n'
        "  shift\ndone\nexit 1\n"
    )
    curl.chmod(0o755)
    tar = fakebin / "tar"
    tar.write_text(f"#!/bin/sh\ntouch '{tmp_path / 'extracted'}'\n")
    tar.chmod(0o755)
    script = textwrap.dedent(_install_step(workflow).split("run: |", 1)[1])
    env = {**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}", "RUNNER_TEMP": str(tmp_path)}
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "FAILED" in result.stdout
    assert not (tmp_path / "extracted").exists()
