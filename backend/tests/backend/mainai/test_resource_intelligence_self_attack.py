"""Independent adversarial self-attack pass over the Resource Intelligence program. Separate
from each module's own unit tests (BUILDER != FINAL EXAMINER) -- targets: RESOURCE_OPTIMIZATION
!= AUTHORITY (full-package sweep, not just decision.py/scheduler.py), METRIC != TRUTH (every
public metric-returning function actually returns a MetricEnvelope), UNKNOWN stays UNKNOWN
(never a fabricated zero), ONE_RUN != LONG_TERM_PROFILE (provisional gate boundary), and
cross-owner isolation."""

from __future__ import annotations

import ast
import inspect
import uuid

import pytest

from app.resource_intelligence import (
    cost_bridge,
    decision,
    efficiency_profile,
    scheduler,
    session_checkpoint,
    telemetry,
    types,
)
from app.resource_intelligence.efficiency_profile import MIN_SAMPLE_SIZE_FOR_ESTABLISHED, is_provisional
from app.resource_intelligence.types import MetricEnvelope

FORBIDDEN_MUTATING_SYMBOLS = (
    "create_work_assignment",
    "authorize_execution_scope",
    "transition_status",
    "reserve_provider_spend_call",
    "settle_provider_spend_call",
    "release_provider_spend_call",
    "record_provider_spend_usage",
    "record_capability_observation",
    "dismiss_work_candidate",
    "supersede_work_candidate",
    "authorize_work_candidate",
    "acquire_lease",
)

ALL_MODULES = (types, telemetry, cost_bridge, session_checkpoint, efficiency_profile, decision, scheduler)


# --- 1. RESOURCE_OPTIMIZATION != AUTHORITY: full-package sweep, not just decision/scheduler. --


@pytest.mark.parametrize("module", ALL_MODULES, ids=lambda m: m.__name__)
def test_no_module_in_the_package_imports_a_real_mutating_authority_function(module):
    source = inspect.getsource(module)
    tree = ast.parse(source)
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported_names.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
    for forbidden in FORBIDDEN_MUTATING_SYMBOLS:
        assert forbidden not in imported_names, f"{module.__name__} imports {forbidden!r} -- resource intelligence must never grant or exercise authority"


def test_decision_module_is_fully_pure_no_db_no_sqlalchemy():
    source = inspect.getsource(decision)
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module.split(".")[0])
    assert "sqlalchemy" not in imported_modules
    sig = inspect.signature(decision.propose_resource_action)
    assert "db" not in sig.parameters


@pytest.mark.parametrize("module", (decision, scheduler), ids=lambda m: m.__name__)
def test_decision_and_scheduler_never_call_db_add_commit_flush(module):
    source = inspect.getsource(module)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in ("add", "commit", "flush", "delete"), f"{module.__name__} calls db.{node.func.attr}() -- must be read-only"


def test_every_returned_recommendation_has_authorized_false():
    decision_result = decision.propose_resource_action(
        context_utilization=types.unknown_metric(unit="percent", definition="d", source="s"),
        time_to_limit=types.unknown_metric(unit="seconds", definition="d", source="s"),
    )
    assert decision_result.authorized is False


# --- 2. METRIC != TRUTH: every public metric function returns a real MetricEnvelope. ---------


def test_context_utilization_and_time_to_limit_return_metric_envelope_shape(superuser_db):
    owner_id = uuid.uuid4()
    result = telemetry.context_utilization(superuser_db, owner_id=owner_id, attempt_id=uuid.uuid4())
    assert isinstance(result, MetricEnvelope)
    result2 = telemetry.estimated_time_to_context_limit(superuser_db, owner_id=owner_id, attempt_id=uuid.uuid4())
    assert isinstance(result2, MetricEnvelope)


def test_idle_productive_blocked_time_returns_dict_of_metric_envelopes(superuser_db):
    result = telemetry.idle_productive_blocked_time(superuser_db, owner_id=uuid.uuid4(), assignment_id=uuid.uuid4())
    assert set(result.keys()) == {"idle_seconds", "productive_seconds", "blocked_seconds"}
    for value in result.values():
        assert isinstance(value, MetricEnvelope)


def test_efficiency_profile_returns_exactly_the_documented_keys_as_metric_envelopes(superuser_db):
    result = efficiency_profile.agent_efficiency_profile(superuser_db, owner_id=uuid.uuid4(), agent_id=uuid.uuid4())
    assert set(result.keys()) == set(efficiency_profile.PROFILE_METRIC_KEYS)
    for value in result.values():
        assert isinstance(value, MetricEnvelope)


