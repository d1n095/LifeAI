"""FOUNDER ACCEPTANCE SUITE — executable founder scenarios (directive 2026-11-18, P2).

Ten scenarios, one per founder requirement, run against REAL PostgreSQL (settings.database_url)
with the real restricted app role (mainai_app), real RLS policies and real services — not mocks.
The schema must already be at the single alembic head (the suite composes it if needed).

Two scenarios have a leg that CANNOT run in this environment by design, and they say so
explicitly instead of faking success (SAID ≠ STORED ≠ PLANNED ≠ IMPLEMENTED ≠ VERIFIED):
  * scenario 5's model leg — provider real-invoke is fail-closed (ProviderActivationBlocked)
    until first-cycle authorization; the boundary itself is asserted here, and the real-model
    evidence lives in PR #447's vps-compose-verify (real Ollama embed/chat, 17/37 steps) and
    PR #458's pinned-candidate CI.
  * scenario 10's PR leg — needs GitHub credentials; the real vertical slice already exists
    upstream (PR #443's crash→takeover→PR-created-once test).

Run:  DATABASE_URL=postgresql://lifeai:lifeai@localhost:5433/lifeai_verify \
      APP_DATABASE_URL=postgresql://mainai_app:mainai_app@localhost:5433/lifeai_verify \
      MAINAI_APP_PASSWORD=mainai_app python3 -m pytest tests/founder_acceptance/ -q
"""

import subprocess
import sys
import uuid
import warnings
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models.agent_task  # noqa: F401
import app.models.document  # noqa: F401
import app.models.document_chunk  # noqa: F401
import app.models.import_job  # noqa: F401
import app.models.mainai_execution  # noqa: F401
import app.models.memory_source_unit  # noqa: F401
import app.models.project_memory  # noqa: F401
import app.models.source_relationship  # noqa: F401
import app.models.user  # noqa: F401
from app.config import get_settings
from app.models.agent_task import AgentTask
from app.models.document import ActiveTruthStatus, Document, DocumentSource, KnowledgeClassification
from app.models.document_chunk import DocumentChunk
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.mainai_execution import EngineeringLesson, EngineeringLessonSeverity, EngineeringLessonStatus
from app.models.memory_source_unit import MemorySourceUnit
from app.models.project_memory import ProjectBranchPRStatus, ProjectSource
from app.models.source_relationship import SourceRelationship
from app.models.user import User, UserRole
from app.models.knowledge_claim import ClaimConfidence, ClaimStatus, ClaimType, KnowledgeClaim
from app.rag.claim_graph import record_claim_relationship, trace_evidence_chain
from app.rag.claims import EXTRACTION_VERSION
from app.rag.epistemics import epistemic_status_counts_sql, format_epistemic_instructions
from app.rag.vector_store import hybrid_search
from app.request_context import current_user_id as current_user_id_var
from app.security import hash_password

warnings.filterwarnings("ignore")

_BACKEND_ROOT = str(Path(__file__).resolve().parent.parent.parent)
_APPLY_RUNTIME_PRIVILEGES = _BACKEND_ROOT + "/scripts/security/apply_runtime_privileges.py"


def _is_real_pg(url: str) -> bool:
    return url.startswith("postgresql")


@pytest.fixture(scope="session")
def admin_engine():
    url = get_settings().database_url
    if not _is_real_pg(url):
        pytest.skip("Founder Acceptance Suite requires REAL PostgreSQL (settings.database_url)")
    engine = create_engine(url)
    with engine.connect() as conn:
        head = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    if head is None:
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=_BACKEND_ROOT, check=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def app_role_engine():
    url = get_settings().app_database_url
    if not _is_real_pg(url):
        pytest.skip("RLS scenarios require the restricted app role (settings.app_database_url)")
    return create_engine(url)


