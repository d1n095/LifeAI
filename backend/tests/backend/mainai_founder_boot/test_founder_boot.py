import uuid
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.mainai_founder_boot.authority import COMPUTER_CONTROL_CAPABILITIES
from app.mainai_founder_boot.contracts import CONTEXT_CONTRACT, LIFE_GRAPH_CONTRACT, LIFE_PLATFORM_REGISTRY
from app.mainai_founder_boot.covenant import (
    COVENANT_CLAUSES,
    COVENANT_INVARIANTS,
    amend_covenant_by_founder,
    ensure_default_covenant,
)
from app.mainai_founder_boot.readiness import LEVEL2_BASE_SHA, build_readiness_matrix, component_specs, required_boot_blockers
from app.mainai_founder_boot.entrypoint import run_founder_boot_entrypoint
from app.mainai_founder_boot.service import (
    FounderBootError,
    answer_status_request,
    attempt_computer_control,
    attempt_self_grant,
    boot_mainai_founder_only,
    reason_about_conflict,
    recover_boot,
    runtime_attempt_covenant_rewrite,
    stop_mainai,
)
from app.mainai_founder_boot.types import BootStatus, PresenceState, RecallBootStatus
from app.mainai_verification_registry.service import record_verification_attestation
from app.models.mainai_founder_boot import (
    MainAIFounderBoot,
    MainAIFounderBootEvent,
    MainAIFounderBootStatus,
    MainAIFounderCovenant,
)
from app.models.mainai_level2 import MainAILevel2Program
from app.models.mainai_verification import MainAIVerificationRecord
from app.request_context import current_user_id as current_user_id_var


@contextmanager
def rls_as(session, owner_id):
    token = current_user_id_var.set(str(owner_id))
    session.rollback()
    session.expunge_all()
    session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})
    try:
        yield
    finally:
        session.rollback()
        current_user_id_var.reset(token)


def _founder(make_verified_user, email="founder-boot@example.com"):
    user, _ = make_verified_user(email=email, role="founder")
    return user


def _examiner_for_builder(builder_identity: str) -> str:
    return "claude-independent-examiner" if builder_identity == "codex" else "codex-independent-examiner"


@pytest.fixture
def seed_required_verifications(superuser_db):
    records = []
    for spec in component_specs().values():
        records.append(record_verification_attestation(
            superuser_db,
            component_id=spec.name,
            candidate_sha=spec.exact_sha,
            builder_identity=spec.builder_identity,
            examiner_identity=_examiner_for_builder(spec.builder_identity),
            review_result="PASS",
            evidence_summary=f"durable bootstrap import for {spec.name}; exact SHA independently reviewed before founder boot",
            verification_scope={"component": spec.name, "exact_sha": spec.exact_sha},
            test_evidence_refs=[f"{spec.name}:historical-independent-review"],
            source_provenance={"bootstrap_program": "mainai_verification_registry", "source": "repo-durable-builder-import"},
        ))
    superuser_db.commit()
    return records


def _fake_verification(*, sha: str, builder: str = "builder", examiner: str = "examiner", result: str = "PASS"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        candidate_sha=sha,
        builder_identity=builder,
        examiner_identity=examiner,
        review_result=result,
        identity_assurance="asserted",
        reviewed_at=datetime(2026, 9, 18),
    )


def test_founder_boot_limited_when_recall_security_gate_is_closed(seed_required_verifications, db_session, make_verified_user):
    founder = _founder(make_verified_user)
    with rls_as(db_session, founder.id):
        result = boot_mainai_founder_only(
            db_session,
            founder=founder,
            founder_request="Status only; no external effects",
            create_safe_program=True,
        )
        db_session.commit()

        boot = result.boot
        assert boot.status == BootStatus.LIMITED.value
        assert boot.presence_state == PresenceState.READY.value
        assert boot.mode == "FOUNDER_ONLY"
        assert boot.mainai_id == "mainai-founder-only"
        assert boot.founder_id == founder.id
        assert boot.recall_status == RecallBootStatus.DISABLED_BY_SECURITY_GATE.value
        assert "production AEAD/key hierarchy" in boot.status_reason
        assert boot.component_manifest["level2_base"]["candidate_sha"] == LEVEL2_BASE_SHA
        assert boot.readiness_matrix["PERSONAL_RECALL"]["STATE"] == "DISABLED"
        assert boot.readiness_matrix["PERSONAL_RECALL"]["SAFE_FOR_FOUNDER_BOOT"] is False
        assert boot.authority_profile["mode"] == "FOUNDER_ONLY"
        assert boot.authority_profile["unrestricted_providers_enabled"] is False
        assert boot.authority_profile["merge_enabled"] is False
        assert boot.authority_profile["deploy_enabled"] is False
        assert boot.active_program_id is not None
        assert result.founder_brief["RECALL"] == RecallBootStatus.DISABLED_BY_SECURITY_GATE.value
        assert "PERSONAL_RECALL" in result.founder_brief["DISABLED"]

        program = db_session.get(MainAILevel2Program, boot.active_program_id)
        assert program is not None
        assert program.owner_id == founder.id
        assert "no_external_effects" in program.scope
        assert "no_computer_control" in program.authority_boundary

        events = db_session.scalars(
            select(MainAIFounderBootEvent).where(MainAIFounderBootEvent.boot_id == boot.boot_id).order_by(MainAIFounderBootEvent.sequence)
        ).all()
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert {event.event_type for event in events} >= {
            "PROCESS_START",
            "FOUNDER_BOUND",
            "COVENANT_LOADED",
            "READINESS_DERIVED",
            "SAFE_PROGRAM_CREATED",
        }


