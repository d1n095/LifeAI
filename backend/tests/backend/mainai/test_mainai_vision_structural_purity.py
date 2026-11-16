"""MainAI Cognitive Control Plane -- package-wide structural purity sweep, matching
`resource_intelligence.decision`'s own AST-based technique: every module in `app.mainai_vision`
must never call a real authority-granting/mutating function belonging to any sibling program.
VISION != AUTHORITY / IMPLIED REQUIREMENT != EXECUTION AUTHORITY / MODEL OUTPUT != AUTHORITY,
verified structurally across the WHOLE package at once, not just per-module.

See docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the architecture this
package implements."""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil

import app.mainai_vision as mainai_vision_pkg

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
    }
)


def _all_module_names() -> list[str]:
    names = []
    for m in pkgutil.iter_modules(mainai_vision_pkg.__path__):
        names.append(f"app.mainai_vision.{m.name}")
    return names


def test_no_module_in_the_package_calls_a_forbidden_mutating_function():
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


def test_only_gap_generator_and_mind_change_and_cognitive_loop_touch_db_writes():
    """Every OTHER module must contain zero `db.add(`/`db.commit(`/INSERT/UPDATE/DELETE --
    `gap_generator.persist_gap_proposals()` (staging only) and `mind_change.py` (append-only
    FounderMemoryNote) are the two deliberate, documented exceptions; `cognitive_loop.py` itself
    never writes directly but composes `run_executive_cycle()`, which does (that function's own
    writes are not this package's concern -- it is a real, already-proven function)."""
    import re

    allowed_writers = {"app.mainai_vision.gap_generator", "app.mainai_vision.mind_change", "app.mainai_vision.cognitive_loop"}
    violations = []
    for name in _all_module_names():
        if name in allowed_writers:
            continue
        module = importlib.import_module(name)
        source = inspect.getsource(module)
        if re.search(r"\bdb\.add\(", source) or re.search(r"\bdb\.commit\(", source) or re.search(r"\bINSERT INTO\b", source) or re.search(r"\bUPDATE \w+ SET\b", source) or re.search(r"\bDELETE FROM\b", source):
            violations.append(name)
    assert not violations, f"unexpected write in: {violations}"


def test_every_public_dataclass_defaults_authorized_to_false():
    """Sweep every module for a dataclass field literally named `authorized` -- every one found
    must default to False. Catches a future accidental `authorized: bool = True` default."""
    import re

    offenders = []
    for name in _all_module_names():
        module = importlib.import_module(name)
        source = inspect.getsource(module)
        for match in re.finditer(r"authorized:\s*bool\s*=\s*(\w+)", source):
            if match.group(1) != "False":
                offenders.append((name, match.group(0)))
    assert not offenders, f"a dataclass defaults authorized to something other than False: {offenders}"
