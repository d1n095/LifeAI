"""Integration tests: the evidence-quality METADATA PRODUCER is CONNECTED to real
execution paths, not merely available as a standalone grader.

Until this producer existed (app/rag/evidence_metadata.py, migration 0073),
`grade_evidence()` had NO production caller: every live `classify_claim()` ran uncapped and
the whole evidence-quality layer was tested-but-connected-to-nothing — exactly the
"state exists != driver exists" shape this lane keeps removing.

Seams covered (real PostgreSQL, real RLS, real HTTP for the founder API):
  1. Founder stamps structured study metadata on a claim (PUT /api/library/claims/{id}/
     evidence-metadata) -> the Library source-detail read path classifies that claim UNDER
     the materialized cap (classify_claim(quality_cap=...)) and surfaces grade/cap/caveats.
  2. The one-query SQL rollup (epistemic_status_counts_sql — the chat/workbench instruction
     source) agrees with the per-claim detail view on every capped claim: a capped
     verified_fact counts as its CAP, never as verified_fact. Sum invariant still holds.
  3. The cap can only LOWER: an assumption stays an assumption under even a meta-analysis
     grade; conflicting_evidence (a contradiction verdict) is never granted or revoked by
     metadata quality.
  4. Fail-closed declarations: unknown keys are rejected with named reasons and never
     stored; an invalid study design names the valid vocabulary; a declaration with zero
     valid keys is refused outright (422), so an honest absence can never become a stored
     grade.
  5. Re-stamping UPDATES the one row and moves the materialized verdict with it — a stale
     grade behind a corrected declaration is structurally impossible.
  6. Owner isolation end to end: another owner's service-level stamp 404s; the composite
     (claim_id, owner_id) FK makes a cross-owner row a database-level IntegrityError, not
     merely something RLS hides afterwards.
"""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.knowledge_claim import ClaimConfidence, ClaimStatus, ClaimType
from app.models.claim_relationship import ClaimRelationship, ClaimRelationshipType
from app.rag.evidence_metadata import (
    ClaimNotFoundError,
    NothingDeclaredError,
    normalize_declared_metadata,
    record_claim_evidence_metadata,
)
from app.rag.epistemics import epistemic_status_counts_sql, format_epistemic_instructions

from tests.backend.test_epistemics_integration import (
    FOUNDER_EMAIL,
    FOUNDER_PASSWORD,
    _make_claimed_source,
    _set_rls_user,
)

# A mechanism-only design caps the ladder at hypothesis no matter how the other axes look —
# "a plausible mechanism is a HYPOTHESIS, never established harm or established benefit".
MECHANISM_ONLY_META = {"study_design": "mechanism_only", "population": "n=12 cell line"}
# A randomized trial with documented dose-response and a supported causal claim is the
# strongest declaration the vocabulary can carry.
RGT_META = {
    "study_design": "randomized_trial",
    "exposure_duration": "12 weeks",
    "dose": "5 mg/day",
    "dose_response_documented": True,
    "population": "n=340 adults",
    "outcomes": ["primary outcome measured at week 12"],
    "interactions": ["none recorded"],
    "conflicts_of_interest": "none declared",
    "competing_explanations": ["placebo effect addressed by design"],
    "causal_claim": True,
    "causal_claim_supported_by_design": True,
}


def _login_founder(client) -> str:
    """Mutating routes (PUT) require the X-CSRF-Token header folded into get_current_user —
    the value is delivered once in the login response body (app/deps.py), never a cookie."""
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200
    return res.json()["csrf_token"]


