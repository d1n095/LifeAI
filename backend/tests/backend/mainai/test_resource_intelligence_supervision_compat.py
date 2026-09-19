"""MainAI Resource Intelligence Round 2 -- `app.resource_intelligence.supervision_compat` --
pure translation from a `mainai_supervision_telemetry`-shaped dict (Codex's Continuous
Supervision candidate, SHA a7df7f90dba9f8bc993005b2cce1d4c8cb7dcec4) into this package's own
telemetry kwargs, with UNKNOWN preserved and unrecognized columns surfaced rather than dropped.

This module never imports anything from that candidate -- these tests build the input dict by
hand, matching that table's own reviewed column names literally, exactly as a real caller
bridging the two systems after both eventually land would have to."""

from __future__ import annotations

from app.resource_intelligence.supervision_compat import (
    SUPERVISION_TELEMETRY_COLUMNS,
    from_supervision_telemetry_row,
    supervision_cost_fields,
)

_FULL_ROW = {
    "owner_id": "owner-1", "agent_id": "agent-1", "job_id": "job-1", "attempt_id": "attempt-1",
    "provider": "anthropic", "model": "claude-sonnet-5", "state": "RUNNING",
    "productive_seconds": 120.0, "idle_seconds": 30.0, "blocked_seconds": 0.0, "stalled_seconds": 0.0,
    "continuation_count": 1, "premature_return_count": 0, "restart_count": 0,
    "context_input_tokens": 5000, "context_output_tokens": 200, "context_cached_tokens": 100,
    "context_limit_tokens": 200000, "provider_quota_remaining": 0.8,
    "estimated_cost": 0.05, "reported_cost": 0.04, "validated_cost": None,
    "retries": 0, "failed_attempts": 0, "rework_count": 0, "examiner_outcome": None,
    "last_progress_at": "2026-09-11T00:00:00Z", "handoff_ready": False, "context_risk": "LOW",
    "observed_at": "2026-09-11T00:00:00Z",
}


def test_full_row_translates_context_fields():
    result = from_supervision_telemetry_row(_FULL_ROW)
    assert result.sample_kwargs["context_used_tokens"] == 5000
    assert result.sample_kwargs["context_window_tokens"] == 200000
    assert result.sample_kwargs["output_tokens"] == 200
    assert result.sample_kwargs["cached_tokens"] == 100
    assert result.unrecognized == ()
    assert any("context_input_tokens" in note for note in result.notes)


def test_missing_fields_stay_none_never_fabricated():
    result = from_supervision_telemetry_row({"agent_id": "agent-1"})
    assert result.sample_kwargs["context_used_tokens"] is None
    assert result.sample_kwargs["context_window_tokens"] is None
    assert result.notes == ()


def test_unrecognized_column_is_surfaced_not_silently_dropped():
    result = from_supervision_telemetry_row({**_FULL_ROW, "some_future_column": 1})
    assert result.unrecognized == ("some_future_column",)


def test_provenance_omits_none_fields():
    result = from_supervision_telemetry_row({"agent_id": "agent-1"})
    assert "source_agent_id" in result.provenance
    assert "source_job_id" not in result.provenance


def test_every_declared_column_round_trips_without_becoming_unrecognized():
    row = {col: None for col in SUPERVISION_TELEMETRY_COLUMNS}
    result = from_supervision_telemetry_row(row)
    assert result.unrecognized == ()


def test_supervision_cost_fields_are_uncertain_without_independent_validation():
    fields = supervision_cost_fields(_FULL_ROW)
    assert fields["classification"] == "UNCERTAIN"
    assert fields["reported_cost"] == 0.04


def test_supervision_cost_fields_validated_when_validated_cost_present():
    fields = supervision_cost_fields({**_FULL_ROW, "validated_cost": 0.04})
    assert fields["classification"] == "VALIDATED_BY_SUPERVISION"


def test_module_never_imports_the_supervision_candidate():
    """This module must remain buildable/testable without that unmerged branch ever existing
    in this checkout -- confirmed structurally, not just by convention."""
    import ast
    import inspect

    import app.resource_intelligence.supervision_compat as module

    tree = ast.parse(inspect.getsource(module))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    assert not any("supervision" in m and "resource_intelligence" not in m for m in imported_modules)
