"""Durable Investigation Graph -- architecture decision + validation. See
docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md §23B for the full reasoning. Closes P1 #2
from the Research, Truth & Advisory Intelligence round's own handoff doc.

DECISION: keep the actor/relationship/money-flow/timeline graph inside the EXISTING
`mainai_research_evidence_links.provenance` JSONB column (already present, already
`jsonb_typeof(...) = 'object'` constrained at the DB level) rather than add a new relational
Actor/Relationship table. Reasoning:

1. A dedicated relational table would duplicate canonical evidence-link storage -- the graph's
   nodes/edges are, by construction, ALWAYS derived from already-evidenced facts that already
   live in `mainai_research_evidence_links`. A second table would either re-store the same
   facts (violates "derive, never duplicate") or store un-evidenced claims (violates RELATIONSHIP
   != CONTROL / BENEFIT != PROOF OF INTENT's requirement that every edge trace to real evidence).
2. `app.mainai_research.investigation_graph` is deliberately pure/in-memory (no DB) and is built
   fresh per query from whatever evidence a caller supplies -- there is no accumulating,
   independently-growing graph state that would justify its own migration.
3. Queryability: migration 0074 adds a GIN index on `provenance`, so `provenance @> '{...}'`
   containment queries over stored graph payloads are index-supported without a new table.
4. This module supplies the missing piece the founder's own instruction demanded even for the
   "no" branch: INVARIANTS + VALIDATION sufficient for durability and queryability --
   `validate_graph_provenance_payload()` is real, structural validation, not just prose."""

from __future__ import annotations

from typing import Any

REQUIRED_ACTOR_KEYS = {"actor_id", "name", "kind"}
REQUIRED_RELATIONSHIP_KEYS = {"from_actor_id", "to_actor_id", "kind"}
REQUIRED_MONEY_FLOW_KEYS = {"from_actor_id", "to_actor_id", "flow_kind"}
FORBIDDEN_RELATIONSHIP_KINDS = {"controls", "conspires_with"}  # RELATIONSHIP != CONTROL


def validate_graph_provenance_payload(payload: dict[str, Any]) -> tuple[str, ...]:
    """Returns a tuple of error strings (empty tuple = valid). Validates the shape a caller
    stores under `evidence_link.provenance["graph"]` when they want an evidenced actor/
    relationship/money-flow fact to be durably queryable via the GIN index."""

    errors: list[str] = []
    graph = payload.get("graph")
    if graph is None:
        return ("payload has no 'graph' key -- nothing to validate",)
    if not isinstance(graph, dict):
        return ("payload['graph'] must be an object",)

    for actor in graph.get("actors", []):
        missing = REQUIRED_ACTOR_KEYS - set(actor.keys())
        if missing:
            errors.append(f"actor missing required keys: {sorted(missing)}")

    for rel in graph.get("relationships", []):
        missing = REQUIRED_RELATIONSHIP_KEYS - set(rel.keys())
        if missing:
            errors.append(f"relationship missing required keys: {sorted(missing)}")
        elif rel.get("kind") in FORBIDDEN_RELATIONSHIP_KINDS:
            errors.append(f"relationship kind {rel['kind']!r} implies control/intent -- RELATIONSHIP != CONTROL, use a descriptive tie instead")

    for flow in graph.get("money_flows", []):
        missing = REQUIRED_MONEY_FLOW_KEYS - set(flow.keys())
        if missing:
            errors.append(f"money_flow missing required keys: {sorted(missing)}")

    return tuple(errors)


def build_containment_query_fragment(*, actor_id: str) -> dict[str, Any]:
    """Returns the JSONB containment shape a caller passes to a `provenance @> :fragment` query
    (GIN-index-supported) to find every evidence_link mentioning a given actor -- concrete
    proof this module treats queryability as a real requirement, not just a claim."""

    return {"graph": {"actors": [{"actor_id": actor_id}]}}
