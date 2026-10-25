"""Independent-completion acceptance for the eventual REAL provider cycle (examiner-owned).

Subject: #309 aff5de1a. Property: a provider's self-reported FINISHED is NOT independent
verification. A completion may be certified only when a NON-builder examiner records an
independent PASS, bound to one exact candidate SHA, with evidence — and that record cannot be
forged by the ordinary MainAI runtime. The provider cycle must eventually CONSULT that registry
before treating a completion as verified / continuing.

Surface driven (real product code):
- app.mainai_verification_registry.service (record_verification_attestation / find_independent_pass)
- model MainAIVerificationRecord + its DB guard (migration 0080): append-only, and session_user
  'mainai_app' (ordinary runtime) cannot INSERT — only a privileged (superuser) writer can.
- app.agent_coordination.provider_ingestion (the provider completion path).

Classification, test-first:
- PASS  = the registry really enforces this today (executable now).
- xfail(strict) = required integration the provider cycle does NOT yet do (fails now; XPASS if fixed).
- skip (BLOCKED) = needs a real authenticated round trip / Founder live key.

No fixture is ever reported as a vendor/independent PASS. first_cycle_go=false.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text as sa_text

from _guards import egress  # noqa: F401
from _harness import _privileges, owner_id  # noqa: F401

SECRET = "whsec_examiner_icv"
UA = "Cursor-Agent-Webhook/1.0"
SHA = "a" * 40
BUILDER = "cursor:ext_build_1"
EXAMINER = "examiner:independent_1"


def _attest(db, **kw):
    """Call the real attestation API with sensible defaults; caller supplies the db session
    (db_session = ordinary runtime, superuser_db = privileged writer)."""
    from app.mainai_verification_registry.service import record_verification_attestation
    base = dict(component_id="provider-cycle", candidate_sha=SHA, builder_identity=BUILDER,
                examiner_identity=EXAMINER, review_result="PASS",
                evidence_summary="independent rerun green")
    base.update(kw)
    return record_verification_attestation(db, **base)


def _attest_priv(superuser_db, **kw):
    """Record via the privileged writer (an independent examiner path) and commit so the
    ordinary runtime session can read it."""
    rec = _attest(superuser_db, **kw)
    rid = rec.id
    superuser_db.commit()
    return rid


def _find(db, owner=None, builder=BUILDER, sha=SHA):
    from app.mainai_verification_registry.service import find_independent_pass
    return find_independent_pass(db, component_id="provider-cycle", candidate_sha=sha,
                                 builder_identity=builder, owner_id=owner)


def _sig(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _body(status="FINISHED", agent_id="ext_icv", ts=None) -> bytes:
    p = {"event": "statusChange", "status": status, "id": agent_id}
    if ts:
        p["timestamp"] = ts
    return json.dumps(p).encode()


def _svc_bound(owner, recipient="cursor", agent_id="ext_icv"):
    from app.agent_coordination.durable import DurableCoordinationService
    from app.agent_coordination.provider_ingestion import bind_provider_assignment
    svc = DurableCoordinationService()
    aid = "asg_" + uuid.uuid4().hex[:8]
    svc.assign(agent_identity=recipient, workstream="build", owner_id=owner,
               instruction_id="i1", assignment_id=aid, actor="founder")
    bind_provider_assignment(svc, owner_id=owner, assignment_id=aid, provider="cursor",
                             external_agent_id=agent_id, recipient=recipient)
    return svc, aid


def _ingest(svc, owner, status="FINISHED", agent_id="ext_icv"):
    from app.agent_coordination.provider_ingestion import ingest_authenticated_provider_event
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    body = _body(status, agent_id=agent_id, ts=ts)
    return ingest_authenticated_provider_event(svc, owner_id=owner, secret=SECRET, raw_body=body,
                                               signature=_sig(body), user_agent=UA, fixture=True)


# ---------------------------------------------------------------------------
# A. Attestation API enforces independence + evidence + exact SHA (validation; PASS)
# ---------------------------------------------------------------------------

def test_ICV_A1_builder_self_certification_rejected(db_session):
    from app.mainai_verification_registry.service import VerificationRegistryError
    with pytest.raises(VerificationRegistryError):
        _attest(db_session, examiner_identity=BUILDER)  # examiner == builder, raises before DB


def test_ICV_A2_evidence_required(db_session):
    from app.mainai_verification_registry.service import VerificationRegistryError
    with pytest.raises(VerificationRegistryError):
        _attest(db_session, evidence_summary="   ")


def test_ICV_A3_candidate_sha_must_be_exact(db_session):
    from app.mainai_verification_registry.service import VerificationRegistryError
    for bad in ("", "abc", "A" * 40, "a" * 39, "g" * 40):
        with pytest.raises(VerificationRegistryError):
            _attest(db_session, candidate_sha=bad)


def test_ICV_A4_review_result_constrained(db_session):
    from app.mainai_verification_registry.service import VerificationRegistryError
    with pytest.raises(VerificationRegistryError):
        _attest(db_session, review_result="GREEN")  # raises before DB


# ---------------------------------------------------------------------------
# B. DB-level guard: ordinary runtime cannot forge a record; append-only (PASS, strong control)
# ---------------------------------------------------------------------------

def test_ICV_B1_ordinary_runtime_cannot_insert_verification_record(db_session):
    """session_user 'mainai_app' is blocked by trg_mainai_verification_records_guard: the
    examined runtime cannot manufacture its own independent-verification pass."""
    from sqlalchemy.exc import InternalError, ProgrammingError
    with pytest.raises((InternalError, ProgrammingError)) as ei:
        _attest(db_session)  # valid record -> reaches INSERT -> trigger raises
        db_session.flush()
    assert "cannot create verification records" in str(ei.value)
    db_session.rollback()


def test_ICV_B2_records_are_append_only(superuser_db):
    """Even the privileged writer cannot UPDATE/DELETE a verification record (append-only)."""
    from sqlalchemy.exc import InternalError, ProgrammingError
    rid = _attest_priv(superuser_db)
    with pytest.raises((InternalError, ProgrammingError)) as ei:
        superuser_db.execute(sa_text(
            "UPDATE mainai_verification_records SET review_result='FAIL' WHERE verification_id=:i"),
            {"i": str(rid)})
        superuser_db.flush()
    assert "append-only" in str(ei.value)
    superuser_db.rollback()


# ---------------------------------------------------------------------------
# C. find_independent_pass selection logic (privileged write, ordinary read; PASS)
# ---------------------------------------------------------------------------

def test_ICV_C1_independent_pass_found_only_from_nonbuilder(db_session, superuser_db):
    assert _find(db_session) is None, "no attestation yet -> not certified"
    _attest_priv(superuser_db)  # independent examiner PASS
    rec = _find(db_session)
    assert rec is not None and rec.examiner_identity == EXAMINER and rec.builder_identity == BUILDER


def test_ICV_C2_invalidated_pass_is_excluded(db_session, superuser_db):
    original = _attest_priv(superuser_db)
    assert _find(db_session) is not None
    _attest_priv(superuser_db, examiner_identity="examiner:independent_2", review_result="FAIL",
                 evidence_summary="re-review found a regression", invalidates_verification_id=original)
    assert _find(db_session) is None, "invalidated independent pass must not certify"


def test_ICV_C3_owner_scoping(db_session, superuser_db, owner_id, make_verified_user):
    other, _ = make_verified_user()
    _attest_priv(superuser_db, owner_id=owner_id)
    assert _find(db_session, owner=owner_id) is not None   # this owner sees it
    assert _find(db_session, owner=other.id) is None       # a different owner does not
    assert _find(db_session, owner=None) is None           # global query doesn't see an owner row


# ---------------------------------------------------------------------------
# D. Provider-reported != independently-verified (executable PASS)
# ---------------------------------------------------------------------------

def test_ICV_D1_provider_finished_creates_no_independent_pass(db_session, owner_id):
    """A real provider FINISHED ingestion completes the ledger obligation but writes NO
    verification attestation — find_independent_pass stays None. Provider-reported is not
    independent verification at the registry level."""
    owner = str(owner_id)
    svc, aid = _svc_bound(owner)
    r = _ingest(svc, owner, status="FINISHED")
    assert r.get("transition") == "completed" and r.get("independently_verified") is False
    assert _find(db_session, owner=owner_id, builder="cursor:ext_icv") is None


# ---------------------------------------------------------------------------
# E. Required integration the provider cycle does NOT yet do (xfail strict)
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="ICV-WIRING: the provider cycle does not consult "
                   "mainai_verification_registry.find_independent_pass before treating a "
                   "completion as verified / eligible for continuation")
def test_ICV_E1_provider_cycle_consults_registry(db_session, owner_id):
    """Required: on a provider FINISHED with NO independent pass recorded, the ingestion result
    must flag that continuation is gated on independent verification. #309 does not import or
    consult the registry from the provider cycle at all."""
    import app.agent_coordination.provider_ingestion as pi
    owner = str(owner_id)
    svc, aid = _svc_bound(owner)
    r = _ingest(svc, owner, status="FINISHED")
    wired = hasattr(pi, "find_independent_pass") or r.get("requires_independent_verification") is True
    assert wired, "provider completion is not gated on an independent-verification record"


# ---------------------------------------------------------------------------
# F. BLOCKED on a real authenticated round trip
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="BLOCKED: independent verification of REAL authenticated provider "
                         "evidence (non-fixture) recorded as a PASS that gates the real cycle "
                         "needs a Founder-authorized live Cursor Cloud key + a real round trip + "
                         "PI-STOP wiring. No fixture may be recorded as an independent/vendor PASS.")
def test_ICV_F1_real_independent_completion():  # pragma: no cover
    ...
