"""MainAI Research -- `app.mainai_research.adapters` + package-wide structural purity sweep. See
docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the architecture this
package implements.

RESEARCH != AUTHORITY. CONCLUSION != EXECUTION AUTHORITY. ECONOMIC RECOMMENDATION != SPEND
AUTHORITY. LEGAL ANALYSIS != OWNER AUTHORITY. SPECIALIST CONSENSUS != AUTHORIZATION. Verified
structurally across the WHOLE package at once, mirroring `app.mainai_vision`'s own AST-based
technique."""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import uuid

import app.mainai_research as mainai_research_pkg
from app.mainai_research.adapters import vision_completion_snapshot
from app.models.user import User

FORBIDDEN_CALLS = frozenset(
    {
        "promote_interpretation_proposal",
        "mark_project_entity_superseded",
        "authorize_work_candidate",
        "dismiss_work_candidate",
        "supersede_work_candidate",
        "create_work_assignment",
        "authorize_execution_scope",
        "transition_status",
        "transition_intent",
        "reserve_provider_spend_call",
        "settle_provider_spend_call",
        "release_provider_spend_call",
        "record_capability_observation",
        "activate_kill_switch",
        "activate_global_kill_switch",
        "authorize_provider_spend",
    }
)


def _all_module_names() -> list[str]:
    return [f"app.mainai_research.{m.name}" for m in pkgutil.iter_modules(mainai_research_pkg.__path__)]


def test_vision_completion_snapshot_is_the_real_completion_engine(superuser_db):
    owner = User(email=f"radp-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(owner)
    superuser_db.flush()
    superuser_db.commit()

    snapshot = vision_completion_snapshot(superuser_db, owner_id=owner.id)
    assert "overall_percent" in snapshot
    assert "does NOT mean" in snapshot["definition"]


def test_no_module_calls_a_forbidden_mutating_function():
    hits: dict[str, set[str]] = {}
    for name in _all_module_names():
        module = importlib.import_module(name)
        tree = ast.parse(inspect.getsource(module))
        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    called.add(func.id)
                elif isinstance(func, ast.Attribute):
                    called.add(func.attr)
        overlap = called & FORBIDDEN_CALLS
        if overlap:
            hits[name] = overlap
    assert not hits, f"forbidden mutating call(s) found: {hits}"


def test_every_authorized_field_defaults_to_false():
    import re

    offenders = []
    for name in _all_module_names():
        module = importlib.import_module(name)
        source = inspect.getsource(module)
        for match in re.finditer(r"authorized:\s*bool\s*=\s*(\w+)", source):
            if match.group(1) != "False":
                offenders.append((name, match.group(0)))
    assert not offenders, f"a dataclass defaults authorized to something other than False: {offenders}"


def test_only_research_ledger_module_writes_to_the_database():
    """Every OTHER module must contain zero `db.add(`/INSERT/UPDATE/DELETE -- `research_
    ledger.py` (the durable ledger itself) and `book_provenance.py` (composes `research_ledger`
    read functions only, but imports the module) are the documented exceptions for write
    surface; `adapters.py` only reads."""
    import re

    allowed_writers = {"app.mainai_research.research_ledger"}
    violations = []
    for name in _all_module_names():
        if name in allowed_writers:
            continue
        module = importlib.import_module(name)
        source = inspect.getsource(module)
        if re.search(r"\bINSERT INTO\b", source) or re.search(r"\bUPDATE \w+ SET\b", source) or re.search(r"\bDELETE FROM\b", source):
            violations.append(name)
    assert not violations, f"unexpected write in: {violations}"
