"""Tests for app/rag/graph_traverse.py — FR-23's graph traversal queries and FR-09's
structural cross-domain discovery, P3 increment #1 (docs/FOUNDER_REQUIREMENTS_BASELINE_v1.md).

Everything runs against the real local Postgres the session fixture provides (migrations
included, RLS applied) — the same discipline as tests/backend/rag/test_trust_engine.py:
documents and source_relationships are RLS-protected, so every insert re-sets
app.current_user_id first, and reads go through the same session the service functions use.
No part of the module under test touches a model leg, a migration or a second engine —
that is the increment's own scope contract, asserted here by construction.
"""

import uuid

import pytest

from app.models.document import Document, DocumentSource
from app.models.project import Project
from app.models.source_relationship import RelationshipType, SourceRelationship
from app.rag.graph_traverse import (
    MAX_DEPTH_HARD_CAP,
    GraphHop,
    annotate_source_graph_nodes,
    find_cross_domain_connections,
    traverse_source_graph,
)


def _set_rls_user(session, owner_id) -> None:
    from sqlalchemy import text as sa_text

    from app.request_context import current_user_id as current_user_id_var

    # Sets the contextvar too, not just the raw SQL setting — the service functions run
    # their own SELECTs in the transaction app/db.py's after_begin listener re-applies RLS
    # to. See test_trust_engine.py's identical note.
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_document(session, owner_id, title="Dok", project_id=None) -> Document:
    # documents and source_relationships are both RLS-protected (migration 0006) — each
    # needs app.current_user_id freshly re-set right before its own insert, since the
    # previous commit ended the transaction that setting was scoped to.
    _set_rls_user(session, owner_id)
    document = Document(title=title, source=DocumentSource.upload, uploaded_by=owner_id, project_id=project_id)
    session.add(document)
    session.commit()
    return document


def _make_project(session, owner_id, name) -> Project:
    _set_rls_user(session, owner_id)
    project = Project(name=name, created_by=owner_id)
    session.add(project)
    session.commit()
    return project


def _relate(session, owner_id, from_doc, to_doc, rel_type=RelationshipType.derived_from):
    _set_rls_user(session, owner_id)
    edge = SourceRelationship(
        owner_id=owner_id, from_source_id=from_doc.id, to_source_id=to_doc.id, relationship_type=rel_type
    )
    session.add(edge)
    session.commit()
    return edge


def _ids(hops):
    return {(h.from_source_id, h.to_source_id, h.depth) for h in hops}


# --------------------------------------------------------------------------------------
# 1. traverse_source_graph — the FR-23 half
# --------------------------------------------------------------------------------------


def test_outbound_traversal_follows_chains_in_depth_order(db_session, make_verified_user):
    user, _ = make_verified_user()
    doc_a = _make_document(db_session, user.id, "A")
    doc_b = _make_document(db_session, user.id, "B")
    doc_c = _make_document(db_session, user.id, "C")
    _relate(db_session, user.id, doc_a, doc_b)
    _relate(db_session, user.id, doc_b, doc_c)

    hops = traverse_source_graph(db_session, user.id, [doc_a.id], direction="outbound")
    assert _ids(hops) == {(doc_a.id, doc_b.id, 1), (doc_b.id, doc_c.id, 2)}
    # Every hop must report WHY it was reached: the full chain back to the root.
    deep = next(h for h in hops if h.depth == 2)
    assert deep.source_path == [doc_a.id, doc_b.id, doc_c.id]
    assert len(deep.edge_path) == 2
    assert deep.root_source_id == doc_a.id