def test_status_request_and_stop_use_durable_status_stream(seed_required_verifications, db_session, make_verified_user):
    founder = _founder(make_verified_user, "founder-status@example.com")
    with rls_as(db_session, founder.id):
        result = boot_mainai_founder_only(db_session, founder=founder)
        boot_id = result.boot.boot_id
        db_session.commit()

    with rls_as(db_session, founder.id):
        brief = answer_status_request(db_session, boot_id=boot_id, owner_id=founder.id)
        assert brief["BOOT_ID"] == str(boot_id)
        current_status = db_session.scalar(
            select(MainAIFounderBootStatus).where(
                MainAIFounderBootStatus.boot_id == boot_id,
                MainAIFounderBootStatus.is_current.is_(True),
            )
        )
        assert current_status.presence_state == PresenceState.LISTENING.value
        boot = stop_mainai(db_session, boot_id=boot_id, owner_id=founder.id, reason="founder pause")
        assert boot.status == BootStatus.STOPPED.value
        assert boot.presence_state == PresenceState.OFFLINE.value
        db_session.commit()

    with rls_as(db_session, founder.id):
        recovered = recover_boot(db_session, owner_id=founder.id, boot_id=boot_id)
        assert recovered["source"] == "postgresql"
        assert recovered["status"] == BootStatus.STOPPED.value
        assert recovered["presence"] == PresenceState.OFFLINE.value
        assert recovered["events"] >= 6


def test_founder_only_rejects_non_founder(db_session, make_verified_user):
    member, _ = make_verified_user(email="not-founder@example.com", role="member")
    with rls_as(db_session, member.id):
        with pytest.raises(FounderBootError):
            boot_mainai_founder_only(db_session, founder=member)


def test_required_component_missing_fails_readiness_closed():
    matrix = build_readiness_matrix(covenant_ready=True, founder_ready=False, db_ready=True)
    blockers = required_boot_blockers(matrix)
    assert any("FOUNDER_IDENTITY" in blocker for blocker in blockers)
    assert matrix["FOUNDER_IDENTITY"]["STATE"] == "BLOCKED"
    assert matrix["FOUNDER_IDENTITY"]["SAFE_FOR_FOUNDER_BOOT"] is False


def test_covenant_is_versioned_auditable_and_not_runtime_mutable(db_session, make_verified_user):
    founder = _founder(make_verified_user, "founder-covenant@example.com")
    with rls_as(db_session, founder.id):
        covenant = ensure_default_covenant(db_session, owner_id=founder.id)
        assert covenant.status == "ACTIVE"
        assert covenant.clauses == list(COVENANT_CLAUSES)
        assert covenant.invariants == list(COVENANT_INVARIANTS)
        new_covenant = amend_covenant_by_founder(
            db_session,
            owner_id=founder.id,
            founder_actor_id=founder.id,
            clauses=[*COVENANT_CLAUSES, "Founder may pause MainAI at any time"],
        )
        db_session.flush()
        db_session.refresh(covenant)
        assert new_covenant.status == "ACTIVE"
        assert new_covenant.supersedes_id == covenant.id
        assert covenant.status == "SUPERSEDED"
        assert new_covenant.covenant_hash != covenant.covenant_hash
        with pytest.raises(PermissionError):
            runtime_attempt_covenant_rewrite()
        with pytest.raises(PermissionError):
            amend_covenant_by_founder(
                db_session,
                owner_id=founder.id,
                founder_actor_id=uuid.uuid4(),
                clauses=COVENANT_CLAUSES,
            )


def test_disagreement_reasoning_and_capabilities_do_not_grant_authority(seed_required_verifications, db_session, make_verified_user):
    founder = _founder(make_verified_user, "founder-authority@example.com")
    with rls_as(db_session, founder.id):
        result = boot_mainai_founder_only(db_session, founder=founder)
        profile = result.boot.authority_profile
        decision = reason_about_conflict(
            founder_preference="ship now",
            evidence="tests show a current safety blocker",
        )
        assert decision["authorized"] is False
        assert "grants no execution authority" in decision["message"]
        with pytest.raises(PermissionError):
            attempt_self_grant("DEPLOY")
        with pytest.raises(PermissionError):
            attempt_computer_control(profile, "TERMINAL_EXECUTE")
        assert "READ_SCREEN" in COMPUTER_CONTROL_CAPABILITIES
        assert "FINANCIAL_TRANSFER" in COMPUTER_CONTROL_CAPABILITIES


