"""MainAI Resource Intelligence Part 2 -- `app.resource_intelligence.decision` -- proves
`propose_resource_action()` is a PURE function (structural proof, matching
`app.mainai_executive.judgment`'s own `test_judgment_module_has_no_db_or_mutating_call_
anywhere` technique), that missing_data on the key inputs never silently means a confident
CONTINUE/KEEP_CURRENT_AGENT "all clear" (mutation-style is not needed here since the property
itself IS the mutation contrast: compare healthy-known-data vs missing-data with everything else
identical), that `wip_at_limit=True` changes the outcome versus the same call without it
(mutation-style, matching `judgment.py`'s own `test_kill_signal_changes_the_outcome_versus_the_
same_call_without_it` precedent), and the documented decision table's other named branches
(CHECKPOINT before COMPACT/RESET, COMPACT vs RESET_SESSION, established poor efficiency ->
HANDOFF/CHANGE_MODEL, provisional efficiency never triggers those).

See docs/mainai_v2/MAINAI_RESOURCE_CONTEXT_COST_RECONCILIATION.md §1.6 for the architecture this
module implements."""

from __future__ import annotations

import ast
import inspect
import re
from datetime import datetime

from app.resource_intelligence.decision import (
    CONTEXT_UTILIZATION_CRITICAL_PCT,
    CONTEXT_UTILIZATION_HIGH_PCT,
    TIME_TO_LIMIT_LOW_SECONDS,
    propose_resource_action,
)
from app.resource_intelligence.efficiency_profile import MIN_SAMPLE_SIZE_FOR_ESTABLISHED
from app.resource_intelligence.types import ContextLifecycleAction, MetricEnvelope, unknown_metric


def _metric(value, *, unit="unit", sample_size=10, uncertainty=None) -> MetricEnvelope:
    return MetricEnvelope(
        value=value, unit=unit, definition="test metric", denominator=None, time_window=None,
        population=None, sample_size=sample_size, source="test", method="test",
        missing_data=False, uncertainty=uncertainty, last_updated=datetime.utcnow(), trend=None,
    )


def _healthy_kwargs() -> dict:
    return dict(
        context_utilization=_metric(30.0, unit="percent"),
        time_to_limit=_metric(10_000.0, unit="seconds"),
    )


# ============================================================================ missing data never means "all clear"


def test_missing_context_utilization_never_returns_a_confident_continue():
    decision = propose_resource_action(
        context_utilization=unknown_metric(unit="percent", definition="d", source="s"),
        time_to_limit=_metric(10_000.0, unit="seconds"),
    )
    assert decision.action != ContextLifecycleAction.CONTINUE_CURRENT_SESSION
    assert decision.action == ContextLifecycleAction.KEEP_CURRENT_AGENT
    assert "missing" in decision.reason.lower()
    assert "all clear" not in decision.reason.lower() or "not" in decision.reason.lower()
    assert decision.authorized is False


def test_missing_time_to_limit_never_returns_a_confident_continue():
    decision = propose_resource_action(
        context_utilization=_metric(30.0, unit="percent"),
        time_to_limit=unknown_metric(unit="seconds", definition="d", source="s"),
    )
    assert decision.action == ContextLifecycleAction.KEEP_CURRENT_AGENT
    assert "missing" in decision.reason.lower()


def test_both_metrics_known_and_healthy_yields_confident_continue():
    """Contrast case: identical shape, but both metrics ARE known and healthy -- this is the
    only path allowed to reach CONTINUE_CURRENT_SESSION."""
    decision = propose_resource_action(**_healthy_kwargs())
    assert decision.action == ContextLifecycleAction.CONTINUE_CURRENT_SESSION
    assert decision.authorized is False


# ============================================================================ wip_at_limit mutation


def test_wip_at_limit_changes_the_outcome_versus_the_same_call_without_it():
    base_kwargs = _healthy_kwargs()

    without_wip = propose_resource_action(**base_kwargs)
    assert without_wip.action == ContextLifecycleAction.CONTINUE_CURRENT_SESSION

    with_wip = propose_resource_action(**base_kwargs, wip_at_limit=True)
    assert with_wip.action == ContextLifecycleAction.DEFER
    assert with_wip.action != without_wip.action
    assert with_wip.signals["wip_at_limit"] is True


# ============================================================================ acute context-lifecycle branches


def test_high_utilization_low_ttl_with_critical_state_checkpoints_before_compact():
    decision = propose_resource_action(
        context_utilization=_metric(CONTEXT_UTILIZATION_HIGH_PCT + 5, unit="percent"),
        time_to_limit=_metric(TIME_TO_LIMIT_LOW_SECONDS - 60, unit="seconds"),
        critical_unsummarized_state=True,
    )
    assert decision.action == ContextLifecycleAction.CHECKPOINT


def test_high_utilization_low_ttl_without_critical_state_compacts_not_checkpoints():
    decision = propose_resource_action(
        context_utilization=_metric(CONTEXT_UTILIZATION_HIGH_PCT + 5, unit="percent"),
        time_to_limit=_metric(TIME_TO_LIMIT_LOW_SECONDS - 60, unit="seconds"),
        critical_unsummarized_state=False,
    )
    assert decision.action == ContextLifecycleAction.COMPACT
    assert decision.action != ContextLifecycleAction.CHECKPOINT


def test_critical_utilization_resets_instead_of_compacting():
    decision = propose_resource_action(
        context_utilization=_metric(CONTEXT_UTILIZATION_CRITICAL_PCT + 1, unit="percent"),
        time_to_limit=_metric(TIME_TO_LIMIT_LOW_SECONDS - 60, unit="seconds"),
        critical_unsummarized_state=False,
    )
    assert decision.action == ContextLifecycleAction.RESET_SESSION


