"""MainAI Resource Intelligence Round 2 -- `app.resource_intelligence.quota` -- proves the
read-only bridge to `ProviderSpendAuthorization`'s own ceilings, that a missing/no-ceiling
dimension is `missing_data=True` (never a fabricated 0.0/1.0), and `quota_critical()`'s
UNKNOWN != EXHAUSTED contract -- against a real Postgres database, never mocked."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.provider_spend import authorize_provider_spend
from app.resource_intelligence.quota import QUOTA_DIMENSIONS, provider_quota_remaining, quota_critical


@pytest.fixture
def owner_id(superuser_db, make_verified_user):
    user, _password = make_verified_user()
    return user.id


def _goal(db, owner_id):
    goal = create_goal(db, owner_id=owner_id, title="RI quota test", original_instruction="quota test", created_by="founder", approval_policy="standard_repo_work")
    create_plan(db, goal=goal, rationale="ri quota test", tasks=[PlannedTaskSpec(description="Task 0", task_type="repo_edit")], created_by="founder")
    db.commit()
    return goal


def _authorization(db, *, owner_id, goal, **overrides):
    proposal = propose_execution_scope(db, owner_id=owner_id, goal_id=goal.id, idempotency_key=f"prop-{uuid.uuid4()}")
    _, envelope = authorize_execution_scope(
        db, owner_id=owner_id, proposal_id=proposal.id, authorized_by="founder",
        authorized_paths=["README.md"], authorized_capabilities=["read_file", "patch_file"],
        authorized_risk="low", envelope_idempotency_key=f"env-{uuid.uuid4()}",
    )
    kwargs = dict(
        owner_id=owner_id, goal_id=goal.id, execution_envelope_id=envelope.id, authorized_by="founder",
        max_cost_usd=Decimal("10.00"), max_requests=10, idempotency_key=f"spend-{uuid.uuid4()}",
        allowed_providers=["fake-local"], allowed_models=["planner-v2"],
    )
    kwargs.update(overrides)
    db.commit()
    return authorize_provider_spend(db, **kwargs)


def test_no_active_authorization_is_missing_data_for_every_dimension(superuser_db, owner_id):
    goal = _goal(superuser_db, owner_id)
    quota = provider_quota_remaining(superuser_db, owner_id=owner_id, goal_id=goal.id)
    assert set(quota.keys()) == set(QUOTA_DIMENSIONS)
    for envelope in quota.values():
        assert envelope.missing_data is True
        assert envelope.value is None
    assert quota_critical(quota) is False


def test_fresh_authorization_reports_full_quota_remaining(superuser_db, owner_id):
    goal = _goal(superuser_db, owner_id)
    _authorization(superuser_db, owner_id=owner_id, goal=goal)
    superuser_db.commit()

    quota = provider_quota_remaining(superuser_db, owner_id=owner_id, goal_id=goal.id)
    assert quota["cost_usd"].value == pytest.approx(1.0)
    assert quota["requests"].value == pytest.approx(1.0)
    # No token ceiling was configured on this authorization -- missing, not fabricated as 1.0.
    assert quota["prompt_tokens"].missing_data is True
    assert quota["completion_tokens"].missing_data is True
    assert quota_critical(quota) is False


def test_nearly_exhausted_cost_ceiling_is_critical(superuser_db, owner_id):
    goal = _goal(superuser_db, owner_id)
    auth = _authorization(superuser_db, owner_id=owner_id, goal=goal, max_cost_usd=Decimal("10.00"))
    superuser_db.commit()
    auth.spent_cost_usd = Decimal("9.50")  # 5% remaining, below QUOTA_CRITICAL_REMAINING_FRACTION
    superuser_db.commit()

    quota = provider_quota_remaining(superuser_db, owner_id=owner_id, goal_id=goal.id)
    assert quota["cost_usd"].value == pytest.approx(0.05)
    assert quota_critical(quota) is True


def test_outstanding_reservation_counts_against_remaining_quota(superuser_db, owner_id):
    goal = _goal(superuser_db, owner_id)
    auth = _authorization(superuser_db, owner_id=owner_id, goal=goal, max_requests=10)
    superuser_db.commit()
    auth.reserved_requests = 9
    superuser_db.commit()

    quota = provider_quota_remaining(superuser_db, owner_id=owner_id, goal_id=goal.id)
    assert quota["requests"].value == pytest.approx(0.1)
    assert quota_critical(quota) is True
    assert "reservation" in quota["requests"].uncertainty


def test_zero_ceiling_is_missing_data_not_a_fabricated_zero(superuser_db, owner_id):
    goal = _goal(superuser_db, owner_id)
    _authorization(superuser_db, owner_id=owner_id, goal=goal, max_cost_usd=Decimal("0"))
    superuser_db.commit()

    quota = provider_quota_remaining(superuser_db, owner_id=owner_id, goal_id=goal.id)
    assert quota["cost_usd"].missing_data is True


def test_quota_never_calls_a_mutating_provider_spend_function():
    """Structural: this module must never reserve/settle/release/revoke -- it only reads. AST-
    based (not a substring/regex search) so this module's own prose -- which legitimately
    discusses these names in English -- cannot produce a false failure, matching `decision.py`'s
    own `test_propose_resource_action_is_pure_no_db_no_mutation_no_io` technique."""
    import ast
    import inspect

    import app.resource_intelligence.quota as module

    tree = ast.parse(inspect.getsource(module))
    forbidden_calls = {"reserve_provider_spend_call", "settle_provider_spend_call", "release_provider_spend_call", "revoke_provider_spend"}
    called_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called_names.add(func.id)
            elif isinstance(func, ast.Attribute):
                called_names.add(func.attr)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "with_for_update":
            raise AssertionError("quota.py must never take a row lock (with_for_update)")
    hit = forbidden_calls & called_names
    assert not hit, f"quota.py must never call {hit}"