def test_inbound_traversal_walks_upstream_against_edge_direction(db_session, make_verified_user):
    user, _ = make_verified_user()
    doc_a = _make_document(db_session, user.id, "A")
    doc_b = _make_document(db_session, user.id, "B")
    _relate(db_session, user.id, doc_a, doc_b)

    hops = traverse_source_graph(db_session, user.id, [doc_b.id], direction="inbound")
    assert _ids(hops) == {(doc_a.id, doc_b.id, 1)}
    hop = hops[0]
    # The edge is still reported in its own recorded direction; only the WALK went upstream.
    assert (hop.from_source_id, hop.to_source_id) == (doc_a.id, doc_b.id)
    assert hop.source_path == [doc_b.id, doc_a.id]


def test_relationship_type_filter_keeps_only_matching_edges(db_session, make_verified_user):
    user, _ = make_verified_user()
    doc_a = _make_document(db_session, user.id, "A")
    doc_b = _make_document(db_session, user.id, "B")
    doc_c = _make_document(db_session, user.id, "C")
    _relate(db_session, user.id, doc_a, doc_b, RelationshipType.supersedes)
    _relate(db_session, user.id, doc_a, doc_c, RelationshipType.derived_from)

    hops = traverse_source_graph(
        db_session, user.id, [doc_a.id], relationship_types=[RelationshipType.supersedes]
    )
    assert _ids(hops) == {(doc_a.id, doc_b.id, 1)}


def test_max_depth_caps_the_walk(db_session, make_verified_user):
    user, _ = make_verified_user()
    docs = [_make_document(db_session, user.id, f"D{i}") for i in range(4)]
    for frm, to in zip(docs, docs[1:]):
        _relate(db_session, user.id, frm, to)

    hops = traverse_source_graph(db_session, user.id, [docs[0].id], max_depth=2)
    assert max(h.depth for h in hops) == 2
    assert all(h.to_source_id != docs[3].id for h in hops)


def test_depth_is_clamped_to_the_hard_cap_not_trusted(db_session, make_verified_user):
    user, _ = make_verified_user()
    doc_a = _make_document(db_session, user.id, "A")
    doc_b = _make_document(db_session, user.id, "B")
    _relate(db_session, user.id, doc_a, doc_b)

    # A runaway caller value must be silently clamped, never passed to the CTE as-is.
    hops = traverse_source_graph(db_session, user.id, [doc_a.id], max_depth=10_000)
    assert hops and all(h.depth <= MAX_DEPTH_HARD_CAP for h in hops)


def test_recorded_cycles_terminate_and_never_revisit_a_source(db_session, make_verified_user):
    """A→B and B→A is exactly the loop a founder can accidentally record. The walk must
    terminate (finite hops) and must NOT re-report B→A (B is already on A's own path)."""
    user, _ = make_verified_user()
    doc_a = _make_document(db_session, user.id, "A")
    doc_b = _make_document(db_session, user.id, "B")
    _relate(db_session, user.id, doc_a, doc_b)
    _relate(db_session, user.id, doc_b, doc_a)

    hops = traverse_source_graph(db_session, user.id, [doc_a.id], max_depth=MAX_DEPTH_HARD_CAP)
    assert len(hops) == 1
    assert _ids(hops) == {(doc_a.id, doc_b.id, 1)}


def test_owner_isolation_roots_and_edges_of_other_owners_are_invisible(db_session, make_verified_user):
    """Owner-scoping is structural: neither a foreign root's edges nor a foreign EDGE between
    own roots may be traversed. source_relationships.owner_id is filtered in every statement
    and RLS (app.current_user_id) applies on top."""
    owner, _ = make_verified_user()
    other, _ = make_verified_user()

    own_a = _make_document(db_session, owner.id, "own-a")
    own_b = _make_document(db_session, owner.id, "own-b")
    foreign = _make_document(db_session, other.id, "foreign")
    _relate(db_session, other.id, own_a, foreign)  # foreign owner's edge out of own root
    _relate(db_session, other.id, own_a, own_b)  # foreign owner's edge BETWEEN own sources

    # Foreign-owner edges must be invisible. RLS is set to the querying owner so this is a
    # real traversal assertion, not an artifact of the session still pointing at `other`.
    _set_rls_user(db_session, owner.id)
    assert traverse_source_graph(db_session, owner.id, [own_a.id], direction="both") == []
    assert find_cross_domain_connections(db_session, owner.id, [own_a.id]) == []