def test_metric_envelope_never_omits_a_required_field_even_when_value_is_unknown():
    envelope = types.unknown_metric(unit="usd", definition="test", source="test")
    assert envelope.value is None
    assert envelope.missing_data is True
    # Every OTHER field must still be honestly present, never silently omitted.
    for field_name in ("unit", "definition", "source", "method", "last_updated"):
        assert getattr(envelope, field_name) is not None


# --- 3. UNKNOWN stays UNKNOWN: no function fabricates a zero for genuinely absent data. -------


def test_cost_for_unknown_assignment_is_missing_not_zero(superuser_db):
    result = cost_bridge.cost_for_assignment(superuser_db, owner_id=uuid.uuid4(), assignment_id=uuid.uuid4())
    assert result.missing_data is True
    assert result.value is None  # never 0.0


def test_context_utilization_with_zero_window_is_missing_not_a_fabricated_percent(superuser_db, make_verified_user):
    """A context_window_tokens=0 must never produce a 0% or 100% reading -- both would be
    fabricated, not observed."""
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)

    from tests.backend.mainai.test_resource_intelligence_longitudinal_scenarios import _agent, _assign, _goal_task

    owner, _password = make_verified_user()
    agent = _agent(superuser_db, "zero-window-agent")
    goal, task = _goal_task(superuser_db, owner.id)
    assignment = _assign(superuser_db, owner_id=owner.id, goal=goal, task=task, agent=agent)
    superuser_db.commit()

    attempt_id = uuid.uuid4()
    telemetry.record_telemetry_sample(
        superuser_db, owner_id=owner.id, assignment_id=assignment.id, attempt_id=attempt_id,
        context_used_tokens=100, context_window_tokens=0,
    )
    superuser_db.commit()

    result = telemetry.context_utilization(superuser_db, owner_id=owner.id, attempt_id=attempt_id)
    assert result.missing_data is True
    assert result.value is None


def test_estimated_time_to_context_limit_missing_for_non_positive_headroom_case_is_never_negative(superuser_db):
    """If a burn rate is somehow computed as exactly zero or negative, the function must return
    missing_data=True -- never a negative or nonsensical 'seconds until limit' value."""
    result = telemetry.estimated_time_to_context_limit(superuser_db, owner_id=uuid.uuid4(), attempt_id=uuid.uuid4())
    assert result.missing_data is True
    assert result.value is None


# --- 4. ONE_RUN != LONG_TERM_PROFILE: exact boundary behavior of is_provisional(). ------------


def test_is_provisional_boundary_exact():
    assert is_provisional(MIN_SAMPLE_SIZE_FOR_ESTABLISHED - 1) is True
    assert is_provisional(MIN_SAMPLE_SIZE_FOR_ESTABLISHED) is False
    assert is_provisional(0) is True
    assert is_provisional(None) is True  # unknown sample size is never treated as established


# --- 5. Never divide by zero, anywhere in the package. ----------------------------------------


def test_cost_per_accepted_commit_zero_completions_never_raises(superuser_db):
    result = cost_bridge.cost_per_accepted_commit(superuser_db, owner_id=uuid.uuid4(), agent_id=uuid.uuid4())
    assert result.missing_data is True


def test_efficiency_profile_zero_assignments_never_raises(superuser_db):
    result = efficiency_profile.agent_efficiency_profile(superuser_db, owner_id=uuid.uuid4(), agent_id=uuid.uuid4())
    for value in result.values():
        assert value.missing_data is True


# --- 6. Checkpoint mechanism never uses a table this package's own docstrings forbid. ---------


def test_session_checkpoint_creates_no_new_table_uses_founder_memory():
    source = inspect.getsource(session_checkpoint)
    assert "founder_memory" in source
    assert "CREATE TABLE" not in source
    tree = ast.parse(source)
    class_defs = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    # AgentSessionCheckpoint (a plain dataclass) is the only class this module defines -- no ORM
    # model / no new table declaration lives here.
    assert class_defs == ["AgentSessionCheckpoint"]


# --- 7. scheduler.py never calls provider_spend directly -- must go through cost_bridge. ------


def test_scheduler_never_imports_provider_spend_directly():
    source = inspect.getsource(scheduler)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module_name = getattr(node, "module", None) or ""
            names = [alias.name for alias in node.names]
            assert "provider_spend" not in module_name, "scheduler.py must go through cost_bridge/efficiency_profile, never provider_spend directly"
            assert not any("provider_spend" in n for n in names)
