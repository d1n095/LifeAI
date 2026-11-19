"""Integration tests: the P2/P3/P4 capabilities are CONNECTED to actual MainAI execution
paths, not merely available as standalone modules.

Seams covered (each additive, fail-open):
  1. chat/workbench system prompt  — app/rag/epistemics.py's ladder rollup rides on the
     SAME instruction block as build_trust_instructions (epistemic_status_counts_sql is
     the one-query mirror of classify_claim's ladder; format_epistemic_instructions is
     the line that lands in the system content).
  2. Library source-detail read path — every claim row in GET /api/library/sources/{id}
     carries its epistemic_status (classify_claim) beside the live confidence.
  3. Planner path — planner.create_plan's lesson application is followed by the
     anti-overcorrection advisory (record_lesson_guard_advisory), which records an audit
     row when past-lesson guards would dominate the plan.
  4. Problem-recording path — problem_learning.service.create_problem stamps recurrence
     provenance onto the new row when the same normalized signature already surfaced.
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import text as sa_text

from app.mainai_execution.correction_intelligence import (
    count_prior_occurrences,
    normalize_problem_signature,
)
from app.mainai_execution.lessons import record_lesson_guard_advisory
from app.models.audit import AuditLog
from app.models.document import Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_claim import ClaimConfidence, ClaimStatus, ClaimType, KnowledgeClaim
from app.models.mainai_execution import EngineeringLesson, EngineeringLessonSeverity
from app.models.problem_learning import LifeProblem
from app.problem_learning import service as problem_service
from app.rag.claims import EXTRACTION_VERSION
from app.rag.epistemics import (
    epistemic_status_counts_sql,
    format_epistemic_instructions,
)
from app.config import get_settings

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
EMBEDDING_DIM = get_settings().embedding_dim


def _set_rls_user(session, owner_id) -> None:
    from app.request_context import current_user_id as current_user_id_var

    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_claimed_source(session, owner_id, *, title="Källa", confidence=ClaimConfidence.likely):
    _set_rls_user(session, owner_id)
    source = Document(title=title, source=DocumentSource.upload, uploaded_by=owner_id)
    session.add(source)
    session.commit()
    chunk = DocumentChunk(document_id=source.id, owner_id=owner_id, chunk_index=0, text="Bolaget grundades 2019.", embedding=[0.1] * EMBEDDING_DIM)
    session.add(chunk)
    session.commit()
    claim = KnowledgeClaim(
        owner_id=owner_id, source_id=source.id, chunk_id=chunk.id,
        claim_text="Bolaget grundades 2019.", confidence=confidence,
        claim_type=ClaimType.historical, status=ClaimStatus.active,
        extraction_version=EXTRACTION_VERSION,
    )
    session.add(claim)
    session.commit()
    return source, claim


# --- seam 1: the ladder rollup + instruction line ----------------------------------------


def test_sql_rollup_matches_the_python_ladder(db_session, make_verified_user):
    user, _ = make_verified_user()
    source, claim = _make_claimed_source(db_session, user.id)  # grounded, active, likely
    _make_claimed_source(db_session, user.id, title="Antagande", confidence=ClaimConfidence.no_basis)

    counts = epistemic_status_counts_sql(db_session, user.id)
    assert counts["verified_fact"] == 1
    assert counts["assumption"] == 1
    assert counts["total"] == 2
    assert sum(counts[k] for k in ("verified_fact", "conflicting_evidence", "hypothesis", "assumption", "unknown")) == counts["total"]

    # Owner isolation: another owner sees zero, and never this owner's claims.
    other, _ = make_verified_user()
    other_counts = epistemic_status_counts_sql(db_session, other.id)
    assert other_counts["total"] == 0

    line = format_epistemic_instructions(counts)
    assert "EPISTEMISK STATUS" in line
    assert "1 verifierade" in line and "1 antaganden" in line


def test_sql_rollup_scopes_to_project(db_session, make_verified_user):
    from app.models.project import Project

    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    project = Project(name="P", created_by=user.id)
    db_session.add(project)
    db_session.commit()
    in_project = Document(title="I", source=DocumentSource.upload, uploaded_by=user.id, project_id=project.id)
    db_session.add(in_project)
    db_session.commit()
    chunk = DocumentChunk(document_id=in_project.id, owner_id=user.id, chunk_index=0, text="T.", embedding=[0.1] * EMBEDDING_DIM)
    db_session.add(chunk)
    db_session.commit()
    db_session.add(
        KnowledgeClaim(
            owner_id=user.id, source_id=in_project.id, chunk_id=chunk.id,
            claim_text="T.", confidence=ClaimConfidence.likely,
            claim_type=ClaimType.historical, status=ClaimStatus.active,
            extraction_version=EXTRACTION_VERSION,
        )
    )
    db_session.commit()
    _make_claimed_source(db_session, user.id, title="Utanför")

    assert epistemic_status_counts_sql(db_session, user.id, project_id=project.id)["total"] == 1
    assert epistemic_status_counts_sql(db_session, user.id)["total"] == 2


def test_empty_ledger_instruction_tells_the_truth(db_session, make_verified_user):
    user, _ = make_verified_user()
    counts = epistemic_status_counts_sql(db_session, user.id)
    assert counts["total"] == 0
    assert "inga lagrade påståenden" in format_epistemic_instructions(counts)


# --- seam 2: the Library read path --------------------------------------------------------


def test_source_detail_response_carries_epistemic_status(db_session, client, make_verified_user):
    # require_founder pins to the exact FOUNDER_USER_ID row (app/deps.py) — the rows this
    # endpoint reads must be owned by THAT user, not by an arbitrary verified user.
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    assert founder is not None
    source, claim = _make_claimed_source(db_session, founder.id)

    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200
    detail = client.get(f"/api/library/{source.id}")
    assert detail.status_code == 200, detail.text
    claims = detail.json()["claims"]
    assert len(claims) == 1
    assert claims[0]["id"] == str(claim.id)
    assert claims[0]["epistemic_status"] == "verified_fact"


# --- seam 3: the planner path (anti-overcorrection advisory) ------------------------------


def _make_lesson(db, owner_id, regression_test):
    lesson = EngineeringLesson(
        problem=f"Recurring guard problem {uuid.uuid4()}",
        root_cause="test root cause",
        affected_component="test_component",
        severity=EngineeringLessonSeverity.low,
        evidence="integration test",
        fix="guard added",
        general_rule="always re-check",
        regression_test=regression_test,
        source_type="founder_correction",
        source_ref="epistemics-integration-tests",
        first_seen_at=datetime.utcnow(),
        created_by=str(owner_id),
    )
    db.add(lesson)
    db.commit()
    return lesson


def test_advisory_records_audit_when_guards_dominate(db_session, make_verified_user):
    user, _ = make_verified_user()
    targets = {f"tests/backend/test_guard_{i}.py" for i in range(6)}
    lessons = [_make_lesson(db_session, user.id, t) for t in targets]

    spec_plan = [{"kind": "targeted_tests", "target": "tests/backend/test_real_work.py"}]
    augmented = spec_plan + [{"kind": "targeted_tests", "target": t} for t in targets]

    record_lesson_guard_advisory(
        db_session,
        owner_id=user.id,
        goal_id=uuid.uuid4(),
        task_type="repo_edit",
        spec_verification_plan=spec_plan,
        augmented_verification_plan=augmented,
        applied_lesson_ids=[l.id for l in lessons],
    )
    db_session.commit()

    rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "lesson_guard_overcorrection_advisory")
        .all()
    )
    assert len(rows) == 1
    assert "6 lesson-guard steps" in rows[0].detail


def test_advisory_is_silent_for_a_balanced_plan(db_session, make_verified_user):
    user, _ = make_verified_user()
    lesson = _make_lesson(db_session, user.id, "tests/backend/test_one_guard.py")

    spec_plan = [{"kind": "targeted_tests", "target": "tests/backend/test_real_work.py"}]
    augmented = spec_plan + [{"kind": "targeted_tests", "target": "tests/backend/test_one_guard.py"}]

    before = db_session.query(AuditLog).filter(AuditLog.action == "lesson_guard_overcorrection_advisory").count()
    record_lesson_guard_advisory(
        db_session,
        owner_id=user.id,
        goal_id=uuid.uuid4(),
        task_type="repo_edit",
        spec_verification_plan=spec_plan,
        augmented_verification_plan=augmented,
        applied_lesson_ids=[lesson.id],
    )
    db_session.commit()
    after = db_session.query(AuditLog).filter(AuditLog.action == "lesson_guard_overcorrection_advisory").count()
    assert after == before, "a balanced plan must not raise the advisory"


# --- seam 4: the problem-recording path ---------------------------------------------------


def test_create_problem_stamps_recurrence_provenance(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)  # life_problems is RLS-protected — set per owner
    first = problem_service.create_problem(
        db_session,
        owner_id=user.id,
        title="Backfill skips archived threads",
        description="The nightly backfill skipped archived memory threads again.",
        idempotency_key=f"rec-{uuid.uuid4()}",
    )
    assert "recurrence" not in first.provenance

    second = problem_service.create_problem(
        db_session,
        owner_id=user.id,
        title="  backfill skips archived threads  ",  # same normalized signature
        description="Reproduced after restart.",
        idempotency_key=f"rec-{uuid.uuid4()}",
    )
    recurrence = second.provenance.get("recurrence")
    assert recurrence is not None, "a repeated problem must carry the recurrence signal"
    assert recurrence["prior_occurrences"] == 1
    assert recurrence["occurrences_including_this"] == 2
    assert recurrence["signature"] == normalize_problem_signature("Backfill skips archived threads")

    assert count_prior_occurrences(db_session, user.id, "Backfill skips archived threads") == 2


def test_recurrence_provenance_scopes_to_owner(db_session, make_verified_user):
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()
    _set_rls_user(db_session, owner_a.id)
    problem_service.create_problem(
        db_session, owner_id=owner_a.id, title="Same title across owners",
        description="A's problem", idempotency_key=f"rec-a-{uuid.uuid4()}",
    )
    _set_rls_user(db_session, owner_b.id)
    b_row = problem_service.create_problem(
        db_session, owner_id=owner_b.id, title="Same title across owners",
        description="B's own first problem", idempotency_key=f"rec-b-{uuid.uuid4()}",
    )
    assert "recurrence" not in b_row.provenance, "another owner's identical problem is not a recurrence"
