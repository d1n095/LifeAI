"""Unreachable code is a decision, and the decision must be executable.

`app/mainai_executive/*` and `app/problem_learning/*` are not reached by any router or worker. The
dead-seam scan reported that as 67 unreferenced functions; a report cannot tell you whether the code
is missing integration, obsolete, or deliberately held back. The two packages turn out to be
different answers, and both need to stay what they are:

  * `mainai_executive` is a NON-AUTHORIZING composed loop — it plans, looks around and dry-runs, and
    its own module docstring says "Provider activation remains disabled". It is held back on purpose,
    because promoting it to an authority is the founder's `first_cycle_go` decision, not a refactor.
  * `problem_learning` is a pure, owner-scoped learning service with no provider calls and no surface
    the founder can reach. That is missing integration, and wiring it would define what MainAI treats
    as a remembered decision — a product decision, recorded as such.

This file locks the properties that make those classifications true. If someone later wires the
executive loop into an authorized path, or teaches problem_learning to call a provider, these tests
fail and force the change to be argued instead of slipped in.
"""
import ast
import json
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent
EXECUTIVE = BACKEND / "app" / "mainai_executive"
PROBLEM_LEARNING = BACKEND / "app" / "problem_learning"

# Calls that would turn the executive loop from a planner into an authority.
AUTHORIZING_CALLS = {
    "authorize_work_candidate",
    "create_pull_request",
    "create_or_update_file",
    "create_branch",
    "chat_with_fallback",
    "subprocess",
}


def _python_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.py"))


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    return getattr(func, "attr", None)


def test_the_executive_cycle_never_authorizes_work():
    offenders = []
    for path in _python_files(EXECUTIVE):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _call_name(node)
                if name in AUTHORIZING_CALLS:
                    offenders.append(f"{path.name}:{node.lineno} calls {name}()")
                # `activate_provider=True` anywhere would make a dry-run a real run.
                for keyword in node.keywords:
                    if (
                        keyword.arg == "activate_provider"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value is True
                    ):
                        offenders.append(f"{path.name}:{node.lineno} passes activate_provider=True")
    assert not offenders, "the executive loop must stay non-authorizing: " + "; ".join(offenders)


def test_problem_learning_makes_no_provider_calls():
    forbidden_imports = {"httpx", "openai", "anthropic"}
    offenders = []
    for path in _python_files(PROBLEM_LEARNING):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in forbidden_imports:
                        offenders.append(f"{path.name}:{node.lineno} imports {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.split(".")[0] in forbidden_imports or module.startswith("app.providers"):
                    offenders.append(f"{path.name}:{node.lineno} imports from {module}")
    assert not offenders, "problem learning is deterministic by contract: " + "; ".join(offenders)


def test_no_router_reaches_the_dormant_packages():
    """If a router ever imports them, the classification in the registry becomes a lie."""
    hits = []
    for path in (BACKEND / "app" / "routers").glob("*.py"):
        text = path.read_text()
        if "mainai_executive" in text or "problem_learning" in text:
            hits.append(path.name)
    assert hits == [], f"these routers now reach a dormant package: {hits}"


def test_each_dormant_package_is_registered_with_a_founder_decision_and_a_gap():
    registry = json.loads((REPO / "requirements" / "registry.json").read_text())
    by_module = {}
    for entry in registry["requirements"]:
        by_module.setdefault(entry["responsible_module"], []).append(entry)

    expected = {
        "backend/app/mainai_executive/loop.py": "REQ-AUTONOMY-001",
        "backend/app/problem_learning/service.py": "REQ-MEM-002",
    }
    for module, requirement_id in expected.items():
        entries = by_module.get(module, [])
        assert entries, f"{module} is unreachable but not recorded in the registry"
        entry = next((e for e in entries if e["id"] == requirement_id), None)
        assert entry is not None, f"{module} must be covered by {requirement_id}"
        assert entry["status"] == "needs_founder_decision", (
            f"{requirement_id} must be an open decision, not a quiet 'verified'"
        )
        assert entry["gaps"], f"{requirement_id} must state what is missing in writing"
