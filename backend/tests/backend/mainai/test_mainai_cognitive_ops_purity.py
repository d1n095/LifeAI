"""Package-wide structural purity sweep for `app.mainai_cognitive_ops`, mirroring
`app.mainai_vision`/`app.mainai_research`'s own AST-based technique. See
docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md.

PUSH/BACKUP != MERGE. MERGE != DEPLOY. REMOTE WRITE != DEPLOY AUTHORITY. This package must
never call a mutating function belonging to another domain, and `repo_backup_intelligence.py`
must never invoke a mutating git subcommand (push/commit/merge/reset/checkout/rebase)."""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import re

import app.mainai_cognitive_ops as pkg

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

FORBIDDEN_GIT_SUBCOMMANDS = frozenset({"push", "commit", "merge", "reset", "checkout", "rebase", "cherry-pick", "clean"})


def _all_module_names() -> list[str]:
    return [f"app.mainai_cognitive_ops.{m.name}" for m in pkgutil.iter_modules(pkg.__path__)]


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


def test_only_founder_communication_ledger_writes_to_the_database():
    allowed_writers = {"app.mainai_cognitive_ops.founder_communication_ledger"}
    violations = []
    for name in _all_module_names():
        if name in allowed_writers:
            continue
        module = importlib.import_module(name)
        source = inspect.getsource(module)
        if re.search(r"\bINSERT INTO\b", source) or re.search(r"\bUPDATE \w+ SET\b", source) or re.search(r"\bDELETE FROM\b", source):
            violations.append(name)
    assert not violations, f"unexpected write in: {violations}"


def test_founder_communication_ledger_never_updates_or_deletes():
    """Append-only by construction: the ledger module itself must contain no UPDATE/DELETE
    statement string (only INSERT/SELECT), matching the DB-level trigger that would reject one
    anyway."""

    module = importlib.import_module("app.mainai_cognitive_ops.founder_communication_ledger")
    source = inspect.getsource(module)
    assert not re.search(r"\bUPDATE \w+ SET\b", source)
    assert not re.search(r"\bDELETE FROM\b", source)


def test_repo_backup_intelligence_never_invokes_a_mutating_git_subcommand():
    module = importlib.import_module("app.mainai_cognitive_ops.repo_backup_intelligence")
    tree = ast.parse(inspect.getsource(module))
    string_literals = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    overlap = string_literals & FORBIDDEN_GIT_SUBCOMMANDS
    assert not overlap, f"repo_backup_intelligence.py references mutating git subcommand(s): {overlap}"


def test_every_authorized_field_defaults_to_false():
    offenders = []
    for name in _all_module_names():
        module = importlib.import_module(name)
        source = inspect.getsource(module)
        for match in re.finditer(r"authorized:\s*bool\s*=\s*(\w+)", source):
            if match.group(1) != "False":
                offenders.append((name, match.group(0)))
    assert not offenders, f"a dataclass defaults authorized to something other than False: {offenders}"
