"""Graph-style traversal over the existing `source_relationships` edges — FR-23/FR-09,
P3 increment #1 in docs/FOUNDER_REQUIREMENTS_BASELINE_v1.md ("recursive CTE over
source_relationships ... pure SQL, no GraphRAG framework").

Scope decisions (each one deliberate, not an omission):

- **No new tables, no migration, no second engine.** Everything here queries the existing
  `source_relationships` and `documents` rows (migrations 0006+, already RLS-protected).
  The baseline's own constraint is "no framework without evidence": this layer makes the
  graph *traversable* so the hybrid-retrieval recall/precision of the edge-augmented model
  can be measured on real corpora BEFORE any GraphRAG-style pipeline is considered.
- **Owner-scoped end to end, RLS composes.** Every SQL statement filters `owner_id` (on
  `source_relationships`) / `uploaded_by` (on `documents`) explicitly — the same discipline
  as `detect_conflicts()` in app/rag/trust.py — and the session's
  `app.current_user_id` RLS setting applies on top, exactly as it does for every other
  read path. A caller that forgets the owner filter gets nothing back, not everything.
- **Cycle-safe by construction.** Each walk carries the set of source ids it has already
  visited (`source_path`) and refuses to step anywhere already on its own path, so a
  founder-recorded `A supersedes B` + `B contradicts A` loop terminates instead of
  infinite-recursing (Postgres would otherwise run the recursive term forever).
- **No model leg, no first_cycle dependency.** This is deterministic graph machinery over
  edges a human (or a future gated model leg, FR-14) recorded — it never invents edges and
  never judges content, so it needs no LLM and is immediately usable at composition.

Two capabilities fall out of one recursive CTE:

1. `traverse_source_graph()` — general traversal (outbound/inbound/both, relationship-type
   filter, depth cap) returning every hop with its full edge path. This is the FR-23
   "graph traversal queries" half.
2. `find_cross_domain_connections()` — the FR-09 "automatic cross-domain discovery" half,
   still deliberately narrow: among the traversed paths it keeps only those whose terminal
   source lives in a DIFFERENT project (or the unfiled bucket) than the root's, and by
   default only INDIRECT ones (`min_depth=2`) — a depth-1 cross-project edge is something
   the founder explicitly recorded and the Library UI already renders, so it is not a
   *discovery*. Detection is structural (project_id comparison), never semantic: it cannot
   claim two sources "disagree" or "relate" in meaning — it can only report that they are
   connected through the graph across a domain boundary, with the exact path as evidence.

`annotate_source_graph_nodes()` supplements both with the node metadata a caller needs to
act on traversal results (title, project, active_truth_status, classification) — the
"readiness joins" the P3 increment names, served from the documents table itself rather
than a separate engine.
"""

import enum
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

# Hard ceiling for `max_depth`, so a caller bug can never turn a dense graph into an
# unbounded recursive walk. `source_path` cycle-breaking already guarantees termination;
# this bounds the DEPTH (and with it the worst-case simple-path count) per root.
MAX_DEPTH_HARD_CAP = 12

# Hard ceiling on returned rows per walk — a runaway-result guard for very dense graphs.
# Deterministic ordering below makes the truncation reproducible.
MAX_ROWS_HARD_CAP = 5000

_ALL_DIRECTIONS = ("outbound", "inbound")


@dataclass
class GraphHop:
    """One edge traversed from one of the requested roots. `edge_path`/`source_path` are the
    full chain (in discovery order) that led here — the evidence a caller needs to show WHY
    a source was reached. For `inbound` walks the chain reads upstream (against edge
    direction), while `from_source_id`/`to_source_id` always report the edge's own direction
    exactly as recorded."""

    root_source_id: uuid.UUID
    direction: str
    from_source_id: uuid.UUID
    to_source_id: uuid.UUID
    relationship_type: str
    depth: int
    edge_path: list[uuid.UUID] = field(default_factory=list)
    source_path: list[uuid.UUID] = field(default_factory=list)


@dataclass
class CrossDomainLink:
    """An indirect, cross-project connection discovered through the graph (FR-09). Only the
    structural facts — which source reached which, across which project boundary, through
    which path. No semantic judgement is attached."""

    root_source_id: uuid.UUID
    reached_source_id: uuid.UUID
    root_project_id: uuid.UUID | None
    reached_project_id: uuid.UUID | None
    direction: str
    depth: int
    edge_path: list[uuid.UUID] = field(default_factory=list)
    source_path: list[uuid.UUID] = field(default_factory=list)


@dataclass
class GraphNodeInfo:
    """Node metadata for acting on traversal results — title/project/active truth
    status/classification, read from the documents table (never fabricated)."""

    source_id: uuid.UUID
    title: str
    project_id: uuid.UUID | None
    active_truth_status: str | None
    classification: str | None