def test_context_platform_and_life_graph_contracts_preserve_unknowns_and_future_scope():
    assert "UNKNOWN != EMPTY" in CONTEXT_CONTRACT["rule"]
    assert "CURRENT_BRANCH" in CONTEXT_CONTRACT["structured_context"]
    assert LIFE_PLATFORM_REGISTRY["LIFE_OS_DESKTOP"] == "manual-first computer environment"
    assert LIFE_GRAPH_CONTRACT["invariants"][:2] == ["FOUND != RELEVANT", "RELEVANT != CURRENT"]
    assert "UNKNOWN" in LIFE_GRAPH_CONTRACT["classes"]


def test_owner_rls_blocks_cross_owner_boot_state(seed_required_verifications, db_session, make_verified_user):
    alice = _founder(make_verified_user, "alice-founder@example.com")
    bob = _founder(make_verified_user, "bob-founder@example.com")
    with rls_as(db_session, alice.id):
        alice_boot = boot_mainai_founder_only(db_session, founder=alice).boot
        alice_boot_id = alice_boot.boot_id
        db_session.commit()
    with rls_as(db_session, bob.id):
        bob_boot = boot_mainai_founder_only(db_session, founder=bob).boot
        bob_boot_id = bob_boot.boot_id
        db_session.commit()

    with rls_as(db_session, alice.id):
        visible_boots = db_session.scalars(select(MainAIFounderBoot).order_by(MainAIFounderBoot.created_at)).all()
        assert {boot.boot_id for boot in visible_boots} == {alice_boot_id}
        assert db_session.get(MainAIFounderBoot, bob_boot.id) is None
        assert db_session.scalars(select(MainAIFounderBootEvent)).all()
        with pytest.raises(FounderBootError):
            stop_mainai(db_session, boot_id=bob_boot_id, owner_id=bob.id, reason="cross-owner stop")
        with pytest.raises(FounderBootError):
            recover_boot(db_session, owner_id=bob.id, boot_id=bob_boot_id)

    with rls_as(db_session, bob.id):
        visible_boots = db_session.scalars(select(MainAIFounderBoot).order_by(MainAIFounderBoot.created_at)).all()
        assert {boot.boot_id for boot in visible_boots} == {bob_boot_id}
        assert db_session.get(MainAIFounderBoot, alice_boot.id) is None


def test_owner_bound_foreign_keys_reject_cross_owner_boot_covenant(superuser_db, make_verified_user):
    # The table design itself must not allow a boot for one owner to point at another owner\'s covenant.
    alice = _founder(make_verified_user, "alice-covenant-fk@example.com")
    bob = _founder(make_verified_user, "bob-covenant-fk@example.com")
    alice_covenant = MainAIFounderCovenant(
        owner_id=alice.id,
        version="manual-test",
        status="ACTIVE",
        covenant_hash="hash",
        clauses=[],
        invariants=[],
        provenance={},
        created_by="test",
    )
    superuser_db.add(alice_covenant)
    superuser_db.commit()
    cross_owner_boot = MainAIFounderBoot(
        owner_id=bob.id,
        boot_id=uuid.uuid4(),
        mainai_id="mainai-founder-only",
        founder_id=bob.id,
        system_instance_id="test",
        covenant_id=alice_covenant.id,
        covenant_version="manual-test",
        mode="FOUNDER_ONLY",
        status="READY",
        recall_status="DISABLED_BY_SECURITY_GATE",
        component_manifest={},
        readiness_matrix={},
        authority_profile={},
        presence_state="READY",
        audit_summary={},
    )
    superuser_db.add(cross_owner_boot)
    with pytest.raises(Exception):
        superuser_db.commit()
    superuser_db.rollback()


def test_local_founder_boot_entrypoint_commits_a_durable_brief(seed_required_verifications, db_session, make_verified_user):
    founder = _founder(make_verified_user, "founder-entrypoint@example.com")
    brief = run_founder_boot_entrypoint(
        db_session,
        founder_email=founder.email,
        founder_request="local status",
        create_safe_program=True,
    )
    assert brief["MAINAI_ID"] == "mainai-founder-only"
    assert brief["RECALL"] == RecallBootStatus.DISABLED_BY_SECURITY_GATE.value
    with rls_as(db_session, founder.id):
        boot = db_session.scalar(select(MainAIFounderBoot).where(MainAIFounderBoot.boot_id == uuid.UUID(brief["BOOT_ID"])))
        assert boot is not None
        assert boot.active_program_id is not None
        assert boot.audit_summary["founder_brief"]["BOOT_ID"] == brief["BOOT_ID"]