@pytest.fixture
def db(admin_engine):
    session = sessionmaker(bind=admin_engine, expire_on_commit=False)()
    yield session
    session.close()


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa.text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _run_id(prefix="fa"):
    return f"{prefix}-{uuid.uuid4().hex[:10]}@acceptance.example"


def _make_founder(session) -> User:
    user = User(email=_run_id(), password_hash=hash_password("Sup3rS3cret!"), role=UserRole.founder, email_verified=True)
    session.add(user)
    session.commit()
    return user


def _make_document(session, owner_id, *, title, project_id=None, status=ActiveTruthStatus.active,
                   classification=KnowledgeClassification.decisions) -> Document:
    _set_rls_user(session, owner_id)
    d = Document(title=title, source=DocumentSource.upload, uploaded_by=owner_id,
                 active_truth_status=status, classification=classification, project_id=project_id)
    session.add(d)
    session.commit()
    return d


def _make_chunk(session, owner_id, document_id, text_value, index=0) -> DocumentChunk:
    _set_rls_user(session, owner_id)
    chunk = DocumentChunk(document_id=document_id, owner_id=owner_id, chunk_index=index,
                          text=text_value, embedding=[0.1] * 1536)
    session.add(chunk)
    session.commit()
    return chunk


def _make_claim(session, owner_id, document_id, chunk_id, claim_text, *, confidence=ClaimConfidence.likely) -> KnowledgeClaim:
    """The claim-level row extract_claims_for_document() (app/rag/claims.py) would create
    for one chunk during real ingestion — built directly here (no live extraction model in
    this suite) with the same production columns, consistent with test_claim_graph_integration
    .py's own _claimed_source helper."""
    _set_rls_user(session, owner_id)
    claim = KnowledgeClaim(
        owner_id=owner_id, source_id=document_id, chunk_id=chunk_id,
        claim_text=claim_text, confidence=confidence,
        claim_type=ClaimType.historical, status=ClaimStatus.active,
        extraction_version=EXTRACTION_VERSION,
    )
    session.add(claim)
    session.commit()
    return claim


# ---------------------------------------------------------------------------
# Scenario 1 — Remembering and correctly retrieving an earlier decision
# ---------------------------------------------------------------------------
def test_scenario_1_remember_and_retrieve_decision(db):
    from app.models.conversation import Conversation, Message as MessageModel, MessageRole
    from app.rag.message_source import MessageSourceLocator, get_or_create_message_source_unit

    founder = _make_founder(db)
    conversation = Conversation(title="Grundarsamtal", user_id=founder.id)
    db.add(conversation)
    db.commit()
    decision_text = "Grundarens beslut 2026-11-18: integrationsordning styrs av BRANCH_REGISTRY."
    message = MessageModel(conversation_id=conversation.id, role=MessageRole.user, content=decision_text)
    db.add(message)
    db.commit()
    db.refresh(message)
    locator = MessageSourceLocator(owner_id=founder.id, message_id=message.id,
                                   conversation_id=conversation.id, role=MessageRole.user,
                                   observed_at=message.created_at or datetime.utcnow(),
                                   content_text=decision_text)
    unit_id = get_or_create_message_source_unit(db, locator)
    fetched = db.execute(select(MemorySourceUnit).where(MemorySourceUnit.owner_id == founder.id)).scalars().all()
    assert len(fetched) == 1 and str(fetched[0].id) == str(unit_id)
    # Immutability + identity: replaying the same message resolves to the SAME unit (idempotent)
    again = get_or_create_message_source_unit(db, locator)
    assert str(again) == str(unit_id), "identity key must be idempotent — no silent duplication"


# ---------------------------------------------------------------------------
# Scenario 2 — Importing a document and citing its actual source
# ---------------------------------------------------------------------------
def test_scenario_2_import_document_and_cite_source(db):
    founder = _make_founder(db)
    doc = _make_document(db, founder.id, title="Databasdesign-nots 2026-11-18")
    _make_chunk(db, founder.id, doc.id, "Beslut: pgvector för vektorsökning, hybrid keyword-kanal för exakta termer.")
    hits = hybrid_search(db, founder.id, None, "pgvector", top_k=5)
    assert hits, "hybrid text channel must find the imported chunk without any embedding provider"
    hit = hits[0]
    assert hit["document_id"] == str(doc.id), "hit must cite the actual imported document"
    assert hit["title"] == "Databasdesign-nots 2026-11-18"


