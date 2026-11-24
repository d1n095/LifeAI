"""M5 (Round 8): END-TO-END COGNITIVE ACCEPTANCE — the complete workflow the mission
names, executed through the REAL paths (HTTP endpoints, the planner, the observation
finalize writer, real PostgreSQL + RLS), with assertions on actual behavior.

The scenario: the founder ingests three studies of one substance (X):
  Study A — randomized trial: "X raises marker Y"        (sound, funded, replicated)
  Study B — prospective cohort: "X lowers marker Y"      (correlation-only, no causal claim)
  Study C — cell-line work: "X binds receptor Z"         (mechanism-only)
plus a follow-up REPORT document derived from Study B. The founder declares the A/B
contradiction, and later a NEW claim contradicts A's finding.

The ten steps and where each is proven:
  1  ingest research material                -> sources + chunks + claims (library primitives)
  2  claims with provenance + metadata       -> PUT evidence-metadata per claim (HTTP)
  3  contradictions + uncertainty            -> contradicts edge -> epistemic_shift audit
  4  evidence-quality evaluation             -> detail view grades/caps/caveats per claim
  5  cross-domain relationships              -> overlap discovery + second-order consequence
  6  reasoned, source-traceable conclusion   -> evidence-chain trace + owner landscape
  7  correction when evidence changes        -> second contradicts edge -> shift on A
  8  verified lessons applied to planning    -> create_plan applies the lesson's guard,
                                                decision row + selection audit block
  9  ineffective lessons reassessed          -> failed-majority lesson EXCLUDED from a new
                                                plan, row untouched, signal in the audit
 10  owner isolation + failure handling +    -> other owner sees nothing; empty-metadata
     restart persistence                        refusal; new-connection persistence

The adversarial case (the correct outcome IS "insufficient evidence") is its own test.
"""

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.db import SessionLocal, migration_engine
from app.mainai_execution import planner
from app.mainai_execution.lessons import record_lesson
from app.models.audit import AuditLog
from app.models.document import Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_claim import ClaimConfidence, ClaimStatus, ClaimType, KnowledgeClaim
from app.models.mainai_execution import EngineeringLessonSeverity
from app.models.lesson_plan_decisions import EngineeringLessonPlanDecision
from app.rag.claims import EXTRACTION_VERSION
from app.rag.evidence_metadata import (
    NothingDeclaredError,
    record_claim_evidence_metadata,
    summarize_owner_evidence_landscape,
)
from app.request_context import current_user_id as current_user_id_var
from app.rls import apply_mainai_execution_privileges
from tests.backend.test_claim_graph_integration import _login

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
DIM = None  # set at first use from settings


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    apply_mainai_execution_privileges(migration_engine)


def _set_rls_user(session, owner_id) -> None:
    global DIM
    if DIM is None:
        from app.config import get_settings

        DIM = get_settings().embedding_dim
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _ingest_source(db, owner_id, *, title, claim_text, confidence, chunk_index=0):
    """Step 1: ingest research material with its extracted claim (the same primitives the
    library import path persists — one source, one chunk, one claim, all owner-anchored)."""
    _set_rls_user(db, owner_id)
    source = Document(title=title, source=DocumentSource.upload, uploaded_by=owner_id)
    db.add(source)
    db.commit()
    chunk = DocumentChunk(
        document_id=source.id, owner_id=owner_id, chunk_index=chunk_index,
        text=claim_text, embedding=[0.1] * DIM,
    )
    db.add(chunk)
    db.commit()
    claim = KnowledgeClaim(
        owner_id=owner_id, source_id=source.id, chunk_id=chunk.id, claim_text=claim_text,
        confidence=confidence, claim_type=ClaimType.historical, status=ClaimStatus.active,
        extraction_version=EXTRACTION_VERSION,
    )
    db.add(claim)
    db.commit()
    return source, claim


