"""Offline MainAI capability audit — APIs enhance; they must not define existence."""

from __future__ import annotations

from typing import Any


def audit_offline_capabilities() -> dict[str, Any]:
    """Import-existence audit of what should work without external providers."""
    import importlib

    modules = {
        "memory": "app.founder_memory",
        "lessons": "app.mainai_execution.lessons",
        "planning": "app.mainai_execution.planner",
        "workforce_local": "app.workforce",
        "capability_reality": "app.capability_reality",
        "school": "app.mainai_school",
        "kill_switch": "app.workforce.kill_switch",
        "work_candidates": "app.work_candidates",
    }
    available: dict[str, bool] = {}
    for name, path in modules.items():
        try:
            importlib.import_module(path)
            available[name] = True
        except Exception:
            available[name] = False

    # Executive may only exist after #235 merges
    try:
        importlib.import_module("app.mainai_executive")
        available["executive"] = True
    except Exception:
        available["executive"] = False

    return {
        "available": available,
        "offline_meaningful": all(
            available.get(k) for k in ("memory", "lessons", "capability_reality", "school")
        ),
        "requires_external_api_to_exist": False,
        "internet_enhances_not_defines": True,
        "provider_invoke_still_disabled": True,
    }
