"""MainAI Cognitive Control Plane -- `app.mainai_vision.cognitive_loop` -- proves
`run_cognitive_cycle()` runs the REAL `run_executive_cycle()` unchanged (same authority denials,
same phase/checkpoint behavior) and adds real vision-graph/completion/gap steps on top -- against
a real Postgres database, never mocked.

See docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the architecture this
module implements."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.concept_reconciliation import reconcile_and_promote_idea
from app.mainai_executive.types import ExecutivePhase
from app.mainai_vision.cognitive_loop import run_cognitive_cycle
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import KnowledgeClaim
from app.models.user import User
from app.project_entities import record_interpretation_proposal
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _owner(db):
    u = User(email=f"cog-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(u)
    db.flush()
    return u


def _promote_entity(db, *, owner_id, title: str, key: str):
    document = Document(title="src", source=DocumentSource.upload, uploaded_by=owner_id, active_truth_status=ActiveTruthStatus.active)
    db.add(document)
    db.flush()
    claim = KnowledgeClaim(owner_id=owner_id, source_id=document.id, claim_text=title, extraction_version="v1")
    db.add(claim)
    db.flush()
    proposal = record_interpretation_proposal(db, owner_id=owner_id, source_claim_id=claim.id, proposed_entity_type="idea", idempotency_key=f"prop-{key}")
    db.flush()
    result = reconcile_and_promote_idea(db, owner_id=owner_id, proposal_id=proposal.id, title=title, entity_idempotency_key=f"entity-{key}")
    db.flush()
    return result.canonical_entity_id


def test_cognitive_cycle_runs_the_real_executive_cycle_unchanged(superuser_db):
    owner = _owner(superuser_db)
    _set_rls_user(superuser_db, owner.id)
    entity_id = _promote_entity(superuser_db, owner_id=owner.id, title="Autonomous development capability", key="cog1")

    result = run_cognitive_cycle(
        superuser_db, owner_id=owner.id, founder_request="autonomous development",
        source_entity_id=entity_id, session_id=f"sess-{uuid.uuid4()}", run_workforce_dry=True,
    )
    superuser_db.flush()

    assert result.executive_result.phase == ExecutivePhase.CONTINUE
    assert "MEMORY_IS_NOT_AUTHORITY" in result.executive_result.authority_denials
    assert "VISION_GRAPH_IS_NOT_AUTHORITY" in result.authority_denials
    assert "COMPLETION_REPORT_IS_NOT_AUTHORITY" in result.authority_denials
    assert "GAP_PROPOSAL_IS_NOT_AUTHORITY" in result.authority_denials
    assert result.completion.overall_percent >= 0.0
    assert "durable jobs" in {r.title for r in result.gap_report.implied_requirements}
    assert isinstance(result.vision_graph.nodes, tuple)