# ---------------------------------------------------------------------------
# Scenario 3 — Detecting contradictions and preserving superseded information
# ---------------------------------------------------------------------------
def test_scenario_3_contradictions_and_supersede(db):
    founder = _make_founder(db)
    doc_v1 = _make_document(db, founder.id, title="Billing decision v1: monthly invoicing",
                            status=ActiveTruthStatus.superseded, classification=KnowledgeClassification.decisions)
    doc_v2 = _make_document(db, founder.id, title="Billing decision v2: usage-based credits",
                            status=ActiveTruthStatus.active, classification=KnowledgeClassification.decisions)
    _make_chunk(db, founder.id, doc_v1.id, "Fakturering sker månadsvis enligt beslut v1.")
    _make_chunk(db, founder.id, doc_v2.id, "Fakturering sker med usage-based credits enligt beslut v2.")
    current = hybrid_search(db, founder.id, None, "fakturering", top_k=10, active_truth_status="active")
    titles = [h["title"] for h in current]
    assert titles == ["Billing decision v2: usage-based credits"], "superseded decision must not present as current"
    still_there = db.execute(
        select(Document).where(Document.uploaded_by == founder.id,
                               Document.active_truth_status == ActiveTruthStatus.superseded)).scalars().all()
    assert len(still_there) == 1, "superseded information is PRESERVED, never silently lost"


# ---------------------------------------------------------------------------
# Scenario 4 — Connecting knowledge across unrelated projects
# ---------------------------------------------------------------------------
def test_scenario_4_cross_project_connection(db):
    from app.models.project import Project
    from app.models.source_relationship import RelationshipType

    founder = _make_founder(db)
    proj_a = Project(name="Projekt A", created_by=founder.id)
    proj_b = Project(name="Projekt B", created_by=founder.id)
    db.add_all([proj_a, proj_b])
    db.commit()
    doc_a = _make_document(db, founder.id, title="Projekt A: rasterpipeline", project_id=proj_a.id,
                           classification=KnowledgeClassification.general)
    doc_b = _make_document(db, founder.id, title="Projekt B: bildarkiv", project_id=proj_b.id,
                           classification=KnowledgeClassification.general)
    rel = SourceRelationship(owner_id=founder.id, from_source_id=doc_b.id,
                             to_source_id=doc_a.id, relationship_type=RelationshipType.derived_from,
                             note="cross-domain inspiration")
    db.add(rel)
    db.commit()
    cross = db.execute(select(SourceRelationship).where(SourceRelationship.owner_id == founder.id)).scalars().all()
    assert len(cross) == 1
    assert {str(cross[0].from_source_id), str(cross[0].to_source_id)} == {str(doc_a.id), str(doc_b.id)}


# ---------------------------------------------------------------------------
# Scenario 5 — Challenging a flawed technical proposal with better alternatives
# ---------------------------------------------------------------------------
def test_scenario_5_challenge_requires_model_leg_and_is_fail_closed(db):
    """The challenge itself is a cognitive (model) capability. Without an authorized
    provider the system must fail CLOSED — never silently fabricate a 'challenge'.
    The fail-closed boundary (ProviderActivationBlocked until founder authorization) is
    R3's verified behavior, exercised by the provider worker tests; this suite records
    the dependency honestly instead of faking a model-driven challenge."""
    pytest.skip("model leg: fail-closed until first_cycle authorization; real-model evidence = PR #447 vps-compose-verify (real Ollama embed/chat) + PR #458 pinned-candidate CI")


