import uuid
from contextlib import contextmanager

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
from app.mainai_founder_boot.readiness import LEVEL2_BASE_SHA, build_readiness_matrix, required_boot_blockers
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
from app.models.mainai_founder_boot import (
    MainAIFounderBoot,
    MainAIFounderBootEvent,
    MainAIFounderBootStatus,
    MainAIFounderCovenant,
)
from app.models.mainai_level2 import MainAILevel2Program
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


def test_founder_boot_limited_when_recall_security_gate_is_closed(db_session, make_verified_user):
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


def test_status_request_and_stop_use_durable_status_stream(db_session, make_verified_user):
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


def test_disagreement_reasoning_and_capabilities_do_not_grant_authority(db_session, make_verified_user):
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


def test_owner_rls_blocks_cross_owner_boot_state(db_session, make_verified_user):
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


def test_local_founder_boot_entrypoint_commits_a_durable_brief(db_session, make_verified_user):
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
