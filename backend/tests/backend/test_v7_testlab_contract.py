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


def test_fake_embedding_ranks_marker_source_above_unrelated_source():
    mod = _load(
        "fake_embedding_contract",
        ROOT / "backend" / "scripts" / "ci" / "fake_embedding.py",
    )
    marker = "V6LAB-KNOWLEDGE-abc123def456"
    query = mod.text_embedding(f"Var namns {marker}? Svara kort och citera kallan.", 1536)
    own = mod.text_embedding(
        f"LifeAI V6 isolated acceptance source. {marker} ar en unik markor.",
        1536,
    )
    unrelated = mod.text_embedding(
        "LifeAI V6 isolated acceptance source. En helt annan markor for ett annat dokument.",
        1536,
    )
    assert len(query) == 1536
    assert query == mod.text_embedding(
        f"Var namns {marker}? Svara kort och citera kallan.", 1536
    )
    own_similarity = sum(left * right for left, right in zip(query, own))
    unrelated_similarity = sum(left * right for left, right in zip(query, unrelated))
    assert own_similarity > unrelated_similarity
