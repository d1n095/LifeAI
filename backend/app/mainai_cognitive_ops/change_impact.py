"""Change Impact Analysis. See docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md.

Composes `compatibility_graph.py` to estimate blast radius BEFORE a change and verify actual
blast radius AFTER -- a mismatch (something affected that was not predicted) is itself a
systemic-debugging signal (NEW_CONFLICT_CHECK, see `systemic_debugging.py`), not something to
silently ignore."""

from __future__ import annotations

from dataclasses import dataclass

from app.mainai_cognitive_ops.compatibility_graph import CompatibilityGraph, blast_radius


@dataclass(frozen=True)
class ImpactEstimate:
    changed_nodes: tuple[str, ...]
    estimated_affected: frozenset[str]


@dataclass(frozen=True)
class ImpactVerification:
    estimated_affected: frozenset[str]
    actually_affected: frozenset[str]
    unexpected_impact: frozenset[str]  # affected in reality but NOT predicted -- a real gap
    over_estimated: frozenset[str]  # predicted but not actually affected -- harmless, just noted
    matches: bool


def estimate_impact(graph: CompatibilityGraph, changed_nodes: tuple[str, ...]) -> ImpactEstimate:
    return ImpactEstimate(changed_nodes=changed_nodes, estimated_affected=blast_radius(graph, changed_nodes))


def verify_impact(estimate: ImpactEstimate, *, actually_affected: frozenset[str]) -> ImpactVerification:
    unexpected = actually_affected - estimate.estimated_affected
    over = estimate.estimated_affected - actually_affected
    return ImpactVerification(
        estimated_affected=estimate.estimated_affected, actually_affected=actually_affected,
        unexpected_impact=unexpected, over_estimated=over, matches=not unexpected,
    )


def select_regression_scope(graph: CompatibilityGraph, changed_nodes: tuple[str, ...]) -> frozenset[str]:
    """Neither "run everything" nor "run nothing but the changed file's own tests" -- the
    regression scope is exactly the changed nodes plus their full blast radius."""

    return frozenset(changed_nodes) | blast_radius(graph, changed_nodes)