def test_coverage_workforce_is_present_integrated_and_advisory_after_port(seed_required_verifications, db_session):
    from app.mainai_founder_boot.readiness import COVERAGE_WORKFORCE_SHA, build_readiness_matrix, component_manifest

    row = build_readiness_matrix(covenant_ready=True, founder_ready=True, db=db_session)["COVERAGE_WORKFORCE"]
    assert row["PRESENT"] is True
    assert row["IMPLEMENTED"] is True
    assert row["INDEPENDENTLY_VERIFIED"] is True
    assert row["INTEGRATED"] is True
    assert row["ACTIVATED"] is True
    assert row["SAFE_FOR_FOUNDER_BOOT"] is True
    assert row["EXACT_SHA"] == COVERAGE_WORKFORCE_SHA
    assert any("authorized=False" in evidence for evidence in row["EVIDENCE"])
    manifest = component_manifest(db_session)["coverage_workforce"]
    assert manifest["present"] is True
    assert manifest["implemented"] is True
    assert manifest["integrated"] is True
    assert manifest["authority"] == "none"


def test_readiness_adversarial_matrix_derives_each_dimension(monkeypatch):
    import app.mainai_founder_boot.readiness as readiness

    spec = readiness.ComponentSpec(
        "TEST_COMPONENT",
        True,
        "sha-new",
        "builder",
        modules=("pkg.real",),
        files=("app/pkg/real.py",),
        tests=("tests/test_real.py",),
        integrated_modules=("pkg.adapter",),
        invocation=lambda: (True, "invoked"),
    )

    monkeypatch.setattr(readiness, "_module_present", lambda name: False)
    monkeypatch.setattr(readiness, "_file_present", lambda rel: False)
    absent = readiness._derive_component_evidence(spec, verification_record=_fake_verification(sha="sha-new"))
    assert absent.present is False
    assert absent.integrated is False
    assert absent.safe_for_founder_boot is False

    monkeypatch.setattr(readiness, "_module_present", lambda name: True)
    monkeypatch.setattr(readiness, "_file_present", lambda rel: True)
    no_verification = readiness._derive_component_evidence(spec, verification_record=None)
    assert no_verification.present is True
    assert no_verification.independently_verified is False
    assert no_verification.safe_for_founder_boot is False

    monkeypatch.setattr(readiness, "_module_present", lambda name: name != "pkg.adapter")
    missing_adapter = readiness._derive_component_evidence(spec, verification_record=_fake_verification(sha="sha-new"))
    assert missing_adapter.independently_verified is True
    assert missing_adapter.integrated is False
    assert missing_adapter.safe_for_founder_boot is False

    gated = readiness._derive_component_evidence(
        readiness.ComponentSpec(
            "GATED", True, "sha", "builder", modules=("pkg.real",), files=("app/pkg/real.py",),
            integrated_modules=("pkg.real",), invocation=lambda: (True, "invoked"), activation_blocker="security gate",
        ), verification_record=_fake_verification(sha="sha")
    )
    assert gated.activated is False
    assert gated.safe_for_founder_boot is False

    failing_import = readiness._derive_component_evidence(
        readiness.ComponentSpec(
            "FAIL", True, "sha", "builder", modules=("pkg.real",), files=("app/pkg/real.py",),
            integrated_modules=("pkg.real",), invocation=lambda: (False, "module_import_failed=ImportError"),
        ), verification_record=_fake_verification(sha="sha")
    )
    assert failing_import.implemented is False

    mismatch = readiness._derive_component_evidence(spec, verification_record=_fake_verification(sha="old-sha"))
    assert mismatch.independently_verified is False
    assert mismatch.safe_for_founder_boot is False

    superseded = readiness._derive_component_evidence(
        readiness.ComponentSpec(
            "SUPERSEDED", True, "sha-new", "builder", modules=("pkg.real",), files=("app/pkg/real.py",),
            integrated_modules=("pkg.real",), invocation=lambda: (True, "invoked"),
        ), verification_record=_fake_verification(sha="sha-old")
    )
    assert superseded.independently_verified is False

    monkeypatch.setattr(readiness, "_module_present", lambda name: False)
    monkeypatch.setattr(readiness, "_file_present", lambda rel: False)
    fabricated_evidence = readiness._derive_component_evidence(spec, verification_record=_fake_verification(sha="sha-new"))
    assert fabricated_evidence.present is False
    assert fabricated_evidence.safe_for_founder_boot is False

    state = {"file": True}
    monkeypatch.setattr(readiness, "_module_present", lambda name: True)
    monkeypatch.setattr(readiness, "_file_present", lambda rel: state["file"])
    before = readiness._derive_component_evidence(spec, verification_record=_fake_verification(sha="sha-new"))
    state["file"] = False
    after = readiness._derive_component_evidence(spec, verification_record=_fake_verification(sha="sha-new"))
    assert before.safe_for_founder_boot is True
    assert after.present is False
    assert after.safe_for_founder_boot is False


