"""MainAI Cognitive Control Plane -- `app.mainai_vision.gap_generator` -- proves implied
requirements are inferred deterministically (the "autonomous development" example the founder
named explicitly), that IMPLIED REQUIREMENT != EXECUTION AUTHORITY is structurally enforced (no
code path reaches `promote_interpretation_proposal()`), and that persisted proposals only ever
reach the existing staging pipeline.

See docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the architecture this
module implements."""

from __future__ import annotations

import uuid

from app.mainai_vision.gap_generator import persist_gap_proposals, propose_implied_requirements


def test_autonomous_development_implies_the_founders_own_named_list():
    report = propose_implied_requirements(capability_description="We want autonomous development.")
    titles = {r.title for r in report.implied_requirements}
    assert "autonomous development" in report.matched_capability_keys
    for expected in ("durable jobs", "agent supervision", "restart recovery", "context lifecycle management", "cost/quota tracking"):
        assert expected in titles
    assert all(r.authorized is False for r in report.implied_requirements)
    assert report.authorized is False


def test_unmatched_capability_still_runs_missing_piece_scan_without_erroring():
    report = propose_implied_requirements(capability_description="Completely unrelated free text about gardening.")
    assert report.matched_capability_keys == ()
    assert isinstance(report.missing_piece_scan, dict)


def test_duplicate_implied_requirements_across_signals_are_deduplicated():
    report = propose_implied_requirements(capability_description="autonomous development")
    titles = [r.title for r in report.implied_requirements]
    assert len(titles) == len(set(titles))


def test_gap_generator_never_reaches_promote_interpretation_proposal():
    """Structural: IMPLIED REQUIREMENT != EXECUTION AUTHORITY. AST-based (not substring) so this
    module's own prose discussing 'promote_interpretation_proposal()' cannot false-fail."""
    import ast
    import inspect

    import app.mainai_vision.gap_generator as module

    tree = ast.parse(inspect.getsource(module))
    called_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called_names.add(func.id)
            elif isinstance(func, ast.Attribute):
                called_names.add(func.attr)
    assert "promote_interpretation_proposal" not in called_names

    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)
    assert "promote_interpretation_proposal" not in imported_names


def test_propose_implied_requirements_is_pure_no_db():
    import inspect

    sig = inspect.signature(propose_implied_requirements)
    assert "db" not in sig.parameters


# ============================================================================ persistence (staging only)


def test_persist_gap_proposals_only_stages_never_promotes(superuser_db):
    from app.models.document import ActiveTruthStatus, Document, DocumentSource
    from app.models.knowledge_claim import KnowledgeClaim
    from app.models.user import User

    owner = User(email=f"gap-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(owner)
    superuser_db.flush()
    document = Document(title="Source", source=DocumentSource.upload, uploaded_by=owner.id, active_truth_status=ActiveTruthStatus.active)
    superuser_db.add(document)
    superuser_db.flush()
    claim = KnowledgeClaim(owner_id=owner.id, source_id=document.id, claim_text="autonomous development", extraction_version="v1")
    superuser_db.add(claim)
    superuser_db.flush()
    superuser_db.commit()

    report = propose_implied_requirements(capability_description="autonomous development")
    rows = persist_gap_proposals(superuser_db, owner_id=owner.id, source_claim_id=claim.id, report=report)
    superuser_db.commit()

    assert len(rows) == len(report.implied_requirements)
    assert all(row.status == "unreviewed" for row in rows)
    assert all(row.promoted_to_entity_id is None for row in rows)
    assert all(row.proposed_entity_type == "implied_requirement" for row in rows)


def test_persist_gap_proposals_is_idempotent_per_owner_and_title(superuser_db):
    from app.models.document import ActiveTruthStatus, Document, DocumentSource
    from app.models.knowledge_claim import KnowledgeClaim
    from app.models.user import User

    owner = User(email=f"gap2-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(owner)
    superuser_db.flush()
    document = Document(title="Source", source=DocumentSource.upload, uploaded_by=owner.id, active_truth_status=ActiveTruthStatus.active)
    superuser_db.add(document)
    superuser_db.flush()
    claim = KnowledgeClaim(owner_id=owner.id, source_id=document.id, claim_text="autonomous development", extraction_version="v1")
    superuser_db.add(claim)
    superuser_db.flush()
    superuser_db.commit()

    report = propose_implied_requirements(capability_description="autonomous development")
    first = persist_gap_proposals(superuser_db, owner_id=owner.id, source_claim_id=claim.id, report=report)
    superuser_db.commit()
    second = persist_gap_proposals(superuser_db, owner_id=owner.id, source_claim_id=claim.id, report=report)
    superuser_db.commit()

    assert {r.id for r in first} == {r.id for r in second}
