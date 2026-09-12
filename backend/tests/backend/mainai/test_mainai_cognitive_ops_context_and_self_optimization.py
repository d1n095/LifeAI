"""`app.mainai_cognitive_ops.context_packaging` + `compression` + `self_optimizing_context`. See
docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md."""

from __future__ import annotations

import os

import pytest

from app.mainai_cognitive_ops.compression import RecoveryCheckpoint, parse_handoff_markdown, validate_checkpoint_reconstructability
from app.mainai_cognitive_ops.context_packaging import build_code_debug_package, build_founder_decision_package
from app.mainai_cognitive_ops.self_optimizing_context import ContextStrategyMetrics, evaluate_strategy_change
from app.mainai_cognitive_ops.types import CognitiveOpsError


def test_code_debug_package_requires_objective_sha_and_failing_behavior():
    with pytest.raises(CognitiveOpsError):
        build_code_debug_package(objective="", failing_behavior="x", exact_sha="abc", changed_files=(), callers_callees=(), interfaces=(), failing_tests=(), relevant_schema=(), authority_constraints=())


def test_founder_decision_package_requires_options():
    with pytest.raises(CognitiveOpsError):
        build_founder_decision_package(decision_required="push to remote?", why_now="unpushed candidate", options=())


def test_compression_quality_check_detects_missing_critical_fields():
    checkpoint = RecoveryCheckpoint(
        current_objective="", why="", program="P", status="active", exact_sha="abc123", branch="b",
        worktree="/w", active_agent="claude",
    )
    missing = validate_checkpoint_reconstructability(checkpoint)
    assert "current_objective" in missing
    assert "why" in missing


def test_fully_populated_checkpoint_reconstructs_cleanly():
    checkpoint = RecoveryCheckpoint(
        current_objective="ship X", why="founder asked", program="P", status="active", exact_sha="abc123",
        branch="b", worktree="/w", active_agent="claude", next_action="run tests", test_evidence="12/12 green",
        remote_backup_state="pushed", dependencies=("Y must merge first",), what_was_tried=("approach A",),
        what_failed=("approach A hit a schema conflict",), what_passed=("approach B",),
        important_findings=("root cause was a stale index",), do_not_repeat=("don't rebuild the evidence axis",),
        authority_boundaries=("no merge to main",), unresolved_questions=("is JSONB sufficient long-term?",),
    )
    assert validate_checkpoint_reconstructability(checkpoint) == ()


def test_fresh_session_reconstructs_program_from_real_durable_handoff_file():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
    result = parse_handoff_markdown(os.path.join(repo_root, "docs", "mainai_v2", "HANDOFF_CLAUDE_RESEARCH_TRUTH_ADVISORY.md"))
    assert all(result.values()), result


def test_self_optimization_rejected_when_quality_falls_despite_fewer_tokens():
    baseline = ContextStrategyMetrics(
        tokens_used=10000, retrieval_latency_ms=200, reread_count=3, missed_dependencies=0, retries=1,
        rework_count=1, examiner_failures=0, wall_clock_seconds=60, cost_usd=0.5, founder_corrections=0, verified_outcomes=5,
    )
    candidate = ContextStrategyMetrics(
        tokens_used=2000, retrieval_latency_ms=50, reread_count=3, missed_dependencies=0, retries=1,
        rework_count=1, examiner_failures=2, wall_clock_seconds=30, cost_usd=0.1, founder_corrections=0, verified_outcomes=5,
    )
    result = evaluate_strategy_change(baseline, candidate)
    assert result.accepted is False


def test_self_optimization_accepted_when_rework_drops_and_quality_holds():
    baseline = ContextStrategyMetrics(
        tokens_used=10000, retrieval_latency_ms=200, reread_count=5, missed_dependencies=0, retries=3,
        rework_count=2, examiner_failures=1, wall_clock_seconds=120, cost_usd=1.0, founder_corrections=0, verified_outcomes=5,
    )
    candidate = ContextStrategyMetrics(
        tokens_used=9000, retrieval_latency_ms=150, reread_count=1, missed_dependencies=0, retries=1,
        rework_count=0, examiner_failures=1, wall_clock_seconds=90, cost_usd=0.8, founder_corrections=0, verified_outcomes=5,
    )
    result = evaluate_strategy_change(baseline, candidate)
    assert result.accepted is True
