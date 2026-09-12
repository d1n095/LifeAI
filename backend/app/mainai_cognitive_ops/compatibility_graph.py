"""Compatibility Graph. See docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md.

Pure builder mirroring `app.mainai_research.investigation_graph`'s exact technique: validates
every edge references a declared node, raises on dangling references, computes upstream/
downstream reachability for blast-radius analysis. No new database table -- a caller who wants
this durable stores it the same way `investigation_graph` does (as JSON alongside the relevant
evidence/decision record)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CompatibilityEdge:
    """producer -> consumer, e.g. "module A calls module B", "schema X consumed by Y",
    "migration affects table Z", "event producer -> listener"."""

    producer: str
    consumer: str
    kind: str


@dataclass(frozen=True)
class CompatibilityGraph:
    nodes: frozenset[str]
    edges: tuple[CompatibilityEdge, ...] = field(default_factory=tuple)

    def downstream_of(self, node: str) -> frozenset[str]:
        return _reachable(self.edges, {node}, forward=True) - {node}

    def upstream_of(self, node: str) -> frozenset[str]:
        return _reachable(self.edges, {node}, forward=False) - {node}


def _reachable(edges: tuple[CompatibilityEdge, ...], start: set[str], *, forward: bool) -> frozenset[str]:
    seen = set(start)
    frontier = set(start)
    while frontier:
        nxt = set()
        for e in edges:
            src, dst = (e.producer, e.consumer) if forward else (e.consumer, e.producer)
            if src in frontier and dst not in seen:
                nxt.add(dst)
        seen |= nxt
        frontier = nxt
    return frozenset(seen)


def build_compatibility_graph(*, nodes: tuple[str, ...], edges: tuple[CompatibilityEdge, ...]) -> CompatibilityGraph:
    node_set = frozenset(nodes)
    for e in edges:
        if e.producer not in node_set:
            raise ValueError(f"edge references undeclared producer node {e.producer!r}")
        if e.consumer not in node_set:
            raise ValueError(f"edge references undeclared consumer node {e.consumer!r}")
    return CompatibilityGraph(nodes=node_set, edges=edges)


def blast_radius(graph: CompatibilityGraph, changed_nodes: tuple[str, ...]) -> frozenset[str]:
    """UPSTREAM IMPACT + DOWNSTREAM IMPACT: every node reachable either forward (consumers of
    the change) or backward (producers the change itself depends on being consistent with),
    excluding the changed nodes themselves."""

    affected: set[str] = set()
    for node in changed_nodes:
        affected |= graph.downstream_of(node)
        affected |= graph.upstream_of(node)
    return frozenset(affected) - set(changed_nodes)