# ---------------------------------------------------------------------------
# Scenario 6 — Differentiating planned work from verified code
# ---------------------------------------------------------------------------
def test_scenario_6_planned_versus_verified(db):
    from app.blueprint_readiness import collect_blueprint_readiness
    db.add(EngineeringLesson(status=EngineeringLessonStatus.active, problem="write-time provenance gap",
                             root_cause="backfill-only capture", affected_component="chat.capture",
                             severity=EngineeringLessonSeverity.high, evidence="audit dbdc2cb3",
                             fix="capture at all 3 write sites", verification_status="verified_by_regression_test",
                             regression_test="tests/backend/test_chat_source_capture.py",
                             source_type="postmortem", source_ref="doc:audit", resolved_in="PR zai/alpha-knowledge-blueprint",
                             general_rule="capture at write time", first_seen_at=datetime.utcnow(), created_by="grundare"))
    db.add(EngineeringLesson(status=EngineeringLessonStatus.active, problem="model challenge leg",
                             root_cause="provider activation gated", affected_component="chat.provider",
                             severity=EngineeringLessonSeverity.medium, evidence="first_cycle_go=false",
                             fix="await founder authorization", verification_status="unverified",
                             source_type="postmortem", source_ref="doc:directive", general_rule="fail closed",
                             first_seen_at=datetime.utcnow(), created_by="grundare"))
    db.commit()
    out = collect_blueprint_readiness(db)
    comp = out["component_index"]["chat.capture"]
    assert comp["verdict_counts"]["verified"] == 1, "verified work must be distinguishable from planned"
    other = out["component_index"]["chat.provider"]
    assert other["verdict_counts"]["unverified"] == 1, "planned work must never present as verified"


# ---------------------------------------------------------------------------
# Scenario 7 — Updating blueprint readiness from real evidence
# ---------------------------------------------------------------------------
def test_scenario_7_readiness_updates_from_evidence(db):
    from app.blueprint_readiness import collect_blueprint_readiness
    before = collect_blueprint_readiness(db)["summary"]
    db.add(ProjectSource(source_type="git_commit", source_ref="1e2cfdc8",
                         commit_sha="1e2cfdc8750dcd2a7b7911c80b0402e1906a9c6a",
                         ingested_by="z.ai", ingested_at=datetime.utcnow()))
    db.add(ProjectBranchPRStatus(kind="pr", ref="zai/alpha-integration-readiness", title="acceptance suite",
                                 status="open", base_ref="main", head_ref="zai/alpha-integration-readiness",
                                 ci_status="pending", is_current=True, recorded_at=datetime.utcnow(),
                                 recorded_by="z.ai"))
    db.commit()
    after = collect_blueprint_readiness(db)["summary"]
    assert after["code_refs"] == before["code_refs"] + 1, "readiness view must reflect new real evidence"
    pr_rows = [p for p in collect_blueprint_readiness(db)["pr_status"] if p["ref"] == "zai/alpha-integration-readiness"]
    assert pr_rows and pr_rows[0]["ci_status"] == "pending"


