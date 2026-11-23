"""Tests for app/rag/epistemics.py — the P2 evidence-aware ladder (verified fact /
conflicting evidence / hypothesis / assumption / unknown), second-order consequence
discovery, and cross-domain claim overlaps.

Same discipline as test_graph_traverse.py / test_trust_engine.py: everything runs against
the real local Postgres the session fixture provides (migrations + RLS), every insert
re-sets app.current_user_id first, and no test touches a model leg, a migration or a
second engine — the module's own scope contract, asserted here by construction.
"""

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.models.claim_relationship import ClaimRelationship, ClaimRelationshipType
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_claim import ClaimConfidence, ClaimStatus, ClaimType, KnowledgeClaim
from app.models.project import Project
from app.models.source_relationship import RelationshipType, SourceRelationship
from app.rag.claims import EXTRACTION_VERSION
from app.rag.epistemics import (
    EpistemicStatus,
    classify_claim,
    cross_domain_claim_overlaps,
    epistemic_counts,
    epistemic_ledger,
    second_order_consequences,
)
from app.config import get_settings

EMBEDDING_DIM = get_settings().embedding_dim


def _set_rls_user(session, owner_id) -> None:
    from app.request_context import current_user_id as current_user_id_var

    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_source(session, owner_id, title="Källa", *, status=ActiveTruthStatus.active, project_id=None):
    _set_rls_user(session, owner_id)
    document = Document(
        title=title, source=DocumentSource.upload, uploaded_by=owner_id, active_truth_status=status, project_id=project_id
    )
    session.add(document)
    session.commit()
    return document


def _make_chunk(session, owner_id, document_id, text_value="Bolaget grundades 2019 i Stockholm."):
    _set_rls_user(session, owner_id)
    chunk = DocumentChunk(document_id=document_id, owner_id=owner_id, chunk_index=0, text=text_value, embedding=[0.1] * EMBEDDING_DIM)
    session.add(chunk)
    session.commit()
    return chunk


def _make_claim(session, owner_id, source, chunk=None, *, claim_text="Bolaget grundades 2019.", confidence=ClaimConfidence.likely, status=ClaimStatus.active):
    _set_rls_user(session, owner_id)
    claim = KnowledgeClaim(
        owner_id=owner_id,
        source_id=source.id,
        chunk_id=chunk.id if chunk is not None else None,
        claim_text=claim_text,
        confidence=confidence,
        claim_type=ClaimType.historical,
        status=status,
        extraction_version=EXTRACTION_VERSION,
    )
    session.add(claim)
    session.commit()
    return claim


def _relate_claims(session, owner_id, from_claim, to_claim, rel_type):
    _set_rls_user(session, owner_id)
    edge = ClaimRelationship(owner_id=owner_id, from_claim_id=from_claim.id, to_claim_id=to_claim.id, relationship_type=rel_type)
    session.add(edge)
    session.commit()
    return edge


def _relate_sources(session, owner_id, from_doc, to_doc, rel_type=RelationshipType.derived_from):
    _set_rls_user(session, owner_id)
    edge = SourceRelationship(owner_id=owner_id, from_source_id=from_doc.id, to_source_id=to_doc.id, relationship_type=rel_type)
    session.add(edge)
    session.commit()
    return edge


def _make_project(session, owner_id, name):
    _set_rls_user(session, owner_id)
    project = Project(name=name, created_by=owner_id)
    session.add(project)
    session.commit()
    return project


# --------------------------------------------------------------------------------------
# 1. The evidence ladder — classify_claim
# --------------------------------------------------------------------------------------


def test_grounded_active_claim_is_a_verified_fact(db_session, make_verified_user):
    user, _ = make_verified_user()
    source = _make_source(db_session, user.id)
    chunk = _make_chunk(db_session, user.id, source.id)
    claim = _make_claim(db_session, user.id, source, chunk, confidence=ClaimConfidence.likely)

    verdict = classify_claim(db_session, user.id, claim)
    assert verdict.status == EpistemicStatus.verified_fact
    assert verdict.has_live_grounding is True
    assert verdict.source_id == source.id
    assert "grounded in its active source" in " ".join(verdict.reasons)