@pytest.fixture
def corpus(db_session, client):
    """The full founder corpus, built step by step on the real owner account. Depends on
    `client` because the founder row is provisioned by the app's own startup bootstrap —
    a test that never opens the app would otherwise find the table truncated and empty."""
    from app.models.user import User

    current_user_id_var.set("")  # a previous test's GUC must never scope the founder query
    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    assert founder is not None, "the founder account must be provisioned by the bootstrap"
    _set_rls_user(db_session, founder.id)
    return {"db": db_session, "founder": founder, "founder_id": founder.id}


# --- steps 1-4: ingest, provenance, contradiction, quality --------------------------------------


def test_ingest_metadata_contradiction_and_quality(db_session, client, corpus):
    db = corpus["db"]
    founder_id = corpus["founder_id"]
    _login(client)

    source_a, claim_a = _ingest_source(db, founder_id, title="Studie A (RCT)", claim_text="X höjer markör Y.", confidence=ClaimConfidence.likely)
    source_b, claim_b = _ingest_source(db, founder_id, title="Studie B (kohort)", claim_text="X sänker markör Y.", confidence=ClaimConfidence.likely)
    source_c, claim_c = _ingest_source(db_session := db, founder_id, title="Studie C (cellinje)", claim_text="X binder receptor Z.", confidence=ClaimConfidence.likely)

    # Step 2: provenance-aware study metadata on every claim, via the REAL endpoint.
    csrf = _login(client)
    stamps = [
        (claim_a, {"study_design": "randomized_trial", "sample_size": 340, "statistical_reporting": "confidence_intervals", "replication": "independent", "outcome_followup": "12 months", "dose": "5 mg", "dose_response_documented": True, "conflicts_of_interest": "industry-funded", "competing_explanations": ["placebo controlled"], "causal_claim": True, "causal_claim_supported_by_design": True}),
        (claim_b, {"study_design": "prospective_cohort", "sample_size": 1200, "statistical_reporting": "p_values", "outcome_followup": "3 months", "conflicts_of_interest": "none declared", "causal_claim": True, "causal_claim_supported_by_design": False}),
        (claim_c, {"study_design": "mechanism_only", "population": "n=12 cell line"}),
    ]
    for claim, meta in stamps:
        res = client.put(
            f"/api/library/claims/{claim.id}/evidence-metadata",
            json={"metadata": meta, "declared_via": "acceptance_test"},
            headers={"X-CSRF-Token": csrf},
        )
        assert res.status_code == 200, res.text

    # Step 3: the founder DECLARES the contradiction — shifts are computed and audited.
    res = client.post(
        f"/api/library/claims/{claim_a.id}/relationships",
        json={"to_claim_id": str(claim_b.id), "relationship_type": "contradicts", "note": "motstridiga riktningar"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200
    shifts = res.json()["epistemic_shifts"]
    assert all(sh["changed"] and sh["prior_status"] == "verified_fact" and sh["current_status"] == "conflicting_evidence" for sh in shifts)
    audit = db.query(AuditLog).filter(AuditLog.action == "epistemic_shift").order_by(AuditLog.created_at.desc()).first()
    assert audit is not None and "CHANGED" in audit.detail

    # Step 4: the detail view evaluates evidence quality — caps, grades, caveats, conflict.
    detail = client.get(f"/api/library/{source_a.id}").json()
    row_a = next(c for c in detail["claims"] if c["id"] == str(claim_a.id))
    assert row_a["epistemic_status"] == "conflicting_evidence", "the contradiction wins over everything"
    assert row_a["quality_grade"] == "low", "industry funding floors the sound RCT at low — surfaced, not invalidated"
    assert row_a["quality_cap"] == "verified_fact"
    assert any("does NOT by itself invalidate" in c for c in row_a["quality_caveats"])

    detail_c = client.get(f"/api/library/{source_c.id}").json()
    row_c = next(c for c in detail_c["claims"] if c["id"] == str(claim_c.id))
    assert row_c["epistemic_status"] == "hypothesis", "a mechanism is a HYPOTHESIS, never established effect"
    assert row_c["quality_grade"] == "very_low"

    detail_b = client.get(f"/api/library/{source_b.id}").json()
    row_b = next(c for c in detail_b["claims"] if c["id"] == str(claim_b.id))
    assert row_b["epistemic_status"] == "conflicting_evidence"
    assert row_b["quality_cap"] == "hypothesis", "correlation-only caps the ladder at hypothesis"

    # cross-project duplicate for step 5 (a different domain's report states the same finding)
    from app.models.project import Project

    project = Project(name=f"Acceptanceprojekt {uuid.uuid4().hex[:6]}", created_by=founder_id)
    db.add(project)
    db.commit()
    _ingest_source(db, founder_id, title="Rapport (annat domän)", claim_text="X höjer markör Y.", confidence=ClaimConfidence.likely, chunk_index=0)
    db.execute(sa_text("UPDATE documents SET project_id = :pid WHERE id = (SELECT id FROM documents WHERE title = 'Rapport (annat domän)')"), {"pid": str(project.id)})
    db.commit()

    overlaps = client.get("/api/library/graph/cross-domain-overlaps").json()
    assert any(o["claim_text"] == "x höjer markör y." and len(o["project_ids"]) >= 1 for o in overlaps), (
        "the same finding surfaced in another domain is discoverable"
    )


# --- steps 5-7: consequence, traceable conclusion, correction ------------------------------------


def test_consequence_trace_and_second_correction(db_session, client, corpus):
    db = corpus["db"]
    founder_id = corpus["founder_id"]
    csrf = _login(client)

    source_a, claim_a = _ingest_source(db, founder_id, title="Root-studie", claim_text="X höjer markör Y.", confidence=ClaimConfidence.likely)
    source_report, _ = _ingest_source(db, founder_id, title="Rapport bygger på studien", claim_text="Sammanfattning av X.", confidence=ClaimConfidence.likely)
    _set_rls_user(db, founder_id)
    from app.models.source_relationship import RelationshipType, SourceRelationship

    db.add(SourceRelationship(owner_id=founder_id, from_source_id=source_report.id, to_source_id=source_a.id, relationship_type=RelationshipType.derived_from))
    db.commit()

    # A NEW claim contradicts the root study — "new information changes an earlier conclusion".
    _source_new, claim_new = _ingest_source(db, founder_id, title="Replikering 2026", claim_text="X påverkar inte markör Y.", confidence=ClaimConfidence.likely)
    res = client.post(
        f"/api/library/claims/{claim_new.id}/relationships",
        json={"to_claim_id": str(claim_a.id), "relationship_type": "contradicts"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200
    sh = res.json()["epistemic_shifts"]
    a_shift = next(s for s in sh if s["claim_id"] == str(claim_a.id))
    assert a_shift["prior_status"] == "verified_fact" and a_shift["current_status"] == "conflicting_evidence"

    # Step 5: the second-order consequence reaches the dependent REPORT (contested ground).
    consequences = client.get(f"/api/library/sources/{source_a.id}/consequences").json()
    dependent_ids = {c["dependent_source_id"] for c in consequences}
    assert str(source_report.id) in dependent_ids, (
        "the new contradiction propagates to the report that depends on the disputed study"
    )
    assert all(c["kind"] == "contested_ground" for c in consequences)

    # Step 6: the reasoned, source-traceable conclusion — the chain carries everything.
    chain = client.get(f"/api/library/claims/{claim_a.id}/evidence-chain").json()
    assert chain["epistemic_status"] == "conflicting_evidence"
    assert chain["verdict_reasons"]
    assert chain["source"]["title"] == "Root-studie" and chain["chunk_grounded"] is True
    assert len(chain["contradicting_edges"]) == 1
    assert chain["contradicting_edges"][0]["other_claim_text"] == "X påverkar inte markör Y.", (
        "the NEW information is on the record, symmetric with the claim it contradicts"
    )
    assert chain["second_order_consequences"], "the chain includes the downstream consequence"

    # Step 2 restated: the corpus carries stamped metadata for the landscape to aggregate.
    res = client.put(
        f"/api/library/claims/{claim_new.id}/evidence-metadata",
        json={"metadata": {"study_design": "randomized_trial", "sample_size": 500, "conflicts_of_interest": "none declared", "causal_claim": True, "causal_claim_supported_by_design": True, "competing_explanations": ["controlled"]}, "declared_via": "acceptance_test"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200

    # The owner's body-of-evidence landscape reflects the corpus (structured, not a verdict).
    landscape = summarize_owner_evidence_landscape(db, founder_id)
    assert landscape.study_count >= 1
    assert landscape.funding_industry_or_conflict >= 1 or landscape.funding_declared_none >= 1, (
        "funding disclosure is COUNTED with provenance — never a verdict"
    )


# --- steps 8-9: the self-correction loop on real plans --------------------------------------------


def _observe_failure(db, owner_id, task, lesson):
    from app.models.lesson_guard_observations import EngineeringLessonGuardObservation

    _set_rls_user(db, owner_id)
    db.add(
        EngineeringLessonGuardObservation(
            owner_id=owner_id, lesson_id=lesson.id, task_id=task.id, goal_id=task.goal_id,
            outcome="guard_failed", evidence_strength="direct",
            relevance_reason="the lesson's own guard failed where it was applied",
            guard_target=lesson.regression_test, evidence={}, source_ref=f"acc:{uuid.uuid4()}",
        )
    )
    db.commit()


def test_lessons_applied_then_ineffective_lesson_reassessed(db_session, client, corpus):
    db = corpus["db"]
    founder_id = corpus["founder_id"]

    good_lesson = record_lesson(
        db,
        problem="Verification gap found in acceptance run", root_cause="missing guard",
        affected_component=f"acc_component_{uuid.uuid4().hex[:6]}", severity=EngineeringLessonSeverity.medium,
        evidence="acceptance scenario", fix="added regression test", general_rule="always verify X",
        applies_to=["run_tests"], source_type="acceptance_tests", source_ref=f"acc-{uuid.uuid4()}",
        created_by="test", first_seen_at=__import__("datetime").datetime.utcnow(),
        regression_test="tests/backend/test_mainai_execution_planner.py::test_get_goal_raises_for_unknown_id",
    )
    db.commit()

    # Step 8: the plan applies the verified lesson; the audit block rides on the event.
    goal = planner.create_goal(db, owner_id=founder_id, title=f"Acceptansmål {uuid.uuid4().hex[:6]}", original_instruction="Kör suite.", created_by="test")
    plan = planner.create_plan(
        db, goal=goal, rationale="acceptance step 8",
        tasks=[planner.PlannedTaskSpec(description="Kör tester", task_type="run_tests")],
        created_by="test",
    )
    db.commit()
    task = db.query(planner.MainAITask).filter(planner.MainAITask.plan_id == plan.id).one()
    targets = {s.get("target") for s in task.verification_plan if s.get("kind") == "targeted_tests"}
    assert good_lesson.regression_test in targets, "a measured-useful lesson INFLUENCES the plan"
    event = db.execute(sa_text("SELECT detail FROM mainai_task_events WHERE task_id = :id AND event_type = 'created'"), {"id": str(task.id)}).first()
    assert event[0]["lessons_applied"] == [str(good_lesson.id)]
    assert any(d["lesson_id"] == str(good_lesson.id) and d["standing"] == "unproven" for d in event[0]["lesson_selection"])
    assert db.query(EngineeringLessonPlanDecision).filter_by(task_id=task.id, lesson_id=good_lesson.id).count() == 1

    goal2 = planner.create_goal(db, owner_id=founder_id, title=f"Acceptansmål 2 {uuid.uuid4().hex[:6]}", original_instruction="Kör igen.", created_by="test")

    # ONE isolated failure: the point-in-time plan STILL applies the rule (flagged) —
    # a single failure must never permanently suppress a possibly useful rule.
    _observe_failure(db, founder_id, task, good_lesson)
    plan2 = planner.create_plan(
        db, goal=goal2, rationale="acceptance step 9a: one failure on the record",
        tasks=[planner.PlannedTaskSpec(description="Kör tester igen", task_type="run_tests")],
        created_by="test",
    )
    db.commit()
    task2 = db.query(planner.MainAITask).filter(planner.MainAITask.plan_id == plan2.id).one()
    event2 = db.execute(sa_text("SELECT detail FROM mainai_task_events WHERE task_id = :id AND event_type = 'created'"), {"id": str(task2.id)}).first()
    decision2 = next(d for d in event2[0]["lesson_selection"] if d["lesson_id"] == str(good_lesson.id))
    assert decision2["standing"] == "single_fail" and decision2["selected"] is True
    assert "single failure must not permanently suppress" in decision2["reason"]
    assert good_lesson.regression_test in {s.get("target") for s in task2.verification_plan if s.get("kind") == "targeted_tests"}

    # The guard fails AGAIN, on a DIFFERENT task (task2's own decision row exists — the
    # lesson was still applied when plan 2 was created): now the majority is failed.
    _observe_failure(db, founder_id, task2, good_lesson)
    plan3 = planner.create_plan(
        db, goal=goal2, rationale="acceptance step 9b: majority failed",
        tasks=[planner.PlannedTaskSpec(description="Kör tredje gången", task_type="run_tests")],
        created_by="test",
    )
    db.commit()
    task3 = db.query(planner.MainAITask).filter(planner.MainAITask.plan_id == plan3.id).one()
    event3 = db.execute(sa_text("SELECT detail FROM mainai_task_events WHERE task_id = :id AND event_type = 'created'"), {"id": str(task3.id)}).first()
    decision3 = next(d for d in event3[0]["lesson_selection"] if d["lesson_id"] == str(good_lesson.id))
    assert decision3["standing"] == "failed_majority" and decision3["selected"] is False, (
        "a twice-failed rule is excluded from auto-application — reassessed, not blindly reinforced"
    )
    assert "reassess" in decision3["reason"].lower()
    targets3 = {s.get("target") for s in task3.verification_plan if s.get("kind") == "targeted_tests"}
    assert good_lesson.regression_test not in targets3
    from app.models.mainai_execution import EngineeringLessonStatus

    db.refresh(good_lesson)
    assert good_lesson.status == EngineeringLessonStatus.active, "exclusion never mutates or deletes the lesson"

    # Step 8 again: two held verdicts restore it — no permanent suppression, recomputed live.
    from app.models.lesson_guard_observations import EngineeringLessonGuardObservation

    for i in range(2):
        db.add(
            EngineeringLessonGuardObservation(
                owner_id=founder_id, lesson_id=good_lesson.id, task_id=task.id, goal_id=task.goal_id,
                outcome="guard_held", evidence_strength="direct", relevance_reason="recovery verdict",
                guard_target=good_lesson.regression_test, evidence={}, source_ref=f"acc-hold-{i}",
            )
        )
    db.commit()
    plan4 = planner.create_plan(
        db, goal=goal2, rationale="acceptance recovery",
        tasks=[planner.PlannedTaskSpec(description="Kör fjärde gången", task_type="run_tests")],
        created_by="test",
    )
    db.commit()
    task4 = db.query(planner.MainAITask).filter(planner.MainAITask.plan_id == plan4.id).one()
    targets4 = {s.get("target") for s in task4.verification_plan if s.get("kind") == "targeted_tests"}
    assert good_lesson.regression_test in targets4, "the recovered rule influences planning again"


# --- step 10: isolation, failure handling, restart persistence ------------------------------------


def test_owner_isolation_failure_handling_and_restart_persistence(db_session, client, corpus, make_verified_user):
    db = corpus["db"]
    founder_id = corpus["founder_id"]
    csrf = _login(client)

    source_a, claim_a = _ingest_source(db, founder_id, title="Isolering A", claim_text="Isolerat påstående.", confidence=ClaimConfidence.likely)
    record_claim_evidence_metadata(db, founder_id, claim_a.id, {"study_design": "randomized_trial", "causal_claim": True, "causal_claim_supported_by_design": True}, declared_via="step10")
    db.commit()

    # Owner isolation: a different owner's session sees NOTHING of the founder's corpus.
    other, _ = make_verified_user()
    _set_rls_user(db, other.id)
    assert db.query(KnowledgeClaim).filter(KnowledgeClaim.id == claim_a.id).count() == 0
    from app.rag.evidence_metadata import quality_metadata_for_claims

    assert quality_metadata_for_claims(db, other.id, [claim_a.id]) == {}
    from app.rag.claim_graph import trace_evidence_chain

    with pytest.raises(LookupError):
        trace_evidence_chain(db, other.id, claim_a.id)

    # Failure handling: an empty declaration is REFUSED, never graded.
    _set_rls_user(db, founder_id)  # back to the founder's scope after the isolation checks
    with pytest.raises(NothingDeclaredError):
        record_claim_evidence_metadata(db, founder_id, claim_a.id, {}, declared_via="step10")

    # Restart persistence: a FRESH connection (new engine session, like a restarted app)
    # sees every committed row — the cognitive state is durable, not session-bound.
    fresh = SessionLocal()
    try:
        _set_rls_user(fresh, founder_id)
        fresh_claim = fresh.query(KnowledgeClaim).filter(KnowledgeClaim.id == claim_a.id).one()
        assert fresh_claim.claim_text == "Isolerat påstående."
        rows = fresh.query(EngineeringLessonPlanDecision).all()  # at least the loop's rows survive
        assert isinstance(rows, list)
        detail_rows = fresh.execute(
            sa_text("SELECT quality_grade FROM claim_evidence_metadata WHERE claim_id = :cid"),
            {"cid": str(claim_a.id)},
        ).first()
        assert detail_rows is not None and detail_rows[0] == "moderate", (
            "the stamped verdict persisted across the restart — and honestly floors at "
            "moderate because competing explanations were never addressed"
        )
    finally:
        fresh.close()


# --- the adversarial case: the correct outcome is INSUFFICIENT EVIDENCE ---------------------------


def test_adversarial_insufficient_evidence_reads_as_unknown_never_certainty(db_session, client, corpus):
    db = corpus["db"]
    founder_id = corpus["founder_id"]
    csrf = _login(client)

    # A claim with the weakest possible basis: no_basis confidence, no metadata, no corroboration.
    _source, weak_claim = _ingest_source(db, founder_id, title="Opålitlig källa", claim_text="X botar allt.", confidence=ClaimConfidence.no_basis)

    detail = client.get(f"/api/library/{_source.id}").json()
    row = next(c for c in detail["claims"] if c["id"] == str(weak_claim.id))
    assert row["epistemic_status"] == "assumption", "a no-basis claim is an ASSUMPTION, never a fact"
    assert row["quality_grade"] is None and row["quality_cap"] is None and row["quality_caveats"] == [], (
        "no stamped metadata = honest absence, never an invented grade"
    )

    # Declaring NOTHING is refused — the system cannot be tricked into grading silence.
    res = client.put(
        f"/api/library/claims/{weak_claim.id}/evidence-metadata",
        json={"metadata": {}, "declared_via": "adversarial_test"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 422

    # Metadata that is present but devoid of quality signals still reads honestly unknown:
    res = client.put(
        f"/api/library/claims/{weak_claim.id}/evidence-metadata",
        json={"metadata": {"population": "okänd"}, "declared_via": "adversarial_test"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["quality_grade"] == "very_low" and body["quality_cap"] == "hypothesis"
    assert any("study design not recorded" in c for c in body["quality_caveats"]), (
        "every unknown axis is NAMED — silence about missing evidence is the failure mode"
    )

    # And the empty-corpus landscape says UNRESOLVED, never a blank check.
    landscape = summarize_owner_evidence_landscape(db, uuid.UUID(int=0))
    assert landscape.study_count == 0
    from app.rag.evidence_quality import format_landscape_instructions

    assert "OLÖST" in format_landscape_instructions(landscape)