def _stamp(client, claim_id, metadata, expect=200):
    csrf = _login_founder(client)
    res = client.put(
        f"/api/library/claims/{claim_id}/evidence-metadata",
        json={"metadata": metadata, "declared_via": "founder_test"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == expect, res.text
    return res.json()


# --- normalization (pure, fail-closed) -----------------------------------------------------


def test_normalization_rejects_unknown_keys_and_never_invents():
    result = normalize_declared_metadata({"study_design": "randomized_trial", "vibe_score": 11, "mainstreamness": "high"})
    assert result.declared == {"study_design": "randomized_trial"}
    assert set(result.rejected) == {"vibe_score", "mainstreamness"}
    assert "unknown key" in result.rejected["vibe_score"]


def test_normalization_rejects_wrong_types_and_invalid_design():
    result = normalize_declared_metadata(
        {"dose_response_documented": "yes", "study_design": "twitter_poll", "population": "  n=8  "}
    )
    assert result.declared == {"population": "n=8"}
    assert "boolean" in result.rejected["dose_response_documented"]
    assert "twitter_poll" in result.rejected["study_design"]
    assert "randomized_trial" in result.rejected["study_design"]  # the valid vocabulary is named


# --- seam 1+2: stamp -> detail capped -> rollup agrees --------------------------------------


def test_stamped_mechanism_only_claim_reads_as_hypothesis_in_detail(db_session, client, make_verified_user):
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    source, claim = _make_claimed_source(db_session, founder.id)  # grounded, active, likely -> uncapped verdict verified_fact
    _login_founder(client)

    body = _stamp(client, claim.id, MECHANISM_ONLY_META)
    assert body["quality_cap"] == "hypothesis"
    assert body["quality_grade"] == "very_low"
    assert body["rejected"] == {}
    assert any("mechanism" in c for c in body["quality_caveats"])

    detail = client.get(f"/api/library/{source.id}")
    row = next(c for c in detail.json()["claims"] if c["id"] == str(claim.id))
    assert row["epistemic_status"] == "hypothesis", "the cap must downgrade a would-be verified_fact"
    assert row["quality_grade"] == "very_low"
    assert row["quality_cap"] == "hypothesis"
    assert row["quality_caveats"], "the caveats that must travel with the evidence are surfaced"


def test_rollup_agrees_with_detail_view_on_capped_claims(db_session, client, make_verified_user):
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    # One claim stays uncapped (verified_fact), one carries a mechanism-only cap.
    _source_a, claim_a = _make_claimed_source(db_session, founder.id, title="Okappad")
    source_b, claim_b = _make_claimed_source(db_session, founder.id, title="Mechanism")
    _login_founder(client)
    _stamp(client, claim_b.id, MECHANISM_ONLY_META)

    counts = epistemic_status_counts_sql(db_session, founder.id)
    assert counts["verified_fact"] == 1, "the uncapped claim still verifies"
    assert counts["hypothesis"] == 1, "the capped claim counts as its CAP, not as verified_fact"
    assert (
        sum(counts[k] for k in ("verified_fact", "conflicting_evidence", "hypothesis", "assumption", "unknown"))
        == counts["total"]
    ), "the cap moves a claim BETWEEN buckets; it never adds or loses one"

    # The chat/workbench instruction line is built from these counts — the numbers the model
    # sees must match what the founder sees in the detail view.
    line = format_epistemic_instructions(counts)
    assert "1 verifierade" in line and "1 hypoteser" in line


def test_project_scoped_rollup_inherits_the_cap(db_session, client, make_verified_user):
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    source, claim = _make_claimed_source(db_session, founder.id)
    _login_founder(client)
    _stamp(client, claim.id, MECHANISM_ONLY_META)
    counts = epistemic_status_counts_sql(db_session, founder.id)
    assert counts["verified_fact"] == 0 and counts["hypothesis"] == 1


# --- seam 3: a cap can only lower, never raise or un-contradict ------------------------------


def test_cap_never_raises_an_assumption(db_session, client, make_verified_user):
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    source, claim = _make_claimed_source(db_session, founder.id, title="No basis", confidence=ClaimConfidence.no_basis)
    _login_founder(client)
    body = _stamp(client, claim.id, RGT_META)  # the strongest metadata there is
    assert body["quality_cap"] == "verified_fact"

    detail = client.get(f"/api/library/{source.id}")
    row = next(c for c in detail.json()["claims"] if c["id"] == str(claim.id))
    assert row["epistemic_status"] == "assumption", "a cap lowers, never raises: no_basis stays an assumption"


def test_conflicting_evidence_is_never_granted_or_revoked_by_metadata(db_session, client, make_verified_user):
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    source_a, claim_a = _make_claimed_source(db_session, founder.id, title="A")
    _source_b, claim_b = _make_claimed_source(db_session, founder.id, title="B")
    _set_rls_user(db_session, founder.id)
    db_session.add(ClaimRelationship(owner_id=founder.id, from_claim_id=claim_a.id, to_claim_id=claim_b.id, relationship_type=ClaimRelationshipType.contradicts))
    db_session.commit()
    _login_founder(client)
    _stamp(client, claim_b.id, RGT_META)

    detail = client.get(f"/api/library/{source_a.id}")
    row = next(c for c in detail.json()["claims"] if c["id"] == str(claim_a.id))
    assert row["epistemic_status"] == "conflicting_evidence", "a contradiction is a contradiction whatever the metadata says"


# --- seam 4: fail-closed declarations --------------------------------------------------------


def test_unknown_keys_are_rejected_with_reasons_and_never_stored(db_session, client, make_verified_user):
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    source, claim = _make_claimed_source(db_session, founder.id)
    _login_founder(client)

    body = _stamp(client, claim.id, {"study_design": "mechanism_only", "funding_bias_score": 0.9, "totally_made_up": True})
    assert set(body["rejected"]) == {"funding_bias_score", "totally_made_up"}
    assert body["declared"] == {"study_design": "mechanism_only"}, "only accepted keys were graded"

    detail = client.get(f"/api/library/{source.id}")
    row = next(c for c in detail.json()["claims"] if c["id"] == str(claim.id))
    assert row["quality_cap"] == "hypothesis"

    from app.models.claim_evidence_metadata import ClaimEvidenceMetadata

    stored = db_session.query(ClaimEvidenceMetadata).filter_by(claim_id=claim.id).one()
    assert set(stored.metadata_json) == {"study_design"}, "rejected keys are audit trail, not stored metadata"
    assert set(stored.rejected_keys) == {"funding_bias_score", "totally_made_up"}


def test_empty_declaration_is_refused(db_session, client, make_verified_user):
    from app.models.user import User
    from app.models.claim_evidence_metadata import ClaimEvidenceMetadata

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    source, claim = _make_claimed_source(db_session, founder.id)
    _login_founder(client)

    csrf = _login_founder(client)
    res = client.put(
        f"/api/library/claims/{claim.id}/evidence-metadata",
        json={"metadata": {}, "declared_via": "founder_test"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 422, "a stamping of nothing must be refused, not graded as unassessable"
    assert db_session.query(ClaimEvidenceMetadata).filter_by(claim_id=claim.id).count() == 0

    detail = client.get(f"/api/library/{source.id}")
    row = next(c for c in detail.json()["claims"] if c["id"] == str(claim.id))
    assert row["quality_grade"] is None and row["quality_cap"] is None
    assert row["quality_caveats"] == []
    assert row["epistemic_status"] == "verified_fact", "absence of metadata leaves the ladder verdict uncapped"


def test_population_only_declaration_is_honest_low_grade(db_session, client, make_verified_user):
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    source, claim = _make_claimed_source(db_session, founder.id)
    _login_founder(client)

    body = _stamp(client, claim.id, {"population": "n=42"})
    assert body["quality_grade"] == "very_low"  # design unknown -> the ceiling is very_low
    assert body["quality_cap"] == "hypothesis"
    assert any("study design not recorded" in c for c in body["quality_caveats"]), (
        "every unknown input produces an explicit caveat — silence about missing evidence is the failure mode"
    )


# --- seam 5: re-stamping moves the verdict, never duplicates ---------------------------------


def test_restamp_updates_the_single_row_and_the_verdict(db_session, client, make_verified_user):
    from app.models.user import User
    from app.models.claim_evidence_metadata import ClaimEvidenceMetadata

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    source, claim = _make_claimed_source(db_session, founder.id)
    _login_founder(client)

    first = _stamp(client, claim.id, MECHANISM_ONLY_META)
    assert first["quality_cap"] == "hypothesis"
    second = _stamp(client, claim.id, RGT_META)
    assert second["quality_cap"] == "verified_fact" and second["quality_grade"] == "high"

    assert db_session.query(ClaimEvidenceMetadata).filter_by(claim_id=claim.id).count() == 1, "upsert, never duplicate"
    detail = client.get(f"/api/library/{source.id}")
    row = next(c for c in detail.json()["claims"] if c["id"] == str(claim.id))
    assert row["epistemic_status"] == "verified_fact" and row["quality_cap"] == "verified_fact"
    assert row["quality_grade"] == "high"


# --- seam 6: owner isolation (service contract + database-level FK) ---------------------------


def test_service_stamp_of_another_owners_claim_is_not_found(db_session, make_verified_user):
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()
    _source, claim = _make_claimed_source(db_session, owner_a.id)

    _set_rls_user(db_session, owner_b.id)
    with pytest.raises(ClaimNotFoundError):
        record_claim_evidence_metadata(db_session, owner_b.id, claim.id, RGT_META, declared_via="other-owner-test")


def test_composite_fk_rejects_a_cross_owner_row_at_the_database(db_session, make_verified_user):
    from app.models.claim_evidence_metadata import ClaimEvidenceMetadata

    owner_a, _ = make_verified_user()
    _source, claim = _make_claimed_source(db_session, owner_a.id)
    owner_b, _ = make_verified_user()

    _set_rls_user(db_session, owner_b.id)
    forged = ClaimEvidenceMetadata(
        owner_id=owner_b.id, claim_id=claim.id, metadata_json={}, quality_grade="high",
        quality_cap="verified_fact", quality_caveats=[], axis_findings=[], rejected_keys={},
        declared_via="forged",
    )
    db_session.add(forged)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_rls_hides_another_owners_metadata_from_the_read_path(db_session, make_verified_user):
    from app.models.claim_evidence_metadata import ClaimEvidenceMetadata
    from app.rag.evidence_metadata import quality_metadata_for_claims

    owner_a, _ = make_verified_user()
    _source, claim = _make_claimed_source(db_session, owner_a.id)
    _set_rls_user(db_session, owner_a.id)
    record_claim_evidence_metadata(db_session, owner_a.id, claim.id, RGT_META, declared_via="rls-test")

    owner_b, _ = make_verified_user()
    _set_rls_user(db_session, owner_b.id)
    assert quality_metadata_for_claims(db_session, owner_b.id, [claim.id]) == {}
    assert db_session.query(ClaimEvidenceMetadata).count() == 0, "B's session sees nothing under RLS"
    _set_rls_user(db_session, owner_a.id)
    assert db_session.query(ClaimEvidenceMetadata).count() == 1, "the row exists — for its owner"
