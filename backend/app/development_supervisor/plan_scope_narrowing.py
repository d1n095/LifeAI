"""Derive task-level path/capability ceilings from a Safe-Planner-validated plan.

WHY (Autonomy Activation B4 / FIRST_AUTONOMOUS_TASK_BLOCKER_MAP):

After Safe Planner ACCEPTS a provider-assisted `PlanCandidate`, execution should run under
the *minimal* surface cited by that exact accepted plan — still never more than the founder
envelope. This is NOT wired when `production_entry` builds initial WorkBindings: at that
point no provider plan exists yet.

Correct runtime ordering:

    provider planning
    → Safe Planner ACCEPTED
    → derive cited paths/capabilities from the exact accepted candidate
    → fail closed on escape / empty write scope
    → intersect with founder envelope
    → narrow effective OperatorContext / execution boundary
    → Driver executes that exact accepted plan

Hard rules:
  - PLANNER OUTPUT != FOUNDER AUTHORITY. This module never invents new paths or capabilities
    that the envelope did not already authorize.
  - Result is always `intersection(envelope_ceiling, plan_citations)`.
  - If the plan cites a path/capability outside the envelope, fail closed.
  - Missing path citations → empty path set (fail closed for write work), never "fall back
    to full envelope".

Final wire belongs immediately after Safe Planner ACCEPTED (inside Supervisor /
plan_with_provider → Driver handoff), not in production_entry WorkBinding construction.
If OperatorContext currently carries paths but not capabilities, the capability half of
narrowing needs a real enforcement boundary at that handoff — do not document a capability
restriction that is not enforced.
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

    Call only AFTER Safe Planner ACCEPTED the candidate — never while constructing the
    initial production_entry WorkBinding (no provider plan exists there yet).
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