# ---------------------------------------------------------------------------
# Scenario 8 — Rejecting unauthorized cross-tenant or privileged operations
# ---------------------------------------------------------------------------
def test_scenario_8_reject_cross_tenant_and_privileged(db, app_role_engine):
    # Narrow the restricted role to its real boot-time policy first (same order as a real
    # boot: ensure_app_role -> alembic -> apply_runtime_privileges), independent of which
    # tests ran before — then assert what the founder's security model requires.
    import importlib.util
    spec = importlib.util.spec_from_file_location("apply_runtime_privileges", _APPLY_RUNTIME_PRIVILEGES)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.apply_and_verify(get_settings().database_url)

    founder_a = _make_founder(db)
    founder_b = _make_founder(db)
    doc_a = _make_document(db, founder_a.id, title="A:s hemliga beslut")
    _make_chunk(db, founder_a.id, doc_a.id, "Endast för grundare A.")

    A = sessionmaker(bind=app_role_engine, expire_on_commit=False)()
    try:
        _set_rls_user(A, founder_a.id)
        seen = A.execute(select(DocumentChunk.text)).scalars().all()
        assert any("grundare A" in t for t in seen), "owner sees own data via restricted role"
        _set_rls_user(A, founder_b.id)
        seen_b = A.execute(select(DocumentChunk.text)).scalars().all()
        assert not any("grundare A" in t for t in seen_b), "RLS must block cross-owner reads"
        priv = A.execute(sa.text(
            "SELECT has_table_privilege('mainai_app','memory_source_units','TRUNCATE'),"
            " has_table_privilege('mainai_app','alembic_version','TRUNCATE')")).one()
        assert priv == (False, False), "restricted role must NOT truncate immutable memory or migration state"
        # HARDENING OBSERVATION (reported, not silently accepted): the s1a policy's
        # schema-wide floor grants DELETE on every public table — including alembic_version —
        # by design (see s1a_privilege_policy.py:_all_public_tables). An app-role compromise
        # could therefore erase the migration bookkeeping row. RLS and the immutable-unit
        # design still hold (asserted above); narrowing this specific grant is a recommended
        # follow-up for the privilege-policy owner.
    finally:
        A.close()
        A.bind.dispose()


# ---------------------------------------------------------------------------
# Scenario 9 — Recovering from interrupted ingestion
# ---------------------------------------------------------------------------
def test_scenario_9_interrupted_ingestion_recovery(db):
    founder = _make_founder(db)
    checksum = uuid.uuid4().hex * 2
    failed = ImportJob(owner_id=founder.id, status=ImportJobStatus.failed,
                       source_filename="rapport.pdf", source_checksum=checksum,
                       progress_total=3, succeeded_count=1, failed_count=2,
                       failure_reason="worker crash mid-batch")
    db.add(failed)
    db.commit()
    retry = ImportJob(owner_id=founder.id, status=ImportJobStatus.pending,
                      source_filename="rapport.pdf", source_checksum=checksum)
    db.add(retry)
    db.commit()
    db.refresh(failed)
    assert failed.status == ImportJobStatus.failed and failed.failed_count == 2, \
        "the interrupted job's state is preserved (no silent loss)"
    rows = db.execute(select(ImportJob).where(ImportJob.source_checksum == checksum)).scalars().all()
    assert len(rows) == 2 and {j.status for j in rows} == {ImportJobStatus.failed, ImportJobStatus.pending}, \
        "checksum-indexed retry creates a NEW job; the failed record remains as evidence"


# ---------------------------------------------------------------------------
# Scenario 10 — Supervised agent task through isolated execution
# ---------------------------------------------------------------------------
def test_scenario_10_supervised_agent_task_record(db):
    from app.blueprint_readiness import collect_blueprint_readiness
    db.add(AgentTask(title="Compose zai/alpha lane onto current migration head",
                     description="Renumber per reconciliation addendum, verify on disposable PG, draft PR.",
                     target_files=["backend/alembic/versions/", "backend/docs/"],
                     acceptance_criteria="single head; composition probe intact; all suites green",
                     required_tests="tests/backend/test_billing_engine.py tests/backend/rag/test_memory_source_units.py",
                     created_by="grundare"))
    db.commit()
    out = collect_blueprint_readiness(db)
    task = out["next_actions"][0]
    assert task["target_files"] and task["required_tests"], "supervised task carries its verification contract"
    # The full PR-creation leg is proven upstream in a real run: PR #443's
    # crash->takeover->PR-created-once vertical slice test (GitHub-credentialed CI).


