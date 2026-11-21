"""Regression contracts for the V7 test-lab runner.

These tests are intentionally independent of Docker: they pin the decisions that must never
silently regress when the harness is changed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_f23_requires_actual_elapsed_four_hours():
    mod = _load(
        "v6_reliability_contract",
        ROOT / "backend" / "scripts" / "acceptance" / "v6_reliability.py",
    )
    assert mod.f23_verdict("PASS", 14400, 8) == "BLOCKED"
    assert mod.f23_verdict("PASS", 14400, 14399.9) == "BLOCKED"
    assert mod.f23_verdict("PASS", 14400, 14400) == "PASS"
    assert mod.f23_verdict("FAIL", 14400, 14400) == "FAIL"
    assert mod.f23_verdict("BLOCKED", 14400, 14400) == "BLOCKED"


def test_acceptance_summary_includes_every_failed_leg():
    mod = _load("testlab_contract", ROOT / "scripts" / "testlab" / "testlab.py")
    assert mod.failed_legs([("journey", 0), ("playwright", 1), ("junit", 2)]) == [
        "playwright (exit 1)",
        "junit (exit 2)",
    ]
    assert mod.failed_legs([("journey", 0)]) == []


def test_ollama_untagged_model_accepts_only_latest_tag():
    mod = _load("testlab_models_contract", ROOT / "scripts" / "testlab" / "testlab.py")
    assert mod.model_installed("all-minilm", {"all-minilm:latest"}) is True
    assert mod.model_installed("qwen2.5:0.5b", {"qwen2.5:0.5b"}) is True
    assert mod.model_installed("qwen2.5:0.5b", {"qwen2.5:7b"}) is False
    assert mod.model_installed("all-minilm", {"nomic-embed-text:latest"}) is False
