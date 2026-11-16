"""MainAI Cognitive Control Plane -- `app.mainai_vision.vision_compiler` -- proves
`compile_vision_graph()` reads real, current `project_entities` rows (migration 0072's widened
vocabulary) into a typed `VisionGraph`, excludes superseded/disputed/historical rows exactly
like `project_entities.service.list_current_project_entities()` already does, and is read-only
-- against a real Postgres database, never mocked.

See docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the architecture this
module implements."""

from __future__ import annotations

import uuid

from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import KnowledgeClaim
from app.models.project_entities import ProjectEntity, ProjectEntityRelationship
from app.models.user import User
from app.mainai_vision.types import VisionEdgeKind, VisionNodeKind
from app.mainai_vision.vision_compiler import compile_vision_graph


def _owner_with_claim(db, claim_text="Vision compiler test claim."):
    user = User(email=f"vc-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    document = Document(title="Source", source=DocumentSource.upload, uploaded_by=user.id, active_truth_status=ActiveTruthStatus.active)
    db.add(document)
    db.flush()
    claim = KnowledgeClaim(owner_id=user.id, source_id=document.id, claim_text=claim_text, extraction_version="v1")
    db.add(claim)
    db.flush()
    return user, claim


def _entity(db, *, owner_id, claim_id, entity_type, title, status="active", **overrides):
    entity = ProjectEntity(
        owner_id=owner_id, entity_type=entity_type, title=title, title_normalized=title.lower(),
        derived_from_claim_id=claim_id, idempotency_key=f"pe-{uuid.uuid4()}", status=status,
        authority=overrides.pop("authority", "founder"), basis=overrides.pop("basis", "manual"),
        confidence=overrides.pop("confidence", None),
        **overrides,
    )
    db.add(entity)
    db.flush()
    return entity


def test_compiles_current_vision_nodes_and_excludes_non_current(superuser_db):
    owner, claim = _owner_with_claim(superuser_db)
    superuser_db.commit()
    current = _entity(superuser_db, owner_id=owner.id, claim_id=claim.id, entity_type="capability", title="Autonomous development")
    superseded = _entity(superuser_db, owner_id=owner.id, claim_id=claim.id, entity_type="requirement", title="Old requirement", status="superseded")
    disputed = _entity(superuser_db, owner_id=owner.id, claim_id=claim.id, entity_type="requirement", title="Disputed requirement", status="disputed")
    # A non-vision entity_type (idea/decision/etc) must never appear as a vision node.
    idea = _entity(superuser_db, owner_id=owner.id, claim_id=claim.id, entity_type="idea", title="Just an idea")
    superuser_db.commit()

    graph = compile_vision_graph(superuser_db, owner_id=owner.id)

    node_ids = {n.entity_id for n in graph.nodes}
    assert current.id in node_ids
    assert superseded.id not in node_ids
    assert disputed.id not in node_ids
    assert idea.id not in node_ids
    assert graph.excluded_count == 2  # superseded + disputed, never silently hidden
    node = graph.node(current.id)
    assert node.kind == VisionNodeKind.CAPABILITY
    assert node.currentness == "current"


def test_compiles_relationships_between_current_vision_nodes_only(superuser_db):
    owner, claim = _owner_with_claim(superuser_db)
    superuser_db.commit()
    parent = _entity(superuser_db, owner_id=owner.id, claim_id=claim.id, entity_type="capability", title="Parent capability")
    child = _entity(superuser_db, owner_id=owner.id, claim_id=claim.id, entity_type="requirement", title="Child requirement")
    excluded = _entity(superuser_db, owner_id=owner.id, claim_id=claim.id, entity_type="requirement", title="Excluded", status="superseded")
    superuser_db.add(ProjectEntityRelationship(owner_id=owner.id, from_entity_id=child.id, to_entity_id=parent.id, relationship_type="depends_on"))
    # A relationship touching an excluded (superseded) node must never appear in the compiled graph.
    superuser_db.add(ProjectEntityRelationship(owner_id=owner.id, from_entity_id=excluded.id, to_entity_id=parent.id, relationship_type="depends_on"))
    superuser_db.commit()

    graph = compile_vision_graph(superuser_db, owner_id=owner.id)
    assert len(graph.edges) == 1
    edge = graph.edges[0]
    assert edge.from_entity_id == child.id and edge.to_entity_id == parent.id
    assert edge.kind == VisionEdgeKind.DEPENDS_ON
    assert graph.edges_from(child.id) == (edge,)
    assert graph.edges_to(parent.id) == (edge,)


def test_owner_isolation_never_leaks_another_owners_vision(superuser_db):
    owner_a, claim_a = _owner_with_claim(superuser_db, "owner a claim")
    owner_b, claim_b = _owner_with_claim(superuser_db, "owner b claim")
    superuser_db.commit()
    _entity(superuser_db, owner_id=owner_a.id, claim_id=claim_a.id, entity_type="capability", title="Owner A capability")
    _entity(superuser_db, owner_id=owner_b.id, claim_id=claim_b.id, entity_type="capability", title="Owner B capability")
    superuser_db.commit()

    graph_a = compile_vision_graph(superuser_db, owner_id=owner_a.id)
    assert len(graph_a.nodes) == 1
    assert graph_a.nodes[0].title == "Owner A capability"


def test_empty_history_returns_empty_graph_not_an_error(superuser_db):
    owner, _claim = _owner_with_claim(superuser_db)
    superuser_db.commit()
    graph = compile_vision_graph(superuser_db, owner_id=owner.id)
    assert graph.nodes == ()
    assert graph.edges == ()
    assert graph.excluded_count == 0


def test_compiler_never_writes_anything():
    """Structural: compile_vision_graph() must never db.add/db.commit/INSERT/UPDATE/DELETE."""
    import inspect
    import re

    import app.mainai_vision.vision_compiler as module

    source = inspect.getsource(module)
    assert not re.search(r"\bdb\.add\(", source)
    assert not re.search(r"\bdb\.commit\(", source)
    assert not re.search(r"\bINSERT INTO\b", source)
    assert not re.search(r"\bUPDATE \w", source)
    assert not re.search(r"\bDELETE FROM\b", source)