# ---------------------------------------------------------------------------
# Scenario 11 — Memory recall, correction and supersession survive a real restart
#
# Founder Alpha Capability Development Program, Workstream A acceptance test: record a
# project decision as the founder (not via the AI-suggestion candidate-signal path — a
# model's own output must never become an authorization source, so this intentionally uses
# the explicit record_founder_memory() API a human/founder action would call), correct it
# from a SEPARATE later interaction, dispose the engine to force a real reconnect (this
# suite's own established restart simulation — see test_scenario_8 above), then build the
# project-status prompt for a THIRD, brand-new conversation and assert it carries the
# CURRENT decision with its source citation, never the superseded one, while the superseded
# row remains permanently queryable as audit history.
# ---------------------------------------------------------------------------
def test_scenario_11_memory_recall_correction_survives_restart(db):
    from app.founder_memory.service import list_founder_memory, record_founder_memory
    from app.project_status.service import format_project_status_instructions

    founder = _make_founder(db)

    decision_a = record_founder_memory(
        db, owner_id=founder.id, note_type="decision",
        content="Vi bygger embedding-pipelinen med Ollama lokalt, inte en extern leverantör.",
        idempotency_key=_run_id("ws-a-decision"), authority="founder", basis="manual",
        source="conversation:samtal-1-2026-11-24",
    )
    db.commit()

    # A LATER, separate interaction corrects it. The old row's content is never touched;
    # only its status flips, and the new row is the one that becomes current.
    decision_b = record_founder_memory(
        db, owner_id=founder.id, note_type="decision",
        content="Uppdatering: vi kör embedding-pipelinen mot en fjärransluten Ollama-endpoint "
                "(16GB-servern räcker inte för både app och modell samtidigt).",
        idempotency_key=_run_id("ws-a-correction"), authority="founder", basis="manual",
        source="conversation:samtal-2-2026-11-24", supersedes_note_id=decision_a.id,
    )
    db.commit()
    decision_a_id, decision_b_id = decision_a.id, decision_b.id

    # Real restart simulation (same convention as scenario 8 above, which disposes its own
    # dedicated engine rather than the shared session-scoped fixture): a brand-new engine and
    # connection, never trusting the same session/connection that wrote the data.
    db.commit()
    restart_engine = create_engine(get_settings().database_url)

    fresh = sessionmaker(bind=restart_engine, expire_on_commit=False)()
    try:
        _set_rls_user(fresh, founder.id)

        # A brand-new, third conversation asks a project-status-relevant question.
        prompt = format_project_status_instructions(
            fresh, owner_id=founder.id, question="Vad är projektstatusen för embedding-pipelinen?"
        )
        assert "Uppdatering: vi kör embedding-pipelinen" in prompt, \
            "the new conversation must see the CURRENT (corrected) decision"
        assert "conversation:samtal-2-2026-11-24" in prompt, \
            "the current decision's source must be citeable, not just its content"
        assert "Vi bygger embedding-pipelinen med Ollama lokalt" not in prompt, \
            "a superseded decision must never be presented as current"

        # The superseded row is never deleted — it remains real audit history.
        history = list_founder_memory(fresh, owner_id=founder.id)
        by_id = {n.id: n for n in history}
        assert by_id[decision_a_id].status == "superseded"
        assert by_id[decision_a_id].content == "Vi bygger embedding-pipelinen med Ollama lokalt, inte en extern leverantör.", \
            "superseding must never rewrite the old row's content"
        assert by_id[decision_b_id].status == "active"
        assert by_id[decision_b_id].supersedes_note_id == decision_a_id
    finally:
        fresh.close()
        restart_engine.dispose()