def test_unknown_root_returns_empty_without_error(db_session, make_verified_user):
    user, _ = make_verified_user()
    assert traverse_source_graph(db_session, user.id, [uuid.uuid4()]) == []
    assert traverse_source_graph(db_session, user.id, []) == []


def test_invalid_direction_is_rejected(db_session, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_document(db_session, user.id)
    with pytest.raises(ValueError):
        traverse_source_graph(db_session, user.id, [doc.id], direction="sideways")


# --------------------------------------------------------------------------------------
# 2. find_cross_domain_connections — the FR-09 half
# --------------------------------------------------------------------------------------


def test_discovers_indirect_cross_project_paths(db_session, make_verified_user):
    """P1: A→B→(P2)→C→D  with P2's chain reached through a cross edge B→C. Root A must
    discover C (depth 2) and D (depth 3) as cross-domain; the direct B→C edge is depth 1 —
    recorded by the founder, therefore NOT a discovery by default."""
    user, _ = make_verified_user()
    p1 = _make_project(db_session, user.id, "P1")
    p2 = _make_project(db_session, user.id, "P2")
    doc_a = _make_document(db_session, user.id, "A", project_id=p1.id)
    doc_b = _make_document(db_session, user.id, "B", project_id=p1.id)
    doc_c = _make_document(db_session, user.id, "C", project_id=p2.id)
    doc_d = _make_document(db_session, user.id, "D", project_id=p2.id)
    _relate(db_session, user.id, doc_a, doc_b)
    _relate(db_session, user.id, doc_b, doc_c)
    _relate(db_session, user.id, doc_c, doc_d)

    links = find_cross_domain_connections(db_session, user.id, [doc_a.id])
    found = {(l.root_source_id, l.reached_source_id, l.depth) for l in links}
    assert found == {(doc_a.id, doc_c.id, 2), (doc_a.id, doc_d.id, 3)}
    for link in links:
        assert link.root_project_id == p1.id
        assert link.reached_project_id == p2.id
    # The evidence path must be carried through, not just the conclusion.
    deep = next(l for l in links if l.depth == 3)
    assert deep.source_path == [doc_a.id, doc_b.id, doc_c.id, doc_d.id]


def test_min_depth_one_includes_direct_cross_project_edges(db_session, make_verified_user):
    user, _ = make_verified_user()
    p1 = _make_project(db_session, user.id, "P1")
    p2 = _make_project(db_session, user.id, "P2")
    doc_a = _make_document(db_session, user.id, "A", project_id=p1.id)
    doc_b = _make_document(db_session, user.id, "B", project_id=p2.id)
    _relate(db_session, user.id, doc_a, doc_b)

    assert find_cross_domain_connections(db_session, user.id, [doc_a.id]) == []
    links = find_cross_domain_connections(db_session, user.id, [doc_a.id], min_depth=1)
    assert [(l.root_source_id, l.reached_source_id) for l in links] == [(doc_a.id, doc_b.id)]


def test_same_project_connections_are_not_cross_domain(db_session, make_verified_user):
    user, _ = make_verified_user()
    p1 = _make_project(db_session, user.id, "P1")
    doc_a = _make_document(db_session, user.id, "A", project_id=p1.id)
    doc_b = _make_document(db_session, user.id, "B", project_id=p1.id)
    doc_c = _make_document(db_session, user.id, "C", project_id=p1.id)
    _relate(db_session, user.id, doc_a, doc_b)
    _relate(db_session, user.id, doc_b, doc_c)

    assert find_cross_domain_connections(db_session, user.id, [doc_a.id]) == []


def test_unfiled_sources_form_their_own_domain_bucket(db_session, make_verified_user):
    """A filed → unfiled path IS cross-domain (IS DISTINCT FROM), two unfiled ones are not —
    otherwise the unfiled bucket would silently behave as one giant shared domain."""
    user, _ = make_verified_user()
    p1 = _make_project(db_session, user.id, "P1")
    filed = _make_document(db_session, user.id, "filed", project_id=p1.id)
    unfiled = _make_document(db_session, user.id, "unfiled", project_id=None)
    unfiled2 = _make_document(db_session, user.id, "unfiled2", project_id=None)
    _relate(db_session, user.id, filed, unfiled)
    _relate(db_session, user.id, unfiled, unfiled2)

    links = find_cross_domain_connections(db_session, user.id, [filed.id])
    # Default min_depth=2: the DIRECT filed→unfiled edge was founder-recorded (not a
    # discovery); only the indirect reach into the second unfiled source counts.
    assert {(l.root_source_id, l.reached_source_id, l.depth) for l in links} == {
        (filed.id, unfiled2.id, 2),
    }
    # With the direct-edge floor lowered, the recorded filed→unfiled edge counts too.
    links_direct = find_cross_domain_connections(db_session, user.id, [filed.id], min_depth=1)
    assert {(l.root_source_id, l.reached_source_id, l.depth) for l in links_direct} == {
        (filed.id, unfiled.id, 1),
        (filed.id, unfiled2.id, 2),
    }
    # ...but an unfiled root only discovers sources in a real project, never other unfiled ones.
    assert find_cross_domain_connections(db_session, user.id, [unfiled.id]) == []


def test_cross_domain_discovery_is_bidirectional(db_session, make_verified_user):
    """An edge recorded P2→P1 must let a P1 root discover the P2 source upstream too —
    connections through the graph are direction-agnostic for discovery purposes."""
    user, _ = make_verified_user()
    p1 = _make_project(db_session, user.id, "P1")
    p2 = _make_project(db_session, user.id, "P2")
    doc_a = _make_document(db_session, user.id, "A", project_id=p1.id)
    doc_b = _make_document(db_session, user.id, "B", project_id=p1.id)
    doc_c = _make_document(db_session, user.id, "C", project_id=p2.id)
    _relate(db_session, user.id, doc_a, doc_b)
    _relate(db_session, user.id, doc_c, doc_b)  # P2 records its source feeds P1's B
    _relate(db_session, user.id, doc_b, doc_a)  # closes the upstream chain: C→B→A

    links = find_cross_domain_connections(db_session, user.id, [doc_a.id])
    assert {(l.root_source_id, l.reached_source_id, l.direction) for l in links} == {
        (doc_a.id, doc_c.id, "inbound")
    }


# --------------------------------------------------------------------------------------
# 3. annotate_source_graph_nodes — the readiness-join half
# --------------------------------------------------------------------------------------


def test_annotation_returns_owner_visible_metadata_only(db_session, make_verified_user):
    user, _ = make_verified_user()
    other, _ = make_verified_user()
    p1 = _make_project(db_session, user.id, "P1")
    doc = _make_document(db_session, user.id, "annotated", project_id=p1.id)
    foreign = _make_document(db_session, other.id, "foreign")

    # The last RLS write above was as `other` — re-point the session at the querying owner
    # before reading, exactly like a real request would (per-owner RLS context).
    _set_rls_user(db_session, user.id)
    nodes = annotate_source_graph_nodes(db_session, user.id, [doc.id, foreign.id, uuid.uuid4()])
    assert len(nodes) == 1
    node = nodes[0]
    assert node.source_id == doc.id
    assert node.title == "annotated"
    assert node.project_id == p1.id
    assert node.active_truth_status is not None  # migration 0006 gives every document a status
    assert node.classification is not None


def test_annotation_of_nothing_is_nothing(db_session, make_verified_user):
    user, _ = make_verified_user()
    assert annotate_source_graph_nodes(db_session, user.id, []) == []
