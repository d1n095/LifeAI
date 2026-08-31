"""Missing-piece detector — inspect existing implementation before proposing new systems.

Find the 80–90% already present; identify the missing 10–20%; reuse primitives.
Does NOT authorize building anything.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MissingPieceFinding:
    request_summary: str
    existing_modules: list[str]
    likely_coverage_pct: int
    missing_pieces: list[str]
    reuse_recommendation: str
    propose_new_subsystem: bool  # always False unless genuinely empty


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


def _module_exists(dotted: str) -> bool:
    try:
        importlib.import_module(dotted)
        return True
    except Exception:
        return False


def detect_missing_pieces(*, founder_request: str) -> dict[str, Any]:
    """Heuristic reuse scan — deterministic keyword → package map, not LLM architecture."""
    lower = founder_request.lower()
    matched: list[str] = []
    existing: list[str] = []
    missing: list[str] = []

    for keyword, packages in _CAPABILITY_MAP.items():
        if keyword.replace("_", " ") in lower or keyword in lower:
            matched.append(keyword)
            for pkg in packages:
                if _module_exists(pkg):
                    existing.append(pkg)
                else:
                    missing.append(f"module_absent:{pkg}")

    # Generic glue gaps that often dominate false "new system" proposals.
    glue_checks = [
        ("runtime wiring", "app.mainai_executive"),
        ("work candidates", "app.work_candidates"),
        ("active context", "app.active_context"),
        ("lessons", "app.mainai_execution.lessons"),
    ]
    for label, pkg in glue_checks:
        if _module_exists(pkg):
            if pkg not in existing:
                existing.append(pkg)
        else:
            missing.append(f"missing:{label}:{pkg}")

    coverage = 0
    if existing:
        coverage = min(95, 70 + 5 * len(existing) - 10 * len(missing))
        coverage = max(40, coverage)
    elif matched:
        coverage = 20
    else:
        coverage = 50  # unknown area — do not claim empty; claim uncertain

    propose_new = coverage < 25 and len(existing) == 0
    return {
        "request_summary": founder_request[:200],
        "matched_capabilities": matched,
        "existing_modules": existing,
        "likely_coverage_pct": coverage,
        "missing_pieces": missing
        or (
            ["runtime_wiring_or_tests_or_observability"]
            if coverage >= 70
            else ["insufficient_existing_primitives_mapped"]
        ),
        "reuse_recommendation": (
            "Reuse existing modules; wire composed path; do not add parallel subsystem."
            if coverage >= 60
            else "Sparse coverage — confirm gap before designing new architecture."
        ),
        "propose_new_subsystem": propose_new,
        "evidence_basis": "import_existence_scan",
        "claimed_as_verified_implementation": False,
    }


def list_app_packages() -> list[str]:
    """Debug helper — enumerate top-level app packages for audits."""
    import app as app_pkg

    return sorted(m.name for m in pkgutil.iter_modules(app_pkg.__path__))