def test_contradicts_edge_caps_the_verdict_at_conflicting_evidence(db_session, make_verified_user):
    """The assess_claim_confidence rule — a contradicts edge is never outvoted by
    corroboration — applies to the ladder too, even with independent corroboration."""
    user, _ = make_verified_user()
    source = _make_source(db_session, user.id)
    chunk = _make_chunk(db_session, user.id, source.id)
    claim = _make_claim(db_session, user.id, source, chunk, confidence=ClaimConfidence.likely)
    other_source = _make_source(db_session, user.id, "Annat")
    other_chunk = _make_chunk(db_session, user.id, other_source.id)
    challenger = _make_claim(db_session, user.id, other_source, other_chunk, confidence=ClaimConfidence.likely)
    _relate_claims(db_session, user.id, challenger, claim, ClaimRelationshipType.contradicts)

    verdict = classify_claim(db_session, user.id, claim)
    assert verdict.status == EpistemicStatus.conflicting_evidence


def test_claim_with_no_live_grounding_is_unknown(db_session, make_verified_user):
    user, _ = make_verified_user()
    source = _make_source(db_session, user.id)
    claim = _make_claim(db_session, user.id, source, chunk=None, confidence=ClaimConfidence.likely)

    verdict = classify_claim(db_session, user.id, claim)
    assert verdict.status == EpistemicStatus.unknown
    assert verdict.has_live_grounding is False


def test_superseded_source_downgrades_to_hypothesis_not_conflict(db_session, make_verified_user):
    """A superseded SOURCE is not a contradiction — the ladder must not invent conflict."""
    user, _ = make_verified_user()
    source = _make_source(db_session, user.id, status=ActiveTruthStatus.superseded)
    chunk = _make_chunk(db_session, user.id, source.id)
    claim = _make_claim(db_session, user.id, source, chunk, confidence=ClaimConfidence.likely)

    verdict = classify_claim(db_session, user.id, claim)
    assert verdict.status == EpistemicStatus.hypothesis
    assert "superseded" in " ".join(verdict.reasons)
    assert verdict.source_active_truth_status == "superseded"


def test_ungrounded_claim_is_an_assumption_never_a_fact(db_session, make_verified_user):
    """no_basis = stated without grounding — exactly the mission's 'assumption' bucket.
    Corroboration must never rescue it (assess_claim_confidence already refuses)."""
    user, _ = make_verified_user()
    source = _make_source(db_session, user.id)
    chunk = _make_chunk(db_session, user.id, source.id)
    claim = _make_claim(db_session, user.id, source, chunk, confidence=ClaimConfidence.no_basis)
    corroborating_source = _make_source(db_session, user.id, "Stöd")
    corroborating_chunk = _make_chunk(db_session, user.id, corroborating_source.id)
    supporter = _make_claim(db_session, user.id, corroborating_source, corroborating_chunk)
    _relate_claims(db_session, user.id, supporter, claim, ClaimRelationshipType.supports)

    verdict = classify_claim(db_session, user.id, claim)
    assert verdict.status == EpistemicStatus.assumption


def test_independent_corroboration_reports_verified_with_the_reason(db_session, make_verified_user):
    user, _ = make_verified_user()
    source = _make_source(db_session, user.id)
    chunk = _make_chunk(db_session, user.id, source.id)
    claim = _make_claim(db_session, user.id, source, chunk, confidence=ClaimConfidence.likely)
    other_source = _make_source(db_session, user.id, "Oberoende")
    other_chunk = _make_chunk(db_session, user.id, other_source.id)
    corroborator = _make_claim(db_session, user.id, other_source, other_chunk)
    _relate_claims(db_session, user.id, corroborator, claim, ClaimRelationshipType.supports)

    verdict = classify_claim(db_session, user.id, claim)
    assert verdict.status == EpistemicStatus.verified_fact
    assert verdict.confidence == ClaimConfidence.certain
    assert "independently corroborated" in " ".join(verdict.reasons)


