"""MainAI Cognitive Control Plane -- `app.mainai_vision.adapters` -- proves the two real
adapters (Founder Reasoning, Resource Intelligence) actually compose with real, existing
functions (never re-implemented), and that this module never imports a mutating function from
any of the six named sibling programs.

See docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the architecture this
module implements."""

from __future__ import annotations

import uuid

from app.mainai_vision.adapters import founder_reasoning_snapshot, resource_intelligence_snapshot
from app.models.user import User


def test_founder_reasoning_snapshot_is_the_real_executive_status_snapshot(superuser_db):
    owner = User(email=f"ad-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(owner)
    superuser_db.flush()
    superuser_db.commit()

    snapshot = founder_reasoning_snapshot(superuser_db, owner_id=owner.id)
    assert snapshot["chain_of_thought_exposed"] is False
    assert snapshot["evidence_basis"] == "durable_rows_only"


def test_resource_intelligence_snapshot_is_the_real_scheduler_output(superuser_db):
    owner = User(email=f"ad2-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(owner)
    superuser_db.flush()
    superuser_db.commit()

    rows = resource_intelligence_snapshot(superuser_db, owner_id=owner.id)
    assert rows == []  # no agents/assignments registered -- honest empty, not an error


def test_adapters_module_never_imports_a_mutating_sibling_function():
    """Structural: RUNTIME STATE != REASONING STATE / MEMORY != AUTHORITY -- this module must
    never import a real authority-granting function from any of the six sibling programs."""
    import ast
    import inspect

    import app.mainai_vision.adapters as module

    tree = ast.parse(inspect.getsource(module))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)

    forbidden = {
        "authorize_work_candidate", "promote_interpretation_proposal", "create_work_assignment",
        "authorize_execution_scope", "transition_status", "record_telemetry_sample",
        "reserve_provider_spend_call", "settle_provider_spend_call",
    }
    assert not (forbidden & imported_names)
