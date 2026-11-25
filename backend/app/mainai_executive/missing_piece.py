"""Missing-piece detector — inspect existing implementation before proposing new systems.

Find the 80–90% already present; identify the missing 10–20%; reuse primitives.
Does NOT authorize building anything.

Evidence is reported as explicit states (``module_located`` … ``runtime_verified``), never as
a coverage percentage. An importable module is not a working capability.
"""

from __future__ import annotations

import pkgutil
from dataclasses import dataclass
from typing import Any

from app.mainai_executive.evidence_state import assess_modules

# Known MainAI capability areas → package roots already in-tree.
_CAPABILITY_MAP: dict[str, tuple[str, ...]] = {
    "memory": ("app.founder_memory", "app.inspectable_memory", "app.memory_work_linkage", "app.memory_threads"),
    "workforce": ("app.workforce",),
    "planning": ("app.safe_planner", "app.mainai_execution", "app.development_driver"),
    "authority": ("app.execution_envelopes", "app.development_supervisor"),
    "recovery": ("app.mainai_executive.continuity", "app.workforce.failure"),
    "self_model": ("app.capability_reality",),
    "language": ("app.founder_memory_signals", "app.context"),
    "observability": ("app.inspectable_memory", "app.mainai_executive.observability"),
    "startup": ("app.mainai_startup_readiness",),
    "spend": ("app.provider_spend",),
}

# Generic glue gaps that often dominate false "new system" proposals. These are checked for
# every request; they never count as a matched capability on their own.
_GLUE_MODULES: tuple[tuple[str, str], ...] = (
    ("runtime wiring", "app.mainai_executive"),
    ("work candidates", "app.work_candidates"),
    ("active context", "app.active_context"),
    ("lessons", "app.mainai_execution.lessons"),
)


@dataclass(frozen=True)
class MissingPieceFinding:
    request_summary: str
    existing_modules: list[str]
    evidence_states: dict[str, str]
    missing_pieces: list[str]
    reuse_recommendation: str
    propose_new_subsystem: bool  # always False unless genuinely empty
    operational_claim: bool


def _module_exists(dotted: str) -> bool:
    """Kept for callers that only need location; prefer ``assess_modules`` for evidence."""
    import importlib

    try:
        importlib.import_module(dotted)
        return True
    except Exception:
        return False


def detect_missing_pieces(
    *,
    founder_request: str,
    repo_root: str | None = None,
    integration_refs: dict[str, tuple[str, ...] | list[str]] | None = None,
    runtime_refs: dict[str, tuple[str, ...] | list[str]] | None = None,
) -> dict[str, Any]:
    """Deterministic keyword → package map plus explicit evidence states.

    ``integration_refs``/``runtime_refs`` are caller-supplied observations. Without them the
    strongest possible state is ``tests_observed`` — file mentions, not passing tests.
    """
    lower = founder_request.lower()
    matched: list[str] = []
    modules: list[str] = []

    for keyword, packages in _CAPABILITY_MAP.items():
        if keyword.replace("_", " ") in lower or keyword in lower:
            matched.append(keyword)
            modules.extend(packages)

    for _label, pkg in _GLUE_MODULES:
        modules.append(pkg)

    # Stable order, no duplicates.
    unique_modules = list(dict.fromkeys(modules))
    evidence = assess_modules(
        unique_modules,
        repo_root=repo_root,
        integration_refs=integration_refs,
        runtime_refs=runtime_refs,
    )
    states: dict[str, str] = evidence["states"]
    existing = [module for module, state in states.items() if state != "unknown"]
    absent = [module for module, state in states.items() if state == "unknown"]

    missing_pieces: list[str] = [f"module_absent:{module}" for module in absent]
    if not missing_pieces:
        if evidence["strongest_state"] != "runtime_verified":
            missing_pieces.append("runtime_verification_not_observed")
        elif evidence["weakest_state"] in ("module_located", "implementation_inspected", "tests_observed"):
            missing_pieces.append("weaker_sibling_modules_not_verified")

    propose_new = bool(matched) and not existing
    strongest = evidence["strongest_state"]
    if strongest == "runtime_verified":
        recommendation = (
            "Operational evidence exists for at least one module. Reuse it; verify the weaker "
            "siblings before extending. Do not add a parallel subsystem."
        )
    elif strongest in ("integration_verified", "tests_observed", "implementation_inspected"):
        recommendation = (
            "Implementation exists. Extend or repair it behind its current contract; a new "
            "subsystem is not justified by location or tests alone."
        )
    elif strongest == "module_located":
        recommendation = "Module located only — inspect the implementation before deciding."
    elif matched:
        recommendation = "Sparse evidence — confirm the gap before designing new architecture."
    else:
        recommendation = (
            "No capability keyword matched. That is not evidence of absence; investigate before "
            "proposing a new component."
        )

    return {
        "request_summary": founder_request[:200],
        "matched_capabilities": matched,
        "existing_modules": existing,
        "evidence_states": states,
        "evidence_detail": [item.to_dict() for item in evidence["modules"]],
        "strongest_state": strongest,
        "weakest_state": evidence["weakest_state"],
        "missing_pieces": missing_pieces,
        "reuse_recommendation": recommendation,
        "propose_new_subsystem": propose_new,
        "operational_claim": evidence["all_operational"],
        "evidence_basis": "explicit_evidence_states",
        "percentage": None,
        "percentage_note": "no coverage percentage is manufactured from module location",
        "claimed_as_verified_implementation": False,
    }


def list_app_packages() -> list[str]:
    """Debug helper — enumerate top-level app packages for audits."""
    import app as app_pkg

    return sorted(m.name for m in pkgutil.iter_modules(app_pkg.__path__))