def _normalize_types(relationship_types) -> list[str] | None:
    if relationship_types is None:
        return None
    # RelationshipType is a str-mixin Enum, but on Python 3.11+ str(member) is
    # "RelationshipType.supersedes", not "supersedes" — always take .value from enum members
    # so the SQL filter matches the stored enum text exactly.
    types = [t.value if isinstance(t, enum.Enum) else str(t) for t in relationship_types]
    return types or None


def _validate_depth(max_depth: int) -> int:
    depth = int(max_depth)
    if depth < 1:
        depth = 1
    if depth > MAX_DEPTH_HARD_CAP:
        depth = MAX_DEPTH_HARD_CAP
    return depth


def _walk_sql(direction: str) -> str:
    """One recursive CTE for the given direction. `source_path` starts with the root and
    grows with each newly reached source; the recursive term refuses any edge whose newly
    reached endpoint is already on the walk's own path (cycle breaking) and stops at
    `max_depth`. `depth` is 1 for the seed edge."""
    if direction == "outbound":
        seed_match = "r.from_source_id = ANY(CAST(:roots AS uuid[]))"
        seed_head = "r.from_source_id::text"
        seed_new = "r.to_source_id::text"
        seed_path = "ARRAY[r.from_source_id::text, r.to_source_id::text]"
        join_edge = "r.from_source_id = w.to_source_id"
        step_new = "r.to_source_id::text"
    else:  # inbound — walk upstream against the recorded edge direction
        seed_match = "r.to_source_id = ANY(CAST(:roots AS uuid[]))"
        seed_head = "r.to_source_id::text"
        seed_new = "r.from_source_id::text"
        seed_path = "ARRAY[r.to_source_id::text, r.from_source_id::text]"
        join_edge = "r.to_source_id = w.from_source_id"
        step_new = "r.from_source_id::text"

    type_filter = "(CAST(:types AS text[]) IS NULL OR r.relationship_type::text = ANY(CAST(:types AS text[])))"
    return f"""
        WITH RECURSIVE walk(owner_id, root_source_id, direction, from_source_id, to_source_id,
                            relationship_type, depth, edge_path, source_path) AS (
            SELECT r.owner_id,
                   {seed_head.replace('::text', '')},
                   '{direction}',
                   r.from_source_id,
                   r.to_source_id,
                   r.relationship_type::text,
                   1,
                   ARRAY[r.id::text],
                   {seed_path}
            FROM source_relationships r
            WHERE r.owner_id = :owner
              AND {seed_match}
              AND {type_filter}
            UNION ALL
            SELECT w.owner_id,
                   w.root_source_id,
                   w.direction,
                   r.from_source_id,
                   r.to_source_id,
                   r.relationship_type::text,
                   w.depth + 1,
                   w.edge_path || r.id::text,
                   w.source_path || {step_new}
            FROM walk w
            JOIN source_relationships r
              ON r.owner_id = w.owner_id
             AND {join_edge}
            WHERE w.depth < :max_depth
              AND NOT ({step_new} = ANY(w.source_path))
              AND {type_filter}
        )
        SELECT root_source_id::text, direction, from_source_id::text, to_source_id::text,
               relationship_type, depth, edge_path, source_path
        FROM walk
        ORDER BY depth, relationship_type, from_source_id::text, to_source_id::text, edge_path
        LIMIT :max_rows
    """


def _hop_from_row(row) -> GraphHop:
    return GraphHop(
        root_source_id=uuid.UUID(row.root_source_id),
        direction=row.direction,
        from_source_id=uuid.UUID(row.from_source_id),
        to_source_id=uuid.UUID(row.to_source_id),
        relationship_type=row.relationship_type,
        depth=int(row.depth),
        edge_path=[uuid.UUID(e) for e in row.edge_path],
        source_path=[uuid.UUID(s) for s in row.source_path],
    )


def _root_project_map(db: Session, owner_id: uuid.UUID, source_ids) -> dict[uuid.UUID, uuid.UUID | None]:
    """project_id per document, owner-scoped. `uploaded_by` is documents' owner column (its
    RLS predicate) — source_relationships' `owner_id` does not exist on documents."""
    if not source_ids:
        return {}
    rows = db.execute(
        text("SELECT id::text, project_id::text FROM documents WHERE uploaded_by = :owner AND id = ANY(CAST(:ids AS uuid[]))"),
        {"owner": str(owner_id), "ids": [str(s) for s in source_ids]},
    ).all()
    return {uuid.UUID(r[0]): (uuid.UUID(r[1]) if r[1] else None) for r in rows}