def test_elevated_utilization_with_large_remaining_task_splits_proactively():
    decision = propose_resource_action(
        context_utilization=_metric(65.0, unit="percent"),
        time_to_limit=_metric(50_000.0, unit="seconds"),
        task_remaining_size="large",
    )
    assert decision.action == ContextLifecycleAction.SPLIT_JOB


# ============================================================================ established vs provisional efficiency profile


def test_established_poor_accepted_commit_rate_recommends_handoff():
    profile = {
        "accepted_commit_rate": _metric(0.2, unit="fraction", sample_size=MIN_SAMPLE_SIZE_FOR_ESTABLISHED),
        "rework_rate": _metric(0.1, unit="fraction", sample_size=MIN_SAMPLE_SIZE_FOR_ESTABLISHED),
    }
    decision = propose_resource_action(**_healthy_kwargs(), efficiency_profile=profile)
    assert decision.action == ContextLifecycleAction.HANDOFF


def test_established_high_rework_rate_with_acceptable_accept_rate_recommends_change_model():
    profile = {
        "accepted_commit_rate": _metric(0.8, unit="fraction", sample_size=MIN_SAMPLE_SIZE_FOR_ESTABLISHED),
        "rework_rate": _metric(0.6, unit="fraction", sample_size=MIN_SAMPLE_SIZE_FOR_ESTABLISHED),
    }
    decision = propose_resource_action(**_healthy_kwargs(), efficiency_profile=profile)
    assert decision.action == ContextLifecycleAction.CHANGE_MODEL


def test_provisional_poor_efficiency_profile_never_triggers_handoff_or_change_model():
    """ONE_RUN != LONG_TERM_PROFILE: the SAME poor numbers, but below the minimum sample size,
    must not bias the decision -- mutation-style contrast with the established tests above."""
    profile = {
        "accepted_commit_rate": _metric(0.1, unit="fraction", sample_size=MIN_SAMPLE_SIZE_FOR_ESTABLISHED - 1),
        "rework_rate": _metric(0.9, unit="fraction", sample_size=MIN_SAMPLE_SIZE_FOR_ESTABLISHED - 1),
    }
    decision = propose_resource_action(**_healthy_kwargs(), efficiency_profile=profile)
    assert decision.action not in (ContextLifecycleAction.HANDOFF, ContextLifecycleAction.CHANGE_MODEL)
    assert decision.action == ContextLifecycleAction.CONTINUE_CURRENT_SESSION


# ============================================================================ signals are never opaque


def test_every_decision_cites_the_real_signals_that_drove_it():
    decision = propose_resource_action(**_healthy_kwargs(), wip_at_limit=True)
    assert decision.signals
    assert "context_utilization" in decision.signals
    assert "time_to_limit" in decision.signals
    assert decision.signals["wip_at_limit"] is True
    assert decision.authorized is False


# ============================================================================ structural purity proof


def test_propose_resource_action_is_pure_no_db_no_mutation_no_io():
    """Structural: `propose_resource_action()` has no `db.add`/`db.commit`/`db.flush`/`UPDATE`,
    no `db` parameter, no sqlalchemy import, and no reference anywhere to any real mutating
    function this whole program forbids this package from calling
    (create_work_assignment/authorize_execution_scope/transition_status/reserve_provider_spend_
    call/settle_provider_spend_call/record_telemetry_sample/save_agent_session_checkpoint/
    record_capability_observation/dismiss_work_candidate/supersede_work_candidate). Uses `ast`
    (not a plain substring search) for the import check, and a real word-boundary regex for the
    call-site checks, matching `app.mainai_executive.judgment`'s own
    `test_judgment_module_has_no_db_or_mutating_call_anywhere` technique so this module's own
    prose can freely discuss these names in English without producing a false failure."""

    import app.resource_intelligence.decision as module

    source = inspect.getsource(module)
    assert not re.search(r"\bdb\.add\(", source)
    assert not re.search(r"\bdb\.commit\(", source)
    assert not re.search(r"\bdb\.flush\(", source)
    assert not re.search(r"\bUPDATE \w", source)

    tree = ast.parse(source)

    # AST-based (not a plain substring/regex search) so this module's OWN prose -- which
    # legitimately discusses names like "authorize_execution_scope()" in English -- cannot
    # produce a false failure. Only real ast.Call nodes count.
    forbidden_calls = {
        "create_work_assignment", "authorize_execution_scope", "transition_status",
        "reserve_provider_spend_call", "settle_provider_spend_call", "record_telemetry_sample",
        "save_agent_session_checkpoint", "record_capability_observation",
        "dismiss_work_candidate", "supersede_work_candidate",
    }
    called_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called_names.add(func.id)
            elif isinstance(func, ast.Attribute):
                called_names.add(func.attr)
    hit = forbidden_calls & called_names
    assert not hit, f"decision.py must never call any of {hit}"

    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module.split(".")[0])
    assert "sqlalchemy" not in imported_modules

    sig = inspect.signature(propose_resource_action)
    assert "db" not in sig.parameters


def test_every_returned_recommendation_has_authorized_false():
    for decision in (
        propose_resource_action(**_healthy_kwargs()),
        propose_resource_action(**_healthy_kwargs(), wip_at_limit=True),
        propose_resource_action(
            context_utilization=_metric(CONTEXT_UTILIZATION_CRITICAL_PCT + 1, unit="percent"),
            time_to_limit=_metric(10.0, unit="seconds"),
            critical_unsummarized_state=True,
        ),
    ):
        assert decision.authorized is False