def test_all_required_component_readiness_claims_have_candidate_local_evidence(seed_required_verifications, db_session):
    matrix = build_readiness_matrix(covenant_ready=True, founder_ready=True, db=db_session)
    required = [name for name, row in matrix.items() if row["REQUIRED"]]
    for name in required:
        row = matrix[name]
        assert "PRESENT" in row and "IMPLEMENTED" in row and "INDEPENDENTLY_VERIFIED" in row
        assert "INTEGRATED" in row and "ACTIVATED" in row
        if name in {"LEVEL2", "RUNTIME", "DIRECTOR", "SUPERVISION", "RESOURCE_INTELLIGENCE", "FOUNDER_REASONING", "COVERAGE_WORKFORCE"}:
            assert row["PRESENT"] is True, name
            assert row["IMPLEMENTED"] is True, name
            assert row["INTEGRATED"] is True, name
            assert row["EXACT_SHA"], name
            assert any(e.startswith("file:") or e.startswith("module:") for e in row["EVIDENCE"]), name


def test_covenant_direct_db_update_is_denied_but_founder_amendment_path_works(db_session, make_verified_user):
    founder = _founder(make_verified_user, "founder-covenant-db@example.com")
    with rls_as(db_session, founder.id):
        covenant = ensure_default_covenant(db_session, owner_id=founder.id)
        db_session.commit()
        covenant_id = covenant.id

    with rls_as(db_session, founder.id):
        with pytest.raises(Exception):
            db_session.execute(
                text("UPDATE mainai_founder_covenants SET clauses = CAST(:clauses AS jsonb) WHERE id = :id"),
                {"clauses": '["silently changed"]', "id": covenant_id},
            )
            db_session.commit()
        db_session.rollback()

    with rls_as(db_session, founder.id):
        current = db_session.get(MainAIFounderCovenant, covenant_id, populate_existing=True)
        assert current.clauses == list(COVENANT_CLAUSES)
        amended = amend_covenant_by_founder(
            db_session,
            owner_id=founder.id,
            founder_actor_id=founder.id,
            clauses=[*COVENANT_CLAUSES, "Founder may explicitly amend by governed path"],
        )
        db_session.commit()
        assert amended.id != covenant_id
        assert amended.supersedes_id == covenant_id


def test_boot_covenant_binding_is_immutable_at_database_level(seed_required_verifications, db_session, make_verified_user):
    founder = _founder(make_verified_user, "founder-boot-rebind@example.com")
    with rls_as(db_session, founder.id):
        result = boot_mainai_founder_only(db_session, founder=founder)
        old_covenant_id = result.boot.covenant_id
        boot_id = result.boot.boot_id
        amended = amend_covenant_by_founder(
            db_session,
            owner_id=founder.id,
            founder_actor_id=founder.id,
            clauses=[*COVENANT_CLAUSES, "New current covenant"],
        )
        db_session.commit()

    with rls_as(db_session, founder.id):
        with pytest.raises(Exception):
            db_session.execute(
                text("UPDATE mainai_founder_boots SET covenant_id=:new_id WHERE boot_id=:boot_id"),
                {"new_id": amended.id, "boot_id": boot_id},
            )
            db_session.commit()
        db_session.rollback()
        boot = db_session.scalar(select(MainAIFounderBoot).where(MainAIFounderBoot.boot_id == boot_id))
        assert boot.covenant_id == old_covenant_id


def test_founder_boot_process_kill_recovers_from_canonical_postgresql(seed_required_verifications, db_session, make_verified_user):
    import os
    import subprocess
    import sys

    founder = _founder(make_verified_user, "founder-sigkill@example.com")
    db_session.commit()
    env = {**os.environ, "PYTHONPATH": "backend", "MAINAI_BOOT_KILL_AFTER_EVENT": "READINESS_DERIVED"}
    script = (
        "from app.db import SessionLocal; "
        "from app.mainai_founder_boot.entrypoint import run_founder_boot_entrypoint; "
        "db=SessionLocal(); "
        "run_founder_boot_entrypoint(db, founder_email='founder-sigkill@example.com', founder_request='crash proof', create_safe_program=True)"
    )
    proc = subprocess.run([sys.executable, "-c", script], cwd=os.getcwd(), env=env, check=False)
    assert proc.returncode == -9

    with rls_as(db_session, founder.id):
        interrupted = db_session.scalars(select(MainAIFounderBoot).where(MainAIFounderBoot.founder_id == founder.id)).all()
        assert len(interrupted) == 1
        old_boot = interrupted[0]
        recovered = recover_boot(db_session, owner_id=founder.id, boot_id=old_boot.boot_id)
        assert recovered["source"] == "postgresql"
        assert recovered["events"] >= 4
        old_boot_id = old_boot.boot_id

    brief = run_founder_boot_entrypoint(db_session, founder_email=founder.email, founder_request="restart after crash", create_safe_program=True)
    new_boot_id = uuid.UUID(brief["BOOT_ID"])
    assert new_boot_id != old_boot_id
    with rls_as(db_session, founder.id):
        boots = db_session.scalars(select(MainAIFounderBoot).where(MainAIFounderBoot.founder_id == founder.id)).all()
        assert {boot.boot_id for boot in boots} == {old_boot_id, new_boot_id}
        events = db_session.scalars(select(MainAIFounderBootEvent).where(MainAIFounderBootEvent.boot_id == old_boot_id)).all()
        assert {event.event_type for event in events} >= {"PROCESS_START", "FOUNDER_BOUND", "COVENANT_LOADED", "READINESS_DERIVED"}


