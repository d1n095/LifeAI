"""Derive task-level path/capability ceilings from a Safe-Planner-validated plan.

WHY (Autonomy Activation B4 / FIRST_AUTONOMOUS_TASK_BLOCKER_MAP):

`production_entry` currently copies the full execution-envelope path/capability ceiling onto
every `WorkBinding`. That is correct as a hard upper bound, but it is not the *minimal*
surface for a concrete accepted plan. After Safe Planner accepts a `PlanCandidate`, the
operator should only be able to touch paths and capabilities the plan actually cited —
still never more than the founder envelope.

Hard rules:
  - PLANNER OUTPUT != FOUNDER AUTHORITY. This module never invents new paths or capabilities
    that the envelope did not already authorize.
  - Result is always `intersection(envelope_ceiling, plan_citations)`.
  - If the plan cites a path/capability outside the envelope, that is a Safe Planner bug /
    bypass attempt — we raise (fail closed), we do not silently drop and proceed with a
    widened-looking "empty" or envelope-full binding.
  - Missing citations (plan has no path-bearing steps) → empty path set (fail closed for
    write work), not "fall back to full envelope".

Final wire into `production_entry` is deferred (Claude owns adjacent authority surfaces).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.safe_planner.service import PATH_KEYS, PlanCandidate


class PlanScopeNarrowingError(ValueError):
    pass


@dataclass(frozen=True)
class NarrowedTaskScope:
    allowed_paths: tuple[str, ...]
    allowed_capabilities: tuple[str, ...]
    cited_paths: tuple[str, ...]
    cited_capabilities: tuple[str, ...]


def _paths_from_arguments(arguments: dict) -> list[str]:
    found: list[str] = []
    for key, value in (arguments or {}).items():
        if key in PATH_KEYS and isinstance(value, str) and value.strip():
            found.append(value.strip())
        elif key == "paths" and isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    found.append(item.strip())
        elif key == "arguments" and isinstance(value, list):
            # run_focused_test style: arguments=["test_foo.py"] — treat bare relative
            # path-looking strings as path citations.
            for item in value:
                if isinstance(item, str) and item.strip() and ("/" in item or item.endswith(".py")):
                    found.append(item.strip())
    return found


def extract_plan_citations(candidate: PlanCandidate) -> tuple[tuple[str, ...], tuple[str, ...]]:
    paths: list[str] = []
    capabilities: list[str] = []
    for step in candidate.steps:
        if step.capability:
            capabilities.append(step.capability)
        paths.extend(_paths_from_arguments(dict(step.arguments or {})))
    # Preserve first-seen order, drop duplicates.
    return (
        tuple(dict.fromkeys(paths)),
        tuple(dict.fromkeys(capabilities)),
    )


def narrow_task_scope_from_accepted_plan(
    *,
    envelope_paths: tuple[str, ...] | list[str],
    envelope_capabilities: tuple[str, ...] | list[str],
    candidate: PlanCandidate,
) -> NarrowedTaskScope:
    """Intersect founder envelope ceiling with citations from an already-validated plan.

    Call only AFTER Safe Planner ACCEPTED the candidate. Does not re-validate plan safety.
    """
    envelope_path_set = {p for p in envelope_paths if p}
    envelope_cap_set = {c for c in envelope_capabilities if c}
    cited_paths, cited_caps = extract_plan_citations(candidate)

    escaped_paths = [p for p in cited_paths if p not in envelope_path_set]
    if escaped_paths:
        raise PlanScopeNarrowingError(
            f"accepted plan cites paths outside the founder envelope: {escaped_paths}"
        )
    escaped_caps = [c for c in cited_caps if c not in envelope_cap_set]
    if escaped_caps:
        raise PlanScopeNarrowingError(
            f"accepted plan cites capabilities outside the founder envelope: {escaped_caps}"
        )

    return NarrowedTaskScope(
        allowed_paths=cited_paths,
        allowed_capabilities=cited_caps,
        cited_paths=cited_paths,
        cited_capabilities=cited_caps,
    )
