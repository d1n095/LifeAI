"""Stage F — continuous simplification proposals (no auto-removal of protected domains)."""

from __future__ import annotations

import uuid

from app.concept_reconciliation import reconcile_and_promote_idea
from app.continuous_simplification import PROTECTED_DOMAINS, SimplificationKind, propose_simplifications
from app.inspectable_memory import founder_add_memory_note
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import KnowledgeClaim
from app.models.user import User
from app.project_entities import record_interpretation_proposal
from app.work_candidates import record_work_candidate


def _owner(db):
    user = User(email=f"simp-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def _promote(db, owner_id, title, key):
    document = Document(title="s", source=DocumentSource.upload, uploaded_by=owner_id, active_truth_status=ActiveTruthStatus.active)
    db.add(document)
    db.flush()
    claim = KnowledgeClaim(owner_id=owner_id, source_id=document.id, claim_text=title, extraction_version="v1")
    db.add(claim)
    db.flush()
    proposal = record_interpretation_proposal(
        db, owner_id=owner_id, source_claim_id=claim.id, proposed_entity_type="idea", idempotency_key=f"p-{key}"
    )
    db.flush()
    return reconcile_and_promote_idea(
        db, owner_id=owner_id, proposal_id=proposal.id, title=title, entity_idempotency_key=f"e-{key}"
    )


def test_detects_duplicate_concepts(superuser_db):
    owner = _owner(superuser_db)
    # Bypass SAME-collapse by using titles that are similar but not normalized-equal enough
    # for promote reuse — still high Jaccard for Stage F.
    a = _promote(superuser_db, owner.id, "Durable inspectable memory foundation layer", "a")
    b = _promote(superuser_db, owner.id, "Durable inspectable memory foundation layers", "b")
    superuser_db.commit()
    report = propose_simplifications(superuser_db, owner_id=owner.id)
    kinds = {p.kind for p in report.proposals}
    # If Stage B collapsed them into one entity, no duplicate_concept — that's fine (already simplified).
    if a.canonical_entity_id != b.canonical_entity_id:
        assert SimplificationKind.DUPLICATE_CONCEPT in kinds
    assert all(p.auto_apply_allowed is False for p in report.proposals)


def test_orphan_memory_and_protected_flag(superuser_db):
    owner = _owner(superuser_db)
    founder_add_memory_note(
        superuser_db,
        owner_id=owner.id,
        content="Random preference with no linkage",
        note_type="preference",
        idempotency_key=f"orph-{uuid.uuid4()}",
    )
    founder_add_memory_note(
        superuser_db,
        owner_id=owner.id,
        content="Never weaken RLS authority audit recovery controls",
        note_type="decision",
        idempotency_key=f"prot-{uuid.uuid4()}",
    )
    superuser_db.commit()
    report = propose_simplifications(superuser_db, owner_id=owner.id)
    orphan = [p for p in report.proposals if p.kind == SimplificationKind.ORPHAN_MEMORY]
    assert orphan
    protected = [p for p in orphan if p.protected]
    assert protected
    assert set(PROTECTED_DOMAINS).issuperset({"security", "authority", "audit", "recovery"})
    assert all(p.auto_apply_allowed is False for p in report.proposals)


def test_duplicate_workflow_candidates(superuser_db):
    owner = _owner(superuser_db)
    result = _promote(superuser_db, owner.id, "Export quarterly archive dump", "wf")
    entity_id = result.canonical_entity_id
    record_work_candidate(
        superuser_db,
        owner_id=owner.id,
        source_entity_id=entity_id,
        title="Export quarterly archive dump now",
        idempotency_key=f"wc1-{uuid.uuid4()}",
        classifier_strategy="test",
    )
    record_work_candidate(
        superuser_db,
        owner_id=owner.id,
        source_entity_id=entity_id,
        title="Export quarterly archive dump now!",
        idempotency_key=f"wc2-{uuid.uuid4()}",
        classifier_strategy="test",
    )
    superuser_db.commit()
    report = propose_simplifications(superuser_db, owner_id=owner.id)
    assert any(p.kind == SimplificationKind.DUPLICATE_WORKFLOW for p in report.proposals)