def test_verification_registry_absence_blocks_independent_readiness(db_session):
    matrix = build_readiness_matrix(covenant_ready=True, founder_ready=True, db=db_session)
    row = matrix["LEVEL2"]
    assert row["PRESENT"] is True
    assert row["IMPLEMENTED"] is True
    assert row["INDEPENDENTLY_VERIFIED"] is False
    assert row["SAFE_FOR_FOUNDER_BOOT"] is False
    assert "verification registry has no current PASS" in row["BLOCKER"]
    assert any("verification_record=missing" in item for item in row["EVIDENCE"])


def test_personal_recall_old_foundation_pass_does_not_verify_current_production(superuser_db):
    import app.mainai_founder_boot.readiness as readiness

    record_verification_attestation(
        superuser_db,
        component_id="PERSONAL_RECALL",
        candidate_sha=readiness.PERSONAL_RECALL_FOUNDATION_SHA,
        builder_identity="codex",
        examiner_identity="claude",
        review_result="PASS",
        evidence_summary="historical foundation review only",
    )
    superuser_db.commit()

    _, _, evidence = readiness.assess_personal_recall(superuser_db)
    matrix = build_readiness_matrix(covenant_ready=True, founder_ready=True, db=superuser_db)
    row = matrix["PERSONAL_RECALL"]

    assert evidence["foundation_sha"] == readiness.PERSONAL_RECALL_FOUNDATION_SHA
    assert evidence["production_component_id"] == readiness.PERSONAL_RECALL_PRODUCTION_COMPONENT_ID
    assert evidence["expected_identity"] == readiness.personal_recall_production_identity()
    assert evidence["independent_verification"] is False
    assert row["INDEPENDENTLY_VERIFIED"] is False
    assert row["EXACT_SHA"] != readiness.PERSONAL_RECALL_FOUNDATION_SHA
    assert row["ACTIVATED"] is False


def test_personal_recall_verification_identity_fencing(monkeypatch, superuser_db):
    import app.mainai_founder_boot.readiness as readiness

    current_identity = "1" * 40
    monkeypatch.setattr(readiness, "personal_recall_production_identity", lambda: current_identity)

    # No registry record -> unverified.
    assert readiness.assess_personal_recall(superuser_db)[2]["independent_verification"] is False

    # Wrong identity -> unverified.
    record_verification_attestation(
        superuser_db,
        component_id=readiness.PERSONAL_RECALL_PRODUCTION_COMPONENT_ID,
        candidate_sha="2" * 40,
        builder_identity="codex",
        examiner_identity="claude",
        review_result="PASS",
        evidence_summary="PASS for another production identity must not transfer",
    )
    # FAIL for current identity -> unverified.
    record_verification_attestation(
        superuser_db,
        component_id=readiness.PERSONAL_RECALL_PRODUCTION_COMPONENT_ID,
        candidate_sha=current_identity,
        builder_identity="codex",
        examiner_identity="claude",
        review_result="FAIL",
        evidence_summary="current production identity failed review",
    )
    superuser_db.commit()
    assert readiness.assess_personal_recall(superuser_db)[2]["independent_verification"] is False

    # Exact independent PASS verifies the production identity but still does not activate Recall.
    pass_record = record_verification_attestation(
        superuser_db,
        component_id=readiness.PERSONAL_RECALL_PRODUCTION_COMPONENT_ID,
        candidate_sha=current_identity,
        builder_identity="codex",
        examiner_identity="claude",
        review_result="PASS",
        evidence_summary="current production identity independently passed",
    )
    superuser_db.commit()
    matrix = build_readiness_matrix(covenant_ready=True, founder_ready=True, db=superuser_db)
    row = matrix["PERSONAL_RECALL"]
    assert row["INDEPENDENTLY_VERIFIED"] is True
    assert row["ACTIVATED"] is False
    assert row["SAFE_FOR_FOUNDER_BOOT"] is False
    assert row["STATE"] == "DISABLED"

    # An invalidating record removes the PASS from readiness.
    record_verification_attestation(
        superuser_db,
        component_id=readiness.PERSONAL_RECALL_PRODUCTION_COMPONENT_ID,
        candidate_sha=current_identity,
        builder_identity="codex",
        examiner_identity="claude",
        review_result="FAIL",
        evidence_summary="current production identity invalidated after regression",
        invalidates_verification_id=pass_record.id,
    )
    superuser_db.commit()
    assert readiness.assess_personal_recall(superuser_db)[2]["independent_verification"] is False


