"""Package-wide structural purity sweep for `app.mainai_coverage` and `app.mainai_workforce`,
mirroring `app.mainai_vision`/`app.mainai_research`/`app.mainai_cognitive_ops`'s own AST-based
technique. See docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md.

CAPABILITY != AUTHORITY. MASTERY != AUTHORITY. SCHEDULER RECOMMENDATION != EXECUTION
AUTHORIZATION."""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import re

import app.mainai_coverage as coverage_pkg
import app.mainai_workforce as workforce_pkg

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
        "acquire_lease",
        "release_lease",
    }
)


def _all_module_names(pkg) -> list[str]:
    return [f"{pkg.__name__}.{m.name}" for m in pkgutil.iter_modules(pkg.__path__)]


def _check_forbidden_calls(pkg) -> dict[str, set[str]]:
    hits: dict[str, set[str]] = {}
    for name in _all_module_names(pkg):
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
    return hits


def test_mainai_coverage_never_calls_a_forbidden_mutating_function():
    hits = _check_forbidden_calls(coverage_pkg)
    assert not hits, f"forbidden mutating call(s) found: {hits}"


def test_mainai_workforce_never_calls_a_forbidden_mutating_function():
    hits = _check_forbidden_calls(workforce_pkg)
    assert not hits, f"forbidden mutating call(s) found: {hits}"


def test_only_mastery_ledger_writes_to_the_database():
    allowed_writers = {"app.mainai_workforce.mastery_ledger"}
    violations = []
    for name in _all_module_names(workforce_pkg):
        if name in allowed_writers:
            continue
        module = importlib.import_module(name)
        source = inspect.getsource(module)
        if re.search(r"\bINSERT INTO\b", source) or re.search(r"\bUPDATE \w+ SET\b", source) or re.search(r"\bDELETE FROM\b", source):
            violations.append(name)
    assert not violations, f"unexpected write in: {violations}"


def test_coverage_dynamic_denominator_only_stages_never_promotes():
    module = importlib.import_module("app.mainai_coverage.dynamic_denominator")
    tree = ast.parse(inspect.getsource(module))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)
    assert "promote_interpretation_proposal" not in imported_names


def test_every_authorized_field_defaults_to_false():
    offenders = []
    for pkg in (coverage_pkg, workforce_pkg):
        for name in _all_module_names(pkg):
            module = importlib.import_module(name)
            source = inspect.getsource(module)
            for match in re.finditer(r"authorized:\s*bool\s*=\s*(\w+)", source):
                if match.group(1) != "False":
                    offenders.append((name, match.group(0)))
    assert not offenders, f"a dataclass defaults authorized to something other than False: {offenders}"
