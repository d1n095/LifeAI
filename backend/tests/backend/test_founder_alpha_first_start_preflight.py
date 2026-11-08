from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "backend" / "scripts" / "ci" / "verify_founder_alpha_first_start.py"


def _safe_env() -> str:
    return """\
ENVIRONMENT=development
DATABASE_URL=postgresql://postgres:synthetic@127.0.0.1:5432/lifeai_rehearsal
APP_DATABASE_URL=postgresql://mainai_app:synthetic@127.0.0.1:5432/lifeai_rehearsal
REDIS_URL=redis://127.0.0.1:6379/15
SECRET_KEY=not-a-real-secret
FOUNDER_EMAIL=founder@lifeai.invalid
FOUNDER_PASSWORD=not-a-real-password
FRONTEND_ORIGINS=http://127.0.0.1:3020
PUBLIC_APP_URL=http://127.0.0.1:3020
STORAGE_ROOT=/tmp/lifeai-first-start-test/uploads
DEFAULT_LLM_PROVIDER=ollama
DEFAULT_LLM_MODEL=qwen2.5:0.5b
DEFAULT_EMBEDDING_PROVIDER=ollama
DEFAULT_EMBEDDING_MODEL=all-minilm
OLLAMA_BASE_URL=http://127.0.0.1:11434
CHAT_FALLBACK_ORDER=
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GOOGLE_API_KEY=
DEEPSEEK_API_KEY=
OPENROUTER_API_KEY=
GITHUB_WRITE_ENABLED=false
GITHUB_AUTO_MERGE_ENABLED=false
FIRST_START_ISOLATED_REHEARSAL=true
FIRST_START_SYNTHETIC_DATA_ONLY=true
"""


def _run(tmp_path: Path, content: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    path = tmp_path / "rehearsal.env"
    path.write_text(content, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--env-file", str(path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, json.loads(result.stdout)


def test_preflight_validates_safe_config_but_stays_no_go_for_external_reviews(tmp_path: Path):
    result, report = _run(tmp_path, _safe_env())

    assert result.returncode == 1
    assert report["repository_valid"] is True
    assert report["failures"] == []
    assert report["rehearsal_preflight"] == "NO-GO"
    assert report["first_cycle_go"] is False
    assert report["startup_authorized"] is False
    assert report["unknown_required_facts"] == [
        "independent exact-SHA reviews remain unresolved"
    ]


def test_preflight_rejects_remote_provider_and_paid_key_without_leaking_secret(tmp_path: Path):
    secret = "must-not-appear-in-output"
    unsafe = (
        _safe_env()
        .replace("OLLAMA_BASE_URL=http://127.0.0.1:11434", "OLLAMA_BASE_URL=https://ai.example")
        .replace("OPENAI_API_KEY=", f"OPENAI_API_KEY={secret}")
    )
    result, report = _run(tmp_path, unsafe)

    assert result.returncode == 1
    assert "OLLAMA_BASE_URL must use an isolated local host" in "\n".join(report["failures"])
    assert "paid-provider keys must be absent/empty" in report["failures"]
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_preflight_rejects_duplicate_configuration_keys(tmp_path: Path):
    result, report = _run(tmp_path, _safe_env() + "GITHUB_WRITE_ENABLED=true\n")

    assert result.returncode == 1
    assert report["configuration_checked"] is True
    assert any("duplicate key 'GITHUB_WRITE_ENABLED'" in item for item in report["failures"])