def traverse_source_graph(
    db: Session,
    owner_id: uuid.UUID,
    root_source_ids,
    *,
    direction: str = "outbound",
    relationship_types=None,
    max_depth: int = 3,
    max_rows: int = MAX_ROWS_HARD_CAP,
) -> list[GraphHop]:
    """Every edge reachable from `root_source_ids` through recorded `source_relationships`,
    as flat hops with full paths. `direction` selects which way edges are followed:
    `outbound` follows from→to, `inbound` walks upstream (to→from), `both` runs each walk
    separately (outbound results first, then inbound — never interleaved, so callers get a
    deterministic order). `relationship_types` filters by the recorded edge type.
    `max_depth` counts edges (1 = the roots' own direct edges) and is clamped to
    [1, MAX_DEPTH_HARD_CAP]. Empty roots return an empty list — never an error."""
    if direction not in ("outbound", "inbound", "both"):
        raise ValueError(f"direction must be outbound|inbound|both, got {direction!r}")
    roots = list(dict.fromkeys(root_source_ids))  # de-duplicated, order preserved
    if not roots:
        return []
    depth = _validate_depth(max_depth)
    types = _normalize_types(relationship_types)
    limit = max(1, int(max_rows))

    params = {
        "owner": str(owner_id),
        "roots": [str(r) for r in roots],
        "types": types,
        "max_depth": depth,
        "max_rows": limit,
    }

    hops: list[GraphHop] = []
    directions = _ALL_DIRECTIONS if direction == "both" else (direction,)
    for dir_name in directions:
        rows = db.execute(text(_walk_sql(dir_name)), params).all()
        hops.extend(_hop_from_row(row) for row in rows)
    return hops


def find_cross_domain_connections(
    db: Session,
    owner_id: uuid.UUID,
    root_source_ids,
    *,
    direction: str = "both",
    relationship_types=None,
    max_depth: int = 3,
    min_depth: int = 2,
) -> list[CrossDomainLink]:
    """FR-09's structural half: the traversed paths whose terminal source belongs to a
    DIFFERENT project than the root's. "Different" is Postgres's own IS DISTINCT FROM
    comparison, so the unfiled bucket (project_id NULL) counts as its own domain — a path
    between a filed and an unfiled source is cross-domain, two unfiled ones are not.

    `min_depth=2` (default) keeps the word "discovery" honest: a depth-1 cross-project edge
    was explicitly recorded by the founder and is already rendered in the Library UI, so it
    is not reported as discovered. Pass min_depth=1 to include direct edges too.

    Deduplication: one link per (root, terminal, direction) pair — the SHALLOWEST path wins
    (ties broken by edge path), because the shortest chain is both the strongest evidence
    and the least surprising to present."""
    hops = traverse_source_graph(
        db, owner_id, root_source_ids,
        direction=direction, relationship_types=relationship_types, max_depth=max_depth,
    )
    if not hops:
        return []

    terminal = {"outbound": lambda h: h.to_source_id, "inbound": lambda h: h.from_source_id}
    touched = {h.root_source_id for h in hops}
    for h in hops:
        touched.add(terminal[h.direction](h))
    project_by_id = _root_project_map(db, owner_id, touched)

    floor = max(1, int(min_depth))
    best: dict[tuple[uuid.UUID, uuid.UUID, str], GraphHop] = {}
    for h in hops:
        if h.depth < floor:
            continue
        head = terminal[h.direction](h)
        root_project = project_by_id.get(h.root_source_id)
        reached_project = project_by_id.get(head)
        if root_project is None and reached_project is None:
            continue
        if root_project == reached_project:
            continue
        key = (h.root_source_id, head, h.direction)
        current = best.get(key)
        if current is None or (h.depth, [str(e) for e in h.edge_path]) < (
            current.depth, [str(e) for e in current.edge_path]
        ):
            best[key] = h

    links = []
    for (root_id, head, dir_name), h in best.items():
        links.append(
            CrossDomainLink(
                root_source_id=root_id,
                reached_source_id=head,
                root_project_id=project_by_id.get(root_id),
                reached_project_id=project_by_id.get(head),
                direction=dir_name,
                depth=h.depth,
                edge_path=list(h.edge_path),
                source_path=list(h.source_path),
            )
        )
    links.sort(key=lambda l: (l.depth, str(l.root_source_id), str(l.reached_source_id)))
    return links


def annotate_source_graph_nodes(db: Session, owner_id: uuid.UUID, source_ids) -> list[GraphNodeInfo]:
    """Node metadata for traversal results (the "readiness joins" of P3 increment #1):
    title, project, active truth status and classification, owner-scoped, read from
    `documents`. Sources the owner cannot see are silently absent — never fabricated with
    blank titles."""
    ids = list(dict.fromkeys(source_ids))
    if not ids:
        return []
    rows = db.execute(
        text(
            "SELECT id::text, title, project_id::text, active_truth_status::text, classification::text "
            "FROM documents WHERE uploaded_by = :owner AND id = ANY(CAST(:ids AS uuid[])) "
            "ORDER BY title, id::text"
        ),
        {"owner": str(owner_id), "ids": [str(s) for s in ids]},
    ).all()
    return [
        GraphNodeInfo(
            source_id=uuid.UUID(r[0]),
            title=r[1],
            project_id=uuid.UUID(r[2]) if r[2] else None,
            active_truth_status=r[3],
            classification=r[4],
        )
        for r in rows
    ]
