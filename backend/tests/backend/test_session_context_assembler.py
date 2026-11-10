"""Session-start context assembler — executable acceptance tests against the 8 criteria in
docs/LIFE_SESSION_CONTEXT_ASSEMBLY_DESIGN_2026-11-09.md, plus the token budget and the real
application (API) boundary. Real PostgreSQL, no provider. Reuses existing canonical sources; the
assembler writes nothing.
"""

import uuid

from sqlalchemy import func, select

from app import session_context as sc
from app.founder_memory.service import record_founder_memory
from app.mainai_executive.continuity import (
    load_continuity_checkpoint,
    resume_summary,
    save_continuity_checkpoint,
)
from app.mainai_executive.types import ContinuityCheckpoint, ExecutivePhase
from app.models.founder_memory import FounderMemoryNote
from app.request_context import current_user_id as current_user_id_var


def _rls(db, owner_id):
    from sqlalchemy import text as sa_text

    current_user_id_var.set(str(owner_id))
    db.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _assemble(db, owner_id, **kw):
    return sc.assemble_session_context(db, owner_id=owner_id, session_id=kw.pop("session_id", "s1"), **kw)


# --- Criterion 1: source-grounded ------------------------------------------------------------

def test_c1_every_item_names_its_source_module(db_session, make_verified_user):
    user, _ = make_verified_user()
    _rls(db_session, user.id)
    record_founder_memory(db_session, owner_id=user.id, note_type="decision",
                          content="Use Postgres", idempotency_key=f"k-{uuid.uuid4()}", authority="founder")
    db_session.commit()
    bundle = _assemble(db_session, user.id)
    for section in bundle.sections:
        for item in section.items:
            assert "source_module" in item, f"item in {section.name} lacks a source ref: {item}"


# --- Criterion 2: active-only founder memory -------------------------------------------------

def test_c2_superseded_founder_memory_never_appears(db_session, make_verified_user):
    user, _ = make_verified_user()
    _rls(db_session, user.id)
    old = record_founder_memory(db_session, owner_id=user.id, note_type="preference",
                                content="prefers tabs", idempotency_key=f"k-{uuid.uuid4()}", authority="founder")
    new = record_founder_memory(db_session, owner_id=user.id, note_type="preference",
                                content="prefers spaces", idempotency_key=f"k-{uuid.uuid4()}",
                                authority="founder", supersedes_note_id=old.id)
    db_session.commit()
    bundle = _assemble(db_session, user.id)
    fm = bundle.section("founder_memory")
    ids = {i["source_ref"] for i in fm.items}
    assert str(new.id) in ids
    assert str(old.id) not in ids  # superseded -> current truth only


# --- Criterion 3: truth-aware -> BLOCKED -----------------------------------------------------

def test_c3_an_open_truth_claim_violation_blocks_the_bundle(db_session, make_verified_user, monkeypatch):
    user, _ = make_verified_user()
    _rls(db_session, user.id)

    class _V:
        id = uuid.uuid4()
        target_kind = "founder_memory_note"
        target_id = uuid.uuid4()
        claimed_state = "verified"

    monkeypatch.setattr("app.inspectable_memory.service.list_truth_claim_violations",
                        lambda db, *, owner_id: [_V()])
    bundle = _assemble(db_session, user.id)
    assert bundle.blocked is True
    assert bundle.readiness == sc.READINESS_BLOCKED
    assert any(r.startswith("unresolved_truth_claim_violations") for r in bundle.blocked_reasons)
    assert bundle.section("truth_claim_violations").items  # surfaced, not hidden


# --- Criterion 4: continuity reuse (no parallel store) ---------------------------------------

def test_c4_continuity_section_equals_resume_summary_of_the_saved_checkpoint(db_session, make_verified_user):
    user, _ = make_verified_user()
    _rls(db_session, user.id)
    cp = ContinuityCheckpoint(
        session_id="sess-xyz", phase=ExecutivePhase.PLAN, founder_request="ship the guard",
        context_set_id=None, note_id=None, source_entity_id=None, work_candidate_ids=[],
        lesson_ids=[], horizon_items=[], staffing_action=None, workforce_request_id=None,
        completed=["wrote guard"], uncertain=[], remaining=["add tests"],
        authority_still_valid=True, authority_notes=[],
    )
    save_continuity_checkpoint(db_session, owner_id=user.id, checkpoint=cp)
    db_session.commit()

    bundle = _assemble(db_session, user.id, session_id="sess-xyz")
    section = bundle.section("continuity")
    assert section.items, "a saved checkpoint should populate the continuity section"
    expected = resume_summary(load_continuity_checkpoint(db_session, owner_id=user.id, session_id="sess-xyz"))
    assert section.items[0]["resume"] == expected  # reuses continuity's own summary, no parallel store