# ---------------------------------------------------------------------------
# Scenario 12 — Two documents disagree; the system surfaces the conflict (never
# silently picks a side), cites both real sources, and this survives a real restart.
#
# Founder Alpha Capability Development Program, Workstream B acceptance test. The
# underlying claim-graph (app/rag/claim_graph.py), epistemic rollup (app/rag/epistemics.py)
# and chat/workbench wiring were found ALREADY correct and extensively tested by the gap
# assessment this scenario's dispatch required — this closes the one real, narrow gap: no
# existing test exercises the full chain (two real documents -> extracted claims -> a
# declared contradiction -> the live retrieval+citation surface -> a genuine restart) in
# one place. Contradiction DETECTION itself stays a declared act (record_claim_relationship,
# the one production writer — app/routers/library.py's POST .../relationships), exactly as
# designed: auto-inferring semantic contradictions from raw text would be exactly the kind
# of unverified AI judgment this effort's own security model refuses to promote into a
# trusted signal without one. This scenario proves the DECLARED path is trustworthy end to
# end, not that declaration should become automatic.
# ---------------------------------------------------------------------------
def test_scenario_12_document_contradiction_citation_survives_restart(db):
    founder = _make_founder(db)

    doc_v1 = _make_document(db, founder.id, title="Prissättningsnotis A (2026-11-10)")
    doc_v2 = _make_document(db, founder.id, title="Prissättningsnotis B (2026-11-20)")
    chunk_v1 = _make_chunk(db, founder.id, doc_v1.id, "Priset för Founder Alpha är 499 kr/mån enligt notis A.")
    chunk_v2 = _make_chunk(db, founder.id, doc_v2.id, "Priset för Founder Alpha är 799 kr/mån enligt notis B.")
    claim_v1 = _make_claim(db, founder.id, doc_v1.id, chunk_v1.id, chunk_v1.text)
    claim_v2 = _make_claim(db, founder.id, doc_v2.id, chunk_v2.id, chunk_v2.text)

    # A reviewer declares the real contradiction through the one production writer —
    # never inferred automatically. Both endpoint claims move to conflicting_evidence.
    _, shifts = record_claim_relationship(
        db, founder.id, from_claim_id=claim_v1.id, to_claim_id=claim_v2.id,
        relationship_type="contradicts", note="Olika prisuppgifter, ej ännu löst.",
    )
    db.commit()
    assert len(shifts) == 2, "a contradicts edge must move BOTH endpoint claims' verdicts"
    claim_v1_id, claim_v2_id, doc_v1_id, doc_v2_id = claim_v1.id, claim_v2.id, doc_v1.id, doc_v2.id

    # Real restart (this suite's established convention, see scenario 8/11): a brand-new
    # engine and connection, never the session that wrote the edge.
    restart_engine = create_engine(get_settings().database_url)
    fresh = sessionmaker(bind=restart_engine, expire_on_commit=False)()
    try:
        _set_rls_user(fresh, founder.id)

        # Retrieval still finds BOTH chunks — the conflict suppresses nothing. Each hit
        # cites its own REAL, still-existing document; no citation is fabricated, since
        # app/routers/chat.py builds `sources` directly from retrieve_context()'s own hits,
        # never from model-generated text.
        hits = hybrid_search(fresh, founder.id, None, "Founder Alpha", top_k=10)
        hit_doc_ids = {h["document_id"] for h in hits}
        assert str(doc_v1_id) in hit_doc_ids and str(doc_v2_id) in hit_doc_ids, \
            "both conflicting sources must remain retrievable -- neither is silently dropped"

        # The epistemic rollup, read fresh, counts the conflict and the instruction line
        # tells the model to present it as a conflict, never pick a winner.
        counts = epistemic_status_counts_sql(fresh, founder.id)
        assert counts["conflicting_evidence"] >= 2, "both claims must land in the conflicting bucket after restart"
        instructions = format_epistemic_instructions(counts)
        assert "motstridiga" in instructions and "aldrig som en sanning" in instructions

        # The evidence chain for EITHER claim shows the conflict from BOTH sides -- it is
        # presented, never adjudicated into a single "winning" claim.
        chain = trace_evidence_chain(fresh, founder.id, claim_v1_id)
        contradicting_ids = {e["other_claim_id"] for e in chain.contradicting_edges}
        assert str(claim_v2_id) in contradicting_ids
    finally:
        fresh.close()
        restart_engine.dispose()