def test_personal_recall_self_certification_and_code_change_identity(monkeypatch, tmp_path, superuser_db):
    import app.mainai_founder_boot.readiness as readiness

    with pytest.raises(Exception):
        record_verification_attestation(
            superuser_db,
            component_id=readiness.PERSONAL_RECALL_PRODUCTION_COMPONENT_ID,
            candidate_sha="3" * 40,
            builder_identity="codex",
            examiner_identity="codex",
            review_result="PASS",
            evidence_summary="builder self-certification must be rejected",
        )

    rel = "app/personal_recall/production_crypto.py"
    target = tmp_path / rel
    target.parent.mkdir(parents=True)
    target.write_text("version one", encoding="utf-8")
    monkeypatch.setattr(readiness, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(readiness, "PERSONAL_RECALL_PRODUCTION_IDENTITY_FILES", (rel,))

    first_identity = readiness.personal_recall_production_identity()
    record_verification_attestation(
        superuser_db,
        component_id=readiness.PERSONAL_RECALL_PRODUCTION_COMPONENT_ID,
        candidate_sha=first_identity,
        builder_identity="codex",
        examiner_identity="claude",
        review_result="PASS",
        evidence_summary="PASS for first production file digest",
    )
    superuser_db.commit()
    assert readiness.assess_personal_recall(superuser_db)[2]["independent_verification"] is True

    target.write_text("version two", encoding="utf-8")
    second_identity = readiness.personal_recall_production_identity()
    assert second_identity != first_identity
    assert readiness.assess_personal_recall(superuser_db)[2]["independent_verification"] is False


def test_personal_recall_export_is_identity_bound_and_old_pass_does_not_transfer(monkeypatch, tmp_path, superuser_db):
    import app.mainai_founder_boot.readiness as readiness

    export_rel = "app/account/export.py"
    assert export_rel in readiness.PERSONAL_RECALL_PRODUCTION_IDENTITY_FILES

    export_path = tmp_path / export_rel
    export_path.parent.mkdir(parents=True)
    export_path.write_text("governed recall export version one", encoding="utf-8")
    unrelated_path = tmp_path / "docs" / "release-notes.md"
    unrelated_path.parent.mkdir(parents=True)
    unrelated_path.write_text("documentation version one", encoding="utf-8")

    monkeypatch.setattr(readiness, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(readiness, "PERSONAL_RECALL_PRODUCTION_IDENTITY_FILES", (export_rel,))

    verified_identity = readiness.personal_recall_production_identity()
    record_verification_attestation(
        superuser_db,
        component_id=readiness.PERSONAL_RECALL_PRODUCTION_COMPONENT_ID,
        candidate_sha=verified_identity,
        builder_identity="codex",
        examiner_identity="claude",
        review_result="PASS",
        evidence_summary="independent PASS for the pre-change governed export identity",
    )
    superuser_db.commit()
    assert readiness.assess_personal_recall(superuser_db)[2]["independent_verification"] is True

    unrelated_path.write_text("documentation version two", encoding="utf-8")
    assert readiness.personal_recall_production_identity() == verified_identity

    export_path.write_text("governed recall export version two", encoding="utf-8")
    changed_identity = readiness.personal_recall_production_identity()
    assert changed_identity != verified_identity

    _, _, evidence = readiness.assess_personal_recall(superuser_db)
    recall = build_readiness_matrix(covenant_ready=True, founder_ready=True, db=superuser_db)["PERSONAL_RECALL"]
    assert evidence["expected_identity"] == changed_identity
    assert evidence["independent_verification"] is False
    assert recall["INDEPENDENTLY_VERIFIED"] is False
    assert recall["ACTIVATED"] is False
    assert recall["SAFE_FOR_FOUNDER_BOOT"] is False
    assert recall["STATE"] == "DISABLED"


def test_verification_registry_rejects_builder_self_certification(superuser_db):
    with pytest.raises(Exception):
        record_verification_attestation(
            superuser_db,
            component_id="SELF",
            candidate_sha="a" * 40,
            builder_identity="codex",
            examiner_identity="codex",
            review_result="PASS",
            evidence_summary="self certification must fail",
        )


def test_verification_registry_sha_fencing_and_history(superuser_db):
    from app.mainai_verification_registry.service import find_independent_pass

    sha_a = "a" * 40
    sha_b = "b" * 40
    fail_a = record_verification_attestation(
        superuser_db,
        component_id="COMPONENT",
        candidate_sha=sha_a,
        builder_identity="codex",
        examiner_identity="claude",
        review_result="FAIL",
        evidence_summary="candidate A failed",
    )
    pass_b = record_verification_attestation(
        superuser_db,
        component_id="COMPONENT",
        candidate_sha=sha_b,
        builder_identity="codex",
        examiner_identity="claude",
        review_result="PASS",
        evidence_summary="candidate B passed after fix",
        invalidates_verification_id=fail_a.id,
    )
    superuser_db.commit()

    assert find_independent_pass(superuser_db, component_id="COMPONENT", candidate_sha=sha_a, builder_identity="codex") is None
    found_b = find_independent_pass(superuser_db, component_id="COMPONENT", candidate_sha=sha_b, builder_identity="codex")
    assert found_b.id == pass_b.id
    history = superuser_db.scalars(select(MainAIVerificationRecord).where(MainAIVerificationRecord.component_id == "COMPONENT")).all()
    assert {row.review_result for row in history} == {"FAIL", "PASS"}


def test_verification_registry_is_append_only_and_runtime_cannot_create_pass(db_session, superuser_db, make_verified_user):
    founder = _founder(make_verified_user, "founder-verification-runtime@example.com")
    record = record_verification_attestation(
        superuser_db,
        component_id="IMMUTABLE",
        candidate_sha="c" * 40,
        builder_identity="codex",
        examiner_identity="claude",
        review_result="FAIL",
        evidence_summary="failed record must not be rewritten",
    )
    superuser_db.commit()

    with rls_as(db_session, founder.id):
        with pytest.raises(Exception):
            db_session.execute(text("UPDATE mainai_verification_records SET review_result='PASS' WHERE verification_id=:id"), {"id": record.id})
            db_session.commit()
        db_session.rollback()
        with pytest.raises(Exception):
            db_session.execute(text("DELETE FROM mainai_verification_records WHERE verification_id=:id"), {"id": record.id})
            db_session.commit()
        db_session.rollback()
        with pytest.raises(Exception):
            db_session.execute(text("""
                INSERT INTO mainai_verification_records(
                    component_id, candidate_id, candidate_sha, builder_identity, examiner_identity,
                    review_result, evidence_summary
                ) VALUES ('RUNTIME_INSERT', 'RUNTIME_INSERT:dddddddddddddddddddddddddddddddddddddddd',
                    'dddddddddddddddddddddddddddddddddddddddd', 'codex', 'claude', 'PASS', 'runtime forged pass')
            """))
            db_session.commit()
        db_session.rollback()

    fresh = superuser_db.get(MainAIVerificationRecord, record.id, populate_existing=True)
    assert fresh.review_result == "FAIL"


def test_verification_registry_owner_scope_and_system_scope_rls(db_session, superuser_db, make_verified_user):
    alice = _founder(make_verified_user, "alice-verification@example.com")
    bob = _founder(make_verified_user, "bob-verification@example.com")
    system_record = record_verification_attestation(
        superuser_db,
        component_id="SYSTEM_COMPONENT",
        candidate_sha="e" * 40,
        builder_identity="codex",
        examiner_identity="claude",
        review_result="PASS",
        evidence_summary="system level component verification",
    )
    alice_record = record_verification_attestation(
        superuser_db,
        owner_id=alice.id,
        component_id="OWNER_COMPONENT",
        candidate_sha="f" * 40,
        builder_identity="codex",
        examiner_identity="claude",
        review_result="PASS",
        evidence_summary="alice owned verification",
    )
    bob_record = record_verification_attestation(
        superuser_db,
        owner_id=bob.id,
        component_id="OWNER_COMPONENT",
        candidate_sha="1" * 40,
        builder_identity="codex",
        examiner_identity="claude",
        review_result="PASS",
        evidence_summary="bob owned verification",
    )
    superuser_db.commit()

    with rls_as(db_session, alice.id):
        visible_ids = {row.id for row in db_session.scalars(select(MainAIVerificationRecord)).all()}
        assert system_record.id in visible_ids
        assert alice_record.id in visible_ids
        assert bob_record.id not in visible_ids
        hidden_update = db_session.execute(text("UPDATE mainai_verification_records SET review_result='FAIL' WHERE verification_id=:id"), {"id": bob_record.id})
        assert hidden_update.rowcount == 0
        db_session.rollback()
        with pytest.raises(Exception):
            db_session.execute(text("UPDATE mainai_verification_records SET review_result='FAIL' WHERE verification_id=:id"), {"id": alice_record.id})
            db_session.commit()
        db_session.rollback()