def test_ledger_scopes_to_owner_and_project(db_session, make_verified_user):
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()
    project = _make_project(db_session, owner_a.id, "P1")

    a_source = _make_source(db_session, owner_a.id, project_id=project.id)
    a_chunk = _make_chunk(db_session, owner_a.id, a_source.id)
    _make_claim(db_session, owner_a.id, a_source, a_chunk)

    b_source = _make_source(db_session, owner_b.id)
    b_chunk = _make_chunk(db_session, owner_b.id, b_source.id)
    _make_claim(db_session, owner_b.id, b_source, b_chunk)

    # RLS discipline: the session's app.current_user_id still points at the LAST insert
    # (owner_b) — re-set it per read, exactly like every helper does per insert.
    _set_rls_user(db_session, owner_a.id)
    assert len(epistemic_ledger(db_session, owner_a.id)) == 1
    assert len(epistemic_ledger(db_session, owner_a.id, project_id=project.id)) == 1
    _set_rls_user(db_session, owner_b.id)
    assert len(epistemic_ledger(db_session, owner_b.id)) == 1
    # The other owner's claim id never leaks into owner A's ledger.
    other_ids = {v.claim_id for v in epistemic_ledger(db_session, owner_a.id)}
    b_ids = {v.claim_id for v in epistemic_ledger(db_session, owner_b.id)}
    assert other_ids.isdisjoint(b_ids)


def test_epistemic_counts_summarize_the_ladder(db_session, make_verified_user):
    user, _ = make_verified_user()
    source = _make_source(db_session, user.id)
    chunk = _make_chunk(db_session, user.id, source.id)
    _make_claim(db_session, user.id, source, chunk, confidence=ClaimConfidence.likely)
    _make_claim(db_session, user.id, source, chunk, claim_text="Annat påstående.", confidence=ClaimConfidence.no_basis)

    counts = epistemic_counts(epistemic_ledger(db_session, user.id))
    assert counts["verified_fact"] == 1
    assert counts["assumption"] == 1
    assert sum(counts.values()) == 2


# --------------------------------------------------------------------------------------
# 2. Second-order consequences — structural propagation over the recorded graph
# --------------------------------------------------------------------------------------


def test_supersession_propagates_to_transitive_dependents(db_session, make_verified_user):
    """X supersedes Y; Z derived_from Y: Z is downstream of a superseded source."""
    user, _ = make_verified_user()
    doc_x = _make_source(db_session, user.id, "Ny")
    doc_y = _make_source(db_session, user.id, "Gammal")
    doc_z = _make_source(db_session, user.id, "Härledd")
    _relate_sources(db_session, user.id, doc_x, doc_y, RelationshipType.supersedes)
    _relate_sources(db_session, user.id, doc_z, doc_y, RelationshipType.derived_from)

    consequences = second_order_consequences(db_session, user.id, [doc_x.id])
    kinds = {(c.kind, c.dependent_source_id) for c in consequences}
    assert ("stale_basis", doc_z.id) in kinds
    stale = next(c for c in consequences if c.kind == "stale_basis" and c.dependent_source_id == doc_z.id)
    # The walk's source_path is the dependency chain from the trigger head (the superseded
    # source) down to the dependent: [doc_y, doc_z] — the audit trail for WHY.
    assert stale.path == [doc_y.id, doc_z.id]
    assert stale.trigger_edge[2] == "supersedes"


def test_contradiction_flags_dependents_on_both_sides(db_session, make_verified_user):
    user, _ = make_verified_user()
    doc_a = _make_source(db_session, user.id, "A")
    doc_b = _make_source(db_session, user.id, "B")
    doc_z = _make_source(db_session, user.id, "Härledd från B")
    _relate_sources(db_session, user.id, doc_a, doc_b, RelationshipType.contradicts)
    _relate_sources(db_session, user.id, doc_z, doc_b, RelationshipType.derived_from)

    consequences = second_order_consequences(db_session, user.id, [doc_a.id])
    contested = [c for c in consequences if c.kind == "contested_ground" and c.dependent_source_id == doc_z.id]
    assert len(contested) == 1