# --- Criterion 5: no new store (writes nothing) ----------------------------------------------

def test_c5_assembling_writes_nothing(db_session, make_verified_user):
    user, _ = make_verified_user()
    _rls(db_session, user.id)
    record_founder_memory(db_session, owner_id=user.id, note_type="decision", content="x",
                          idempotency_key=f"k-{uuid.uuid4()}", authority="founder")
    db_session.commit()
    before = db_session.execute(select(func.count()).select_from(FounderMemoryNote)).scalar()
    _assemble(db_session, user.id)
    _assemble(db_session, user.id)
    after = db_session.execute(select(func.count()).select_from(FounderMemoryNote)).scalar()
    assert before == after  # read-only: no rows created


# --- Criterion 6: owner isolation ------------------------------------------------------------

def test_c6_another_owners_memory_never_leaks(db_session, make_verified_user):
    a, _ = make_verified_user()
    b, _ = make_verified_user()
    _rls(db_session, a.id)
    secret = record_founder_memory(db_session, owner_id=a.id, note_type="decision",
                                   content="A-only secret", idempotency_key=f"k-{uuid.uuid4()}", authority="founder")
    db_session.commit()
    _rls(db_session, b.id)
    bundle_b = _assemble(db_session, b.id)
    ids_b = {i.get("source_ref") for i in bundle_b.section("founder_memory").items}
    assert str(secret.id) not in ids_b


# --- Criterion 7: fail closed on a failing source --------------------------------------------

def test_c7_a_failing_critical_source_blocks_and_names_itself(db_session, make_verified_user, monkeypatch):
    user, _ = make_verified_user()
    _rls(db_session, user.id)

    def _boom(db, *, owner_id):
        raise RuntimeError("capability store unavailable")

    monkeypatch.setattr("app.capability_reality.service.list_capability_gaps", _boom)
    bundle = _assemble(db_session, user.id)
    assert bundle.blocked is True
    assert bundle.readiness == sc.READINESS_BLOCKED
    assert "source_unavailable:capability_reality" in bundle.blocked_reasons
    assert bundle.section("capability_gaps").error is not None  # named, not a silent partial


# --- Criterion 8: deterministic ordering -----------------------------------------------------

def test_c8_section_order_is_fixed(db_session, make_verified_user):
    user, _ = make_verified_user()
    _rls(db_session, user.id)
    names = [s.name for s in _assemble(db_session, user.id).sections]
    assert names == [
        "startup_readiness", "continuity", "active_context", "founder_memory",
        "truth_claim_violations", "capability_gaps", "active_assignments", "project_status",
    ]


# --- Requirement 6: token budget never drops safety sections ---------------------------------

def test_token_budget_trims_nonsafety_sections_but_keeps_safety(db_session, make_verified_user):
    user, _ = make_verified_user()
    _rls(db_session, user.id)
    for _ in range(25):
        record_founder_memory(db_session, owner_id=user.id, note_type="observation",
                              content="x" * 200, idempotency_key=f"k-{uuid.uuid4()}", authority="founder")
    db_session.commit()
    bundle = _assemble(db_session, user.id, token_budget=200)
    assert bundle.truncated is True
    # Safety sections are always present (never dropped to save tokens).
    assert bundle.section("startup_readiness").items
    assert bundle.section("truth_claim_violations") is not None
    # Trimmed section is smaller than the full set.
    assert len(bundle.section("founder_memory").items) < 25


# --- Requirement 10: proven at the actual application (API) boundary --------------------------

def test_session_context_api_boundary(client, superuser_db):
    from app.models.user import User

    founder = superuser_db.query(User).filter_by(email="founder@lifeos.local").first()
    _rls(superuser_db, founder.id)
    record_founder_memory(superuser_db, owner_id=founder.id, note_type="decision",
                          content="API boundary note", idempotency_key=f"k-{uuid.uuid4()}", authority="founder")
    superuser_db.commit()

    login = client.post("/api/auth/login", json={"email": "founder@lifeos.local", "password": "TestFounderPassword123!"})
    assert login.status_code == 200, login.text
    csrf = login.json()["csrf_token"]

    res = client.get("/api/session/context", params={"session_id": "api-s1"}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["owner_id"] == str(founder.id)  # owner-scoped at the boundary
    assert [s["name"] for s in body["sections"]][0] == "startup_readiness"
    assert "blocked" in body and "readiness" in body


def test_session_context_api_requires_authentication(client):
    res = client.get("/api/session/context")
    assert res.status_code in (401, 403)  # no silent anonymous session context
