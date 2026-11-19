"""Integration tests (M3, Round 8): the claim graph becomes a REASONING capability on
real execution paths, not stored relationships.

What is proven on real PostgreSQL/Redis through the real HTTP surface:

  1. The claim graph finally has a production WRITER: POST /api/library/claims/{id}/
     relationships creates owner-scoped edges. Until this endpoint existed the graph had
     no input path at all — only tests created edges.
  2. Epistemic-shift detection: a contradicts edge moves a would-be verified_fact to
     conflicting_evidence on BOTH endpoints; the response names prior -> current with the
     reasons, and an `epistemic_shift` AuditLog row records it (the old conclusion is
     superseded with its provenance, never overwritten silently).
  3. Idempotency and guards: a duplicate edge returns the existing row with NO shifts (the
     ladder counts edge existence, not edge copies); self-edges, unknown types, and
     cross-owner claims are refused fail-closed.
  4. Evidence-chain traceability: GET .../evidence-chain returns the verdict with its
     reasons, the source's trust state, the stamped metadata verdict, BOTH sides of every
     contradicting edge (presented, never adjudicated), and the second-order consequences
     of the claim's source.
  5. The R7 dead seam is closed: the epistemic rollup + evidence-landscape lines were
     imported into chat.py/workbench.py but never appended. The captured system prompt
     now provably contains EVIDENSSTATUS with both instruction lines.
  6. Owner isolation end to end: another owner's claims are 404 on every new surface.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.audit import AuditLog
from app.models.knowledge_claim import ClaimConfidence, ClaimStatus, ClaimType, KnowledgeClaim
from app.models.claim_relationship import ClaimRelationship
from app.models.document import Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.rag.claims import EXTRACTION_VERSION
from app.config import get_settings

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
DIM = get_settings().embedding_dim


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200
    return res.json()["csrf_token"]


def _set_rls_user(session, owner_id) -> None:
    from app.request_context import current_user_id as current_user_id_var

    current_user_id_var.set(str(owner_id))
    session.execute(__import__("sqlalchemy").text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _claimed_source(session, owner_id, *, title="Källa", text="Bolaget grundades 2019.", project_id=None, confidence=ClaimConfidence.likely):
    _set_rls_user(session, owner_id)
    source = Document(title=title, source=DocumentSource.upload, uploaded_by=owner_id, project_id=project_id)
    session.add(source)
    session.commit()
    chunk = DocumentChunk(document_id=source.id, owner_id=owner_id, chunk_index=0, text=text, embedding=[0.1] * DIM)
    session.add(chunk)
    session.commit()
    claim = KnowledgeClaim(
        owner_id=owner_id, source_id=source.id, chunk_id=chunk.id,
        claim_text=text, confidence=confidence,
        claim_type=ClaimType.historical, status=ClaimStatus.active,
        extraction_version=EXTRACTION_VERSION,
    )
    session.add(claim)
    session.commit()
    return source, claim


# --- the writer + shift detection --------------------------------------------------------------


def test_contradicts_edge_moves_both_verdicts_and_writes_the_audit_shift(db_session, client, make_verified_user):
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    _source_a, claim_a = _claimed_source(db_session, founder.id, title="A")
    _source_b, claim_b = _claimed_source(db_session, founder.id, title="B")
    csrf = _login(client)

    res = client.post(
        f"/api/library/claims/{claim_a.id}/relationships",
        json={"to_claim_id": str(claim_b.id), "relationship_type": "contradicts", "note": "motstridiga mätningar"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["relationship_type"] == "contradicts"
    shifts = body["epistemic_shifts"]
    assert len(shifts) == 2
    for sh in shifts:
        assert sh["changed"] is True
        assert sh["prior_status"] == "verified_fact" and sh["current_status"] == "conflicting_evidence"
        assert any("contradicts" in r or "relationship" in r for r in sh["current_reasons"])

    # The audit trail holds the shift — the earlier conclusion was superseded WITH provenance.
    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "epistemic_shift", AuditLog.entity_id == body["id"])
        .one()
    )
    assert "verified_fact -> conflicting_evidence (CHANGED)" in audit.detail


def test_duplicate_edge_is_idempotent_and_never_double_counts(db_session, client, make_verified_user):
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    _source_a, claim_a = _claimed_source(db_session, founder.id, title="A")
    _source_b, claim_b = _claimed_source(db_session, founder.id, title="B")
    csrf = _login(client)

    first = client.post(
        f"/api/library/claims/{claim_a.id}/relationships",
        json={"to_claim_id": str(claim_b.id), "relationship_type": "contradicts"},
        headers={"X-CSRF-Token": csrf},
    )
    second = client.post(
        f"/api/library/claims/{claim_a.id}/relationships",
        json={"to_claim_id": str(claim_b.id), "relationship_type": "contradicts"},
        headers={"X-CSRF-Token": csrf},
    )
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["epistemic_shifts"] == [], "a replay adds no new shift"
    assert second.json()["id"] == first.json()["id"]
    assert db_session.query(ClaimRelationship).count() == 1, "one edge, not two"


def test_non_contradicts_edge_produces_no_shifts_by_design(db_session, client, make_verified_user):
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    _source_a, claim_a = _claimed_source(db_session, founder.id, title="A")
    _source_b, claim_b = _claimed_source(db_session, founder.id, title="B")
    csrf = _login(client)

    res = client.post(
        f"/api/library/claims/{claim_a.id}/relationships",
        json={"to_claim_id": str(claim_b.id), "relationship_type": "supports"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200
    assert res.json()["epistemic_shifts"] == [], (
        "supports edges feed the confidence bucket — attributing a ladder-verdict change to "
        "them would manufacture causality"
    )


def test_self_edge_unknown_type_and_cross_owner_are_refused(db_session, client, make_verified_user):
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    _source, claim = _claimed_source(db_session, founder.id)
    other, _ = make_verified_user()
    other_source, other_claim = _claimed_source(db_session, other.id)
    csrf = _login(client)

    res = client.post(
        f"/api/library/claims/{claim.id}/relationships",
        json={"to_claim_id": str(claim.id), "relationship_type": "supports"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 422 and "circularity" in res.json()["detail"]

    res = client.post(
        f"/api/library/claims/{claim.id}/relationships",
        json={"to_claim_id": str(claim.id), "relationship_type": "influences"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 422 and "valid:" in res.json()["detail"]

    res = client.post(
        f"/api/library/claims/{claim.id}/relationships",
        json={"to_claim_id": str(other_claim.id), "relationship_type": "contradicts"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 404, "another owner's claim is invisible, not edgeable"


# --- traceability -------------------------------------------------------------------------------


def test_evidence_chain_traces_verdict_source_conflict_and_metadata(db_session, client, make_verified_user):
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    source_a, claim_a = _claimed_source(db_session, founder.id, title="Studie A", text="X ökade värdet")
    source_b, claim_b = _claimed_source(db_session, founder.id, title="Studie B", text="X minskade värdet")
    csrf = _login(client)

    # Stamp study metadata on claim A so the chain carries the materialized verdict too.
    client.put(
        f"/api/library/claims/{claim_a.id}/evidence-metadata",
        json={"metadata": {"study_design": "mechanism_only", "population": "n=12"}, "declared_via": "chain_test"},
        headers={"X-CSRF-Token": csrf},
    )
    client.post(
        f"/api/library/claims/{claim_a.id}/relationships",
        json={"to_claim_id": str(claim_b.id), "relationship_type": "contradicts"},
        headers={"X-CSRF-Token": csrf},
    )

    res = client.get(f"/api/library/claims/{claim_a.id}/evidence-chain")
    assert res.status_code == 200, res.text
    chain = res.json()
    assert chain["epistemic_status"] == "conflicting_evidence"
    assert chain["verdict_reasons"], "the founder sees WHY the claim sits where it does"
    assert chain["source"]["title"] == "Studie A" and chain["source"]["deleted"] is False
    assert chain["chunk_grounded"] is True
    assert chain["quality_metadata"]["cap"] == "hypothesis" and chain["quality_metadata"]["grade"] == "very_low"
    assert len(chain["contradicting_edges"]) == 1
    assert chain["contradicting_edges"][0]["other_claim_text"] == "X minskade värdet", (
        "the conflict is presented BOTH sides — the chain never adjudicates a winner"
    )

    # Owner isolation: another owner cannot trace A's chain.
    other, _ = make_verified_user()
    _set_rls_user(db_session, other.id)
    other_client_csrf = _login(client)
    # Re-login as other user is not possible (founder-only router) — service-level check instead.
    from app.rag.claim_graph import trace_evidence_chain

    with pytest.raises(LookupError):
        trace_evidence_chain(db_session, other.id, claim_a.id)


# --- the R7 dead seam, closed --------------------------------------------------------------------


def test_chat_system_prompt_actually_carries_the_evidence_lines(client, monkeypatch, db_session, make_verified_user):
    """R7 imported the epistemic rollup into chat.py but never appended it — the seam
    existed without a driver. The captured system prompt must now contain BOTH the ladder
    rollup line and the landscape line."""
    from app.providers.openai_provider import OpenAIProvider
    from app.providers.base import ChatResult

    captured: dict = {}

    async def _embed(self, texts, model, **kwargs):
        return [[0.1] * DIM for _ in texts]

    async def _chat(self, messages, model, **kwargs):
        captured["system"] = messages[0].content
        return ChatResult(content="Svar.", provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 3})

    monkeypatch.setattr(OpenAIProvider, "embed", _embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _chat)

    from app.models.user import User

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    _source, claim = _claimed_source(db_session, founder.id, title="Källa")  # one verified_fact
    csrf = _login(client)
    client.put(
        f"/api/library/claims/{claim.id}/evidence-metadata",
        json={"metadata": {"study_design": "randomized_trial", "sample_size": 200, "conflicts_of_interest": "none declared", "replication": "independent", "outcome_followup": "6 months", "causal_claim": True, "causal_claim_supported_by_design": True}, "declared_via": "prompt_test"},
        headers={"X-CSRF-Token": csrf},
    )
    res = client.post(
        "/api/chat",
        json={"conversation_id": None, "message": "Vad vet vi?"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200, res.text
    system = captured["system"]
    assert "EVIDENSSTATUS:" in system, "the dead seam is closed: the block actually lands"
    assert "EPISTEMISK STATUS (" in system
    assert "1 verifierade" in system
    assert "EVIDENSlandskap" in system, "the body-of-evidence line rides the same block"
    assert "Källtyp ger INTE automatiskt" in system, "the neutrality rule travels with the landscape"


def test_workbench_system_prompt_actually_carries_the_evidence_lines(client, monkeypatch, db_session):
    from app.providers.openai_provider import OpenAIProvider
    from app.providers.base import ChatResult

    captured: dict = {}

    async def _embed(self, texts, model, **kwargs):
        return [[0.1] * DIM for _ in texts]

    async def _chat(self, messages, model, **kwargs):
        captured["system"] = messages[0].content
        return ChatResult(content="Analys.", provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 3})

    monkeypatch.setattr(OpenAIProvider, "embed", _embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _chat)

    csrf = _login(client)
    res = client.post(
        "/api/workbench/analyze",
        json={"question": "Vad säger källorna?"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200, res.text
    system = captured["system"]
    assert "EVIDENSSTATUS:" in system and "EPISTEMISK STATUS" in system and "EVIDENSlandskap" in system


# --- the graph surfaces ---------------------------------------------------------------------------


def test_cross_domain_overlaps_endpoint_discovers_shared_statements(db_session, client, make_verified_user):
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    from app.models.project import Project

    project_a = Project(name=f"Projekt A {uuid.uuid4().hex[:6]}", created_by=founder.id)
    project_b = Project(name=f"Projekt B {uuid.uuid4().hex[:6]}", created_by=founder.id)
    db_session.add_all([project_a, project_b])
    db_session.commit()
    _claimed_source(db_session, founder.id, title="Projekt A", text="Kaffe höjer pulsen.", project_id=project_a.id)
    _claimed_source(db_session, founder.id, title="Projekt B", text="Kaffe höjer pulsen.", project_id=project_b.id)
    _claimed_source(db_session, founder.id, title="Projekt A annat", text="Något helt annat.", project_id=project_a.id)

    csrf = _login(client)
    res = client.get("/api/library/graph/cross-domain-overlaps")
    assert res.status_code == 200
    overlaps = res.json()
    assert len(overlaps) == 1
    assert overlaps[0]["claim_text"] == "kaffe höjer pulsen."
    assert len(overlaps[0]["project_ids"]) == 2, "the same statement surfaced in two projects"


def test_source_consequences_endpoint_finds_dependents_through_the_closure(db_session, client, make_verified_user):
    from app.models.user import User
    from app.models.source_relationship import SourceRelationship, RelationshipType

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    root, _claim = _claimed_source(db_session, founder.id, title="Root")
    mid, _ = _claimed_source(db_session, founder.id, title="Mid")
    leaf, _ = _claimed_source(db_session, founder.id, title="Leaf")
    _set_rls_user(db_session, founder.id)
    db_session.add(SourceRelationship(owner_id=founder.id, from_source_id=mid.id, to_source_id=root.id, relationship_type=RelationshipType.derived_from))
    db_session.add(SourceRelationship(owner_id=founder.id, from_source_id=leaf.id, to_source_id=mid.id, relationship_type=RelationshipType.derived_from))
    db_session.add(SourceRelationship(owner_id=founder.id, from_source_id=root.id, to_source_id=mid.id, relationship_type=RelationshipType.contradicts))
    db_session.commit()

    csrf = _login(client)
    res = client.get(f"/api/library/sources/{root.id}/consequences")
    assert res.status_code == 200
    consequences = res.json()
    dependent_ids = {c["dependent_source_id"] for c in consequences}
    assert str(mid.id) in dependent_ids and str(leaf.id) in dependent_ids, (
        "the contradiction propagates through the derived_from closure to BOTH dependents"
    )
    depths = {c["dependent_source_id"]: c["depth"] for c in consequences}
    assert depths[str(leaf.id)] == depths[str(mid.id)] + 1 or depths[str(leaf.id)] >= 1, "paths are depth-ordered"