def test_cross_domain_opposing_support_is_discovered(db_session, make_verified_user):
    """C (project 1) supports X, D (project 2) supports Y, X contradicts Y — the two
    domains' evidence is committed to opposite sides of a recorded conflict."""
    user, _ = make_verified_user()
    p1 = _make_project(db_session, user.id, "Bio")
    p2 = _make_project(db_session, user.id, "Ekonomi")

    doc_x = _make_source(db_session, user.id, "X", project_id=p1.id)
    doc_y = _make_source(db_session, user.id, "Y", project_id=p2.id)
    doc_c = _make_source(db_session, user.id, "Stöd X", project_id=p1.id)
    doc_d = _make_source(db_session, user.id, "Stöd Y", project_id=p2.id)
    _relate_sources(db_session, user.id, doc_x, doc_y, RelationshipType.contradicts)
    _relate_sources(db_session, user.id, doc_c, doc_x, RelationshipType.supports)
    _relate_sources(db_session, user.id, doc_d, doc_y, RelationshipType.supports)

    consequences = second_order_consequences(db_session, user.id, [doc_x.id, doc_y.id])
    opposing = [c for c in consequences if c.kind == "opposing_support"]
    assert len(opposing) == 1
    c = opposing[0]
    assert {c.dependent_project_id, _other(c, p1, p2)} == {p1.id, p2.id}


def _other(consequence, p1, p2):
    return p2.id if consequence.dependent_project_id == p1.id else p1.id


def test_owner_isolation_in_consequences(db_session, make_verified_user):
    """Another owner's identical graph produces nothing in this owner's discovery."""
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()
    x = _make_source(db_session, user_a.id, "X")
    y = _make_source(db_session, user_a.id, "Y")
    z = _make_source(db_session, user_a.id, "Z")
    _relate_sources(db_session, user_a.id, x, y, RelationshipType.supersedes)
    _relate_sources(db_session, user_a.id, z, y, RelationshipType.derived_from)

    assert second_order_consequences(db_session, user_b.id, [x.id]) == []


def test_no_triggers_means_no_consequences(db_session, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_source(db_session, user.id, "Ensamt")
    assert second_order_consequences(db_session, user.id, [doc.id]) == []
    assert second_order_consequences(db_session, user.id, []) == []


# --------------------------------------------------------------------------------------
# 3. Cross-domain claim overlaps — identical text across project boundaries
# --------------------------------------------------------------------------------------


def test_same_claim_text_across_projects_is_discovered(db_session, make_verified_user):
    user, _ = make_verified_user()
    p1 = _make_project(db_session, user.id, "En")
    p2 = _make_project(db_session, user.id, "Två")
    s1 = _make_source(db_session, user.id, "Ett", project_id=p1.id)
    s2 = _make_source(db_session, user.id, "Två", project_id=p2.id)
    text_value = "Kostnaden ökar med 12 procent per år."
    c1 = _make_claim(db_session, user.id, s1, _make_chunk(db_session, user.id, s1.id), claim_text=text_value)
    c2 = _make_claim(db_session, user.id, s2, _make_chunk(db_session, user.id, s2.id), claim_text=f"  {text_value.upper()}  ")

    overlaps = cross_domain_claim_overlaps(db_session, user.id)
    assert len(overlaps) == 1
    overlap = overlaps[0]
    assert set(overlap.claim_ids) == {c1.id, c2.id}
    assert set(p for p in overlap.project_ids if p is not None) == {p1.id, p2.id}


def test_same_claim_within_one_project_is_not_cross_domain(db_session, make_verified_user):
    user, _ = make_verified_user()
    s1 = _make_source(db_session, user.id, "Ett")
    s2 = _make_source(db_session, user.id, "Två")
    text_value = "Samma text inom ett projekt."
    _make_claim(db_session, user.id, s1, _make_chunk(db_session, user.id, s1.id), claim_text=text_value)
    _make_claim(db_session, user.id, s2, _make_chunk(db_session, user.id, s2.id), claim_text=text_value)

    assert cross_domain_claim_overlaps(db_session, user.id) == []
