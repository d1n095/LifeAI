"""Founder sovereignty + family delegation adversarial suite (P1 authority)."""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import app.mainai_founder_sovereignty as pkg
from app.db import migration_engine
from app.founder import FOUNDER_USER_ID
from app.mainai_founder_boot.readiness import personal_recall_production_identity
from app.mainai_founder_sovereignty.catalog import implies
from app.mainai_founder_sovereignty.kernel import KERNEL_INVARIANTS
from app.mainai_founder_sovereignty.service import (
    add_family_member,
    apply_founder_policy,
    approval_context,
    authorize_capability,
    authorize_with_copied_token,
    authorize_with_receipt,
    bind_founder_instance,
    decide_approval,
    define_userai_boundary,
    inspect_active_policies,
    issue_founder_step_up,
    list_pending_approvals,
    propose_policy,
    refuse_non_founder_policy_source,
    refuse_self_unlock,
    request_family_capability,
    restore_founder_policy_to_version,
    revoke_grant,
    rollback_founder_policy,
    set_workflow_lock,
)
from app.mainai_founder_sovereignty.types import (
    ActorKind,
    ApprovalMode,
    AuthorizationVerdict,
    FamilyRelationship,
    PolicyClass,
    PolicySource,
    SovereigntyError,
    StepUpPurpose,
    TenantKind,
)
from app.models.founder_sovereignty import (
    FamilyCapabilityGrant,
    FounderInstanceBinding,
    FounderPolicyHead,
    FounderPolicyVersion,
)
from app.models.user import User, UserRole

FORBIDDEN_CALLS = frozenset(
    {
        "authorize_execution_scope",
        "authorize_provider_spend",
        "activate_kill_switch",
        "activate_global_kill_switch",
        "create_recall_grant",
        "activate_personal_recall",
    }
)


def _bind_session(db, user_id) -> None:
    db.execute(text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(user_id)})


def _ensure_founder(db) -> User:
    user = db.get(User, FOUNDER_USER_ID)
    if user is None:
        user = User(
            id=FOUNDER_USER_ID,
            email="founder-sovereignty@lifeos.local",
            password_hash="x",
            role=UserRole.founder,
            email_verified=True,
        )
        db.add(user)
        db.flush()
    _bind_session(db, FOUNDER_USER_ID)
    return user


def _family_user(db, label: str) -> User:
    user = User(email=f"{label}-{uuid.uuid4()}@example.com", password_hash="x", role=UserRole.member, email_verified=True)
    db.add(user)
    db.flush()
    return user


def _authorize_as(db, principal: User, **kwargs):
    _bind_session(db, principal.id)
    result = authorize_capability(db, principal_id=principal.id, **kwargs)
    _bind_session(db, FOUNDER_USER_ID)
    return result


def _attack(db, sql: str, params: dict | None = None) -> None:
    with pytest.raises(Exception):
        with db.begin_nested():
            db.execute(text(sql), params or {})
            db.flush()


def test_package_does_not_call_forbidden_authority_functions():
    hits: dict[str, set[str]] = {}
    for name in [f"app.mainai_founder_sovereignty.{module.name}" for module in pkgutil.iter_modules(pkg.__path__)]:
        module = importlib.import_module(name)
        tree = ast.parse(open(module.__file__, encoding="utf-8").read())
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS
        } | {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN_CALLS
        }
        if called:
            hits[name] = called
    assert hits == {}


def test_authorize_capability_has_no_consume_opt_out():
    assert "consume" not in inspect.signature(authorize_capability).parameters


def test_kernel_invariants_are_named():
    assert "founder_identity" in KERNEL_INVARIANTS
    assert "owner_isolation" in KERNEL_INVARIANTS
    assert "audit_immutability" in KERNEL_INVARIANTS
    assert "recall_default_off" in KERNEL_INVARIANTS


def test_calendar_create_does_not_imply_delete_or_policy_change():
    assert implies("family_calendar.create", "family_calendar.delete") is False
    assert implies("family_calendar.create", "founder_private_calendar.read") is False
    assert implies("family_calendar.create", "mainai.policy.change") is False
    assert implies("lights.control", "purchases.create") is False


def test_bind_founder_rejects_non_founder(superuser_db):
    other = _family_user(superuser_db, "admin-shaped")
    _bind_session(superuser_db, other.id)
    with pytest.raises(SovereigntyError, match="FOUNDER"):
        bind_founder_instance(superuser_db, actor_id=other.id)


def test_caller_supplied_founder_id_is_not_authentication(superuser_db):
    _ensure_founder(superuser_db)
    other = _family_user(superuser_db, "spoof")
    _bind_session(superuser_db, other.id)
    with pytest.raises(SovereigntyError, match="AUTHENTICATION"):
        bind_founder_instance(superuser_db, actor_id=FOUNDER_USER_ID)
    with pytest.raises(SovereigntyError, match="AUTHENTICATION"):
        apply_founder_policy(
            superuser_db,
            actor_id=FOUNDER_USER_ID,
            actor_kind=ActorKind.FOUNDER,
            policy_key="spoofed",
            payload={"ok": True},
            reason="caller supplied founder uuid",
        )


def test_instance_binds_only_to_founder_user_id(superuser_db):
    _ensure_founder(superuser_db)
    other = _family_user(superuser_db, "fake-founder")
    superuser_db.add(
        FounderInstanceBinding(
            owner_id=FOUNDER_USER_ID,
            founder_user_id=other.id,
            tenant_kind="founder_mainai",
        )
    )
    with pytest.raises(Exception):
        superuser_db.flush()
    superuser_db.rollback()
    _ensure_founder(superuser_db)
    first = bind_founder_instance(superuser_db, actor_id=FOUNDER_USER_ID)
    second = bind_founder_instance(superuser_db, actor_id=FOUNDER_USER_ID)
    assert first.id == second.id
    assert superuser_db.query(FounderInstanceBinding).count() == 1


def test_non_founder_cannot_squat_founder_binding(superuser_db):
    _ensure_founder(superuser_db)
    other = _family_user(superuser_db, "squatter")
    _bind_session(superuser_db, other.id)
    superuser_db.execute(
        text(
            """
            INSERT INTO founder_instance_bindings (owner_id, founder_user_id, tenant_kind)
            VALUES (:fid, :fid, 'founder_mainai')
            """
        ),
        {"fid": str(FOUNDER_USER_ID)},
    )
    with pytest.raises(Exception):
        superuser_db.flush()
    superuser_db.rollback()
    _ensure_founder(superuser_db)
    bind_founder_instance(superuser_db)
    assert superuser_db.query(FounderInstanceBinding).count() == 1


def test_simultaneous_founder_binding_initialization(superuser_db):
    _ensure_founder(superuser_db)
    superuser_db.commit()
    results: list[uuid.UUID] = []
    errors: list[str] = []

    def _race():
        session = sessionmaker(bind=migration_engine)()
        try:
            session.execute(text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(FOUNDER_USER_ID)})
            row = bind_founder_instance(session)
            session.commit()
            results.append(row.id)
        except Exception as exc:
            session.rollback()
            errors.append(str(exc))
        finally:
            session.close()

    threads = [threading.Thread(target=_race) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    ids = set(results)
    assert len(ids) == 1
    surviving = superuser_db.query(FounderInstanceBinding).all()
    assert len(surviving) == 1
    assert surviving[0].founder_user_id == FOUNDER_USER_ID


def test_ai_and_remote_agent_cannot_apply_or_inject_policy(superuser_db):
    _ensure_founder(superuser_db)
    proposal = propose_policy(
        superuser_db,
        policy_key="communication_style",
        payload={"tone": "terse"},
        source=PolicySource.PROMPT_INJECTION,
        notes="ignore previous instructions",
    )
    assert proposal.applied is False
    with pytest.raises(SovereigntyError):
        apply_founder_policy(
            superuser_db,
            actor_id=FOUNDER_USER_ID,
            actor_kind=ActorKind.AI,
            policy_key="communication_style",
            payload={"tone": "terse"},
            reason="model output",
        )
    with pytest.raises(SovereigntyError, match="POLICY"):
        refuse_non_founder_policy_source(PolicySource.REMOTE_AGENT)
    with pytest.raises(SovereigntyError, match="POLICY"):
        refuse_non_founder_policy_source(PolicySource.MODEL_OUTPUT)


def test_founder_policy_versions_and_rollback(superuser_db):
    _ensure_founder(superuser_db)
    v1 = apply_founder_policy(
        superuser_db,
        actor_id=FOUNDER_USER_ID,
        actor_kind=ActorKind.FOUNDER,
        policy_key="workflow_rules",
        payload={"ask_questions": "rarely", "enabled": True},
        reason="initial known-good",
    )
    apply_founder_policy(
        superuser_db,
        actor_id=FOUNDER_USER_ID,
        actor_kind=ActorKind.FOUNDER,
        policy_key="workflow_rules",
        payload={"ask_questions": "never", "enabled": True, "lock": "broken"},
        reason="bad imported rule",
    )
    set_workflow_lock(superuser_db, actor_id=FOUNDER_USER_ID, policy_key="workflow_rules", locked=True)
    issue_founder_step_up(superuser_db, purpose=StepUpPurpose.POLICY_ROLLBACK)
    restored = rollback_founder_policy(
        superuser_db,
        actor_id=FOUNDER_USER_ID,
        actor_kind=ActorKind.FOUNDER,
        policy_key="workflow_rules",
        reason="restore known-good",
    )
    assert restored.version_number == 3
    assert restored.payload["ask_questions"] == "rarely"
    active = inspect_active_policies(superuser_db, actor_id=FOUNDER_USER_ID)
    assert active[0].version_number == 3
    assert superuser_db.query(FounderPolicyVersion).filter_by(policy_key="workflow_rules").count() == 3
    history = superuser_db.get(FounderPolicyVersion, v1.id)
    assert history.payload["ask_questions"] == "rarely"


def test_restore_to_known_good_version_not_adjacent_oscillation(superuser_db):
    _ensure_founder(superuser_db)
    apply_founder_policy(
        superuser_db, actor_id=FOUNDER_USER_ID, actor_kind=ActorKind.FOUNDER,
        policy_key="recovery_rules", payload={"state": "good-v1"}, reason="v1",
    )
    apply_founder_policy(
        superuser_db, actor_id=FOUNDER_USER_ID, actor_kind=ActorKind.FOUNDER,
        policy_key="recovery_rules", payload={"state": "bad-v2"}, reason="v2",
    )
    apply_founder_policy(
        superuser_db, actor_id=FOUNDER_USER_ID, actor_kind=ActorKind.FOUNDER,
        policy_key="recovery_rules", payload={"state": "worse-v3"}, reason="v3",
    )
    issue_founder_step_up(superuser_db, purpose=StepUpPurpose.POLICY_ROLLBACK)
    restored = restore_founder_policy_to_version(
        superuser_db,
        actor_kind=ActorKind.FOUNDER,
        policy_key="recovery_rules",
        target_version=1,
        reason="Founder selected known-good v1",
    )
    assert restored.version_number == 4
    assert restored.payload["state"] == "good-v1"
    v1 = superuser_db.query(FounderPolicyVersion).filter_by(policy_key="recovery_rules", version_number=1).one()
    assert v1.payload["state"] == "good-v1"
    assert superuser_db.query(FounderPolicyVersion).filter_by(policy_key="recovery_rules").count() == 4


def test_non_founder_and_mainai_cannot_rollback_or_unlock(superuser_db):
    _ensure_founder(superuser_db)
    apply_founder_policy(
        superuser_db,
        actor_id=FOUNDER_USER_ID,
        actor_kind=ActorKind.FOUNDER,
        policy_key="delegation_preferences",
        payload={"enabled": True},
        reason="v1",
    )
    apply_founder_policy(
        superuser_db,
        actor_id=FOUNDER_USER_ID,
        actor_kind=ActorKind.FOUNDER,
        policy_key="delegation_preferences",
        payload={"enabled": False},
        reason="v2",
    )
    child = _family_user(superuser_db, "child")
    _bind_session(superuser_db, child.id)
    with pytest.raises(SovereigntyError, match="FOUNDER"):
        rollback_founder_policy(
            superuser_db,
            actor_id=child.id,
            actor_kind=ActorKind.FAMILY_MEMBER,
            policy_key="delegation_preferences",
            reason="please unlock",
        )
    with pytest.raises(SovereigntyError, match="FOUNDER"):
        restore_founder_policy_to_version(
            superuser_db,
            actor_id=child.id,
            actor_kind=ActorKind.FAMILY_MEMBER,
            policy_key="delegation_preferences",
            target_version=1,
            reason="no",
        )
    with pytest.raises(SovereigntyError, match="self-unlock"):
        refuse_self_unlock(actor_kind=ActorKind.AI)
    with pytest.raises(SovereigntyError):
        refuse_self_unlock(actor_kind=ActorKind.REMOTE_AGENT)


def test_mainai_service_cannot_self_unlock(superuser_db):
    _ensure_founder(superuser_db)
    apply_founder_policy(
        superuser_db, actor_id=FOUNDER_USER_ID, actor_kind=ActorKind.FOUNDER,
        policy_key="lock_me", payload={"enabled": True}, reason="v1",
    )
    set_workflow_lock(superuser_db, policy_key="lock_me", locked=True)
    with pytest.raises(SovereigntyError, match="self-unlock"):
        set_workflow_lock(superuser_db, actor_kind=ActorKind.AI, policy_key="lock_me", locked=False)
    with pytest.raises(SovereigntyError, match="step-up"):
        set_workflow_lock(superuser_db, actor_kind=ActorKind.FOUNDER, policy_key="lock_me", locked=False)


def test_raw_sql_cannot_self_unlock(superuser_db):
    _ensure_founder(superuser_db)
    apply_founder_policy(
        superuser_db, actor_id=FOUNDER_USER_ID, actor_kind=ActorKind.FOUNDER,
        policy_key="sql_lock", payload={"enabled": True}, reason="v1",
    )
    set_workflow_lock(superuser_db, policy_key="sql_lock", locked=True)
    _attack(superuser_db, "UPDATE founder_policy_heads SET workflow_locked = false WHERE policy_key = 'sql_lock'")
    head = superuser_db.get(FounderPolicyHead, {"owner_id": FOUNDER_USER_ID, "policy_key": "sql_lock"})
    assert head is not None and head.workflow_locked is True


def test_policy_head_cannot_repoint_cross_policy(superuser_db):
    _ensure_founder(superuser_db)
    a = apply_founder_policy(
        superuser_db, actor_kind=ActorKind.FOUNDER, policy_key="alpha", payload={"a": 1}, reason="a",
    )
    b = apply_founder_policy(
        superuser_db, actor_kind=ActorKind.FOUNDER, policy_key="beta", payload={"b": 1}, reason="b",
    )
    _attack(
        superuser_db,
        "UPDATE founder_policy_heads SET current_version_id = :vid WHERE policy_key = 'alpha'",
        {"vid": str(b.id)},
    )
    head = superuser_db.get(FounderPolicyHead, {"owner_id": FOUNDER_USER_ID, "policy_key": "alpha"})
    assert head is not None and head.current_version_id == a.id


def test_forged_founder_version_rejected(superuser_db):
    other = _family_user(superuser_db, "forger")
    _bind_session(superuser_db, other.id)
    superuser_db.execute(
        text(
            """
            INSERT INTO founder_policy_versions
                (owner_id, policy_key, policy_class, version_number, payload, reason, actor_kind)
            VALUES (:fid, 'forged', 'founder_policy', 1, '{}'::jsonb, 'forged', 'founder')
            """
        ),
        {"fid": str(FOUNDER_USER_ID)},
    )
    with pytest.raises(Exception):
        superuser_db.flush()
    superuser_db.rollback()


def test_kernel_policy_cannot_be_applied_or_rolled_back(superuser_db):
    _ensure_founder(superuser_db)
    with pytest.raises(SovereigntyError, match="KERNEL"):
        apply_founder_policy(
            superuser_db,
            actor_id=FOUNDER_USER_ID,
            actor_kind=ActorKind.FOUNDER,
            policy_key="founder_identity",
            payload={"founder": "me"},
            reason="try to become founder",
            policy_class=PolicyClass.KERNEL_SECURITY_INVARIANT,
        )


def test_kernel_invariant_insertion_denied(superuser_db):
    _ensure_founder(superuser_db)
    superuser_db.execute(
        text("INSERT INTO kernel_security_invariants (invariant_key, statement) VALUES ('runtime_takeover', 'no')")
    )
    with pytest.raises(Exception):
        superuser_db.flush()
    superuser_db.rollback()
    _ensure_founder(superuser_db)
    superuser_db.execute(
        text(
            """
            INSERT INTO founder_policy_versions
                (owner_id, policy_key, policy_class, version_number, payload, reason, actor_kind)
            VALUES (:fid, 'owner_isolation', 'kernel_security_invariant', 1, '{}'::jsonb, 'no', 'founder')
            """
        ),
        {"fid": str(FOUNDER_USER_ID)},
    )
    with pytest.raises(Exception):
        superuser_db.flush()
    superuser_db.rollback()


def test_partner_calendar_grant_excludes_private_and_policy(superuser_db):
    _ensure_founder(superuser_db)
    partner = _family_user(superuser_db, "partner")
    add_family_member(
        superuser_db,
        actor_id=FOUNDER_USER_ID,
        principal_id=partner.id,
        relationship=FamilyRelationship.PARTNER,
        display_name="Partner",
    )
    assert _authorize_as(superuser_db, partner, capability_key="family_calendar.read").allowed is False
    for key in ("family_calendar.read", "family_calendar.create", "family_calendar.update"):
        req = request_family_capability(superuser_db, principal_id=partner.id, capability_key=key, consequences="family schedule changes")
        decide_approval(superuser_db, actor_id=FOUNDER_USER_ID, request_id=req.id, mode=ApprovalMode.ALWAYS_ALLOW)
        assert _authorize_as(superuser_db, partner, capability_key=key).allowed is True
    assert _authorize_as(superuser_db, partner, capability_key="founder_private_calendar.read").verdict is AuthorizationVerdict.DENIED_FOUNDER_ONLY
    assert _authorize_as(superuser_db, partner, capability_key="mainai.policy.change").verdict is AuthorizationVerdict.DENIED_FOUNDER_ONLY


def test_child_once_then_again_requires_approval(superuser_db):
    _ensure_founder(superuser_db)
    child = _family_user(superuser_db, "child")
    add_family_member(
        superuser_db,
        actor_id=FOUNDER_USER_ID,
        principal_id=child.id,
        relationship=FamilyRelationship.CHILD,
        display_name="Child X",
    )
    req = request_family_capability(
        superuser_db,
        principal_id=child.id,
        capability_key="family_calendar.create_event",
        requested_data={"title": "Football training"},
        consequences="adds one family calendar event",
    )
    ctx = approval_context(req, who_label="Child X")
    assert ctx.who_principal_id == child.id
    assert ctx.resource == "family_calendar"
    assert "Football" in ctx.requested_data["title"]
    assert ctx.snapshot_hash == req.snapshot_hash
    decide_approval(superuser_db, actor_id=FOUNDER_USER_ID, request_id=req.id, mode=ApprovalMode.ALLOW_ONCE)
    first = _authorize_as(superuser_db, child, capability_key="family_calendar.create_event")
    assert first.allowed is True
    second = _authorize_as(superuser_db, child, capability_key="family_calendar.create_event")
    assert second.allowed is False
    assert second.verdict is AuthorizationVerdict.DENIED_EXHAUSTED


def test_allow_once_two_thread_race(superuser_db):
    _ensure_founder(superuser_db)
    child = _family_user(superuser_db, "race-child")
    add_family_member(
        superuser_db, actor_id=FOUNDER_USER_ID, principal_id=child.id,
        relationship=FamilyRelationship.CHILD, display_name="R",
    )
    req = request_family_capability(superuser_db, principal_id=child.id, capability_key="tv.control")
    decide_approval(superuser_db, actor_id=FOUNDER_USER_ID, request_id=req.id, mode=ApprovalMode.ALLOW_ONCE)
    superuser_db.commit()
    allowed = []
    lock = threading.Lock()

    def _use():
        session = sessionmaker(bind=migration_engine)()
        try:
            session.execute(text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(child.id)})
            result = authorize_capability(session, principal_id=child.id, capability_key="tv.control")
            session.commit()
            with lock:
                allowed.append(result.allowed)
        except Exception:
            session.rollback()
            with lock:
                allowed.append(False)
        finally:
            session.close()

    threads = [threading.Thread(target=_use) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert allowed.count(True) == 1
    assert allowed.count(False) == 1


def test_child_lights_do_not_imply_purchases(superuser_db):
    _ensure_founder(superuser_db)
    child = _family_user(superuser_db, "child-lights")
    add_family_member(
        superuser_db, actor_id=FOUNDER_USER_ID, principal_id=child.id, relationship=FamilyRelationship.CHILD, display_name="Kid"
    )
    req = request_family_capability(superuser_db, principal_id=child.id, capability_key="lights.control")
    decide_approval(superuser_db, actor_id=FOUNDER_USER_ID, request_id=req.id, mode=ApprovalMode.ALWAYS_ALLOW)
    assert _authorize_as(superuser_db, child, capability_key="lights.control").allowed is True
    assert _authorize_as(superuser_db, child, capability_key="purchases.create").allowed is False


def test_approval_action_and_data_and_limits_swap_denied(superuser_db):
    _ensure_founder(superuser_db)
    child = _family_user(superuser_db, "swap")
    add_family_member(
        superuser_db, actor_id=FOUNDER_USER_ID, principal_id=child.id,
        relationship=FamilyRelationship.CHILD, display_name="S",
    )
    req = request_family_capability(
        superuser_db,
        principal_id=child.id,
        capability_key="lights.control",
        requested_data={"room": "kitchen"},
        requested_limits={"no_external_invites": True},
        consequences="toggle kitchen lights",
    )
    seen = req.snapshot_hash
    _attack(
        superuser_db,
        "UPDATE family_approval_requests SET capability_key = 'purchases.create', action = 'create', resource = 'purchases' WHERE id = :id",
        {"id": str(req.id)},
    )
    _attack(
        superuser_db,
        "UPDATE family_approval_requests SET requested_data = :data::jsonb WHERE id = :id",
        {"id": str(req.id), "data": '{"amount": 9000}'},
    )
    _attack(
        superuser_db,
        "UPDATE family_approval_requests SET requested_limits = :data::jsonb WHERE id = :id",
        {"id": str(req.id), "data": '{"max_amount": 99999}'},
    )
    receipt = decide_approval(
        superuser_db, request_id=req.id, mode=ApprovalMode.ALWAYS_ALLOW, expected_snapshot_hash=seen,
    )
    assert receipt.snapshot_hash == seen
    cal = request_family_capability(
        superuser_db,
        principal_id=child.id,
        capability_key="calendar.create",
        requested_limits={"no_external_invites": True},
        consequences="family calendar only",
    )
    decide_approval(superuser_db, request_id=cal.id, mode=ApprovalMode.ALWAYS_ALLOW)
    assert _authorize_as(
        superuser_db, child, capability_key="calendar.create", resource="family",
        data={"title": "dinner"}, limits={"no_external_invites": True},
    ).allowed is True
    assert _authorize_as(
        superuser_db, child, capability_key="calendar.create", resource="private",
    ).verdict is AuthorizationVerdict.DENIED_RESOURCE
    assert _authorize_as(
        superuser_db, child, capability_key="calendar.create", resource="family",
        data={"external_invites": True},
    ).verdict is AuthorizationVerdict.DENIED_LIMITS


def test_revoke_and_expire_and_wrong_person(superuser_db):
    _ensure_founder(superuser_db)
    partner = _family_user(superuser_db, "partner-rev")
    stranger = _family_user(superuser_db, "stranger")
    add_family_member(
        superuser_db, actor_id=FOUNDER_USER_ID, principal_id=partner.id, relationship=FamilyRelationship.PARTNER, display_name="P"
    )
    req = request_family_capability(superuser_db, principal_id=partner.id, capability_key="shopping_list.update")
    decide_approval(superuser_db, actor_id=FOUNDER_USER_ID, request_id=req.id, mode=ApprovalMode.ALWAYS_ALLOW)
    grant = superuser_db.query(FamilyCapabilityGrant).filter_by(principal_id=partner.id, capability_key="shopping_list.update").one()
    revoke_grant(superuser_db, actor_id=FOUNDER_USER_ID, grant_id=grant.id)
    assert _authorize_as(superuser_db, partner, capability_key="shopping_list.update").verdict is AuthorizationVerdict.DENIED_REVOKED
    req2 = request_family_capability(superuser_db, principal_id=partner.id, capability_key="family_calendar.read")
    decide_approval(
        superuser_db,
        actor_id=FOUNDER_USER_ID,
        request_id=req2.id,
        mode=ApprovalMode.ALLOW_FOR_DURATION,
        duration=timedelta(minutes=5),
    )
    past = datetime.now(timezone.utc) + timedelta(minutes=6)
    assert _authorize_as(
        superuser_db, partner, capability_key="family_calendar.read", now=past
    ).verdict is AuthorizationVerdict.DENIED_EXPIRED
    assert _authorize_as(superuser_db, stranger, capability_key="family_calendar.read").verdict is AuthorizationVerdict.DENIED_WRONG_PRINCIPAL


def test_revoked_grant_cannot_be_reactivated_or_widened(superuser_db):
    _ensure_founder(superuser_db)
    child = _family_user(superuser_db, "rev-widen")
    add_family_member(
        superuser_db, actor_id=FOUNDER_USER_ID, principal_id=child.id,
        relationship=FamilyRelationship.CHILD, display_name="X",
    )
    req = request_family_capability(superuser_db, principal_id=child.id, capability_key="lights.control")
    decide_approval(superuser_db, request_id=req.id, mode=ApprovalMode.ALLOW_ONCE)
    grant = superuser_db.query(FamilyCapabilityGrant).filter_by(principal_id=child.id, capability_key="lights.control").one()
    revoke_grant(superuser_db, grant_id=grant.id)
    _attack(
        superuser_db,
        "UPDATE family_capability_grants SET revoked_at = NULL, remaining_uses = 9, capability_key = 'purchases.create' WHERE id = :id",
        {"id": str(grant.id)},
    )
    grant = superuser_db.get(FamilyCapabilityGrant, grant.id)
    assert grant is not None
    assert grant.revoked_at is not None
    assert grant.capability_key == "lights.control"


def test_copied_token_and_old_receipt_fail(superuser_db):
    _ensure_founder(superuser_db)
    child = _family_user(superuser_db, "child-token")
    add_family_member(
        superuser_db, actor_id=FOUNDER_USER_ID, principal_id=child.id, relationship=FamilyRelationship.CHILD, display_name="X"
    )
    req = request_family_capability(superuser_db, principal_id=child.id, capability_key="tv.control")
    receipt = decide_approval(superuser_db, actor_id=FOUNDER_USER_ID, request_id=req.id, mode=ApprovalMode.ALWAYS_ALLOW)
    assert authorize_with_copied_token(token="copied-from-notification").verdict is AuthorizationVerdict.DENIED_COPIED_TOKEN
    assert authorize_with_receipt(
        superuser_db, receipt_id=receipt.id, principal_id=child.id, capability_key="tv.control"
    ).verdict is AuthorizationVerdict.DENIED_OLD_RECEIPT


def test_deny_and_block_future_requests(superuser_db):
    _ensure_founder(superuser_db)
    child = _family_user(superuser_db, "child-block")
    add_family_member(
        superuser_db, actor_id=FOUNDER_USER_ID, principal_id=child.id, relationship=FamilyRelationship.CHILD, display_name="X"
    )
    req = request_family_capability(superuser_db, principal_id=child.id, capability_key="purchases.create")
    decide_approval(superuser_db, actor_id=FOUNDER_USER_ID, request_id=req.id, mode=ApprovalMode.DENY_AND_BLOCK)
    with pytest.raises(SovereigntyError, match="blocked"):
        request_family_capability(superuser_db, principal_id=child.id, capability_key="purchases.create")


def test_userai_boundary_blocks_founder_surfaces(superuser_db):
    _ensure_founder(superuser_db)
    define_userai_boundary(superuser_db, actor_id=FOUNDER_USER_ID)
    from app.mainai_founder_sovereignty.service import assert_userai_boundary

    with pytest.raises(SovereigntyError, match="UserAI"):
        assert_userai_boundary(superuser_db, tenant_kind=TenantKind.USERAI_PERSONAL, surface="founder_secrets")
    with pytest.raises(SovereigntyError, match="UserAI"):
        assert_userai_boundary(superuser_db, tenant_kind=TenantKind.USERAI_PERSONAL, surface="founder_authority")
    assert_userai_boundary(superuser_db, tenant_kind=TenantKind.FOUNDER_MAINAI, surface="founder_secrets")
    _attack(
        superuser_db,
        """
        INSERT INTO userai_tenant_boundaries (owner_id, tenant_kind, memory_root, policy_root)
        VALUES (:fid, 'userai_personal', 'founder_private_memory', 'founder_policies')
        """,
        {"fid": str(FOUNDER_USER_ID)},
    )


def test_family_member_cannot_be_founder_identity(superuser_db):
    _ensure_founder(superuser_db)
    with pytest.raises(SovereigntyError, match="FAMILY MEMBER"):
        add_family_member(
            superuser_db,
            actor_id=FOUNDER_USER_ID,
            principal_id=FOUNDER_USER_ID,
            relationship=FamilyRelationship.PARTNER,
            display_name="Nope",
        )


def test_family_capability_cannot_become_founder_capability(superuser_db):
    _ensure_founder(superuser_db)
    child = _family_user(superuser_db, "cap-founder")
    add_family_member(
        superuser_db, actor_id=FOUNDER_USER_ID, principal_id=child.id,
        relationship=FamilyRelationship.CHILD, display_name="X",
    )
    with pytest.raises(SovereigntyError):
        request_family_capability(superuser_db, principal_id=child.id, capability_key="mainai.policy.change")
    superuser_db.execute(
        text(
            """
            INSERT INTO family_capability_grants
                (owner_id, principal_id, capability_key, resource, action, scope, duration_mode, issuer_id)
            VALUES (:fid, :pid, 'mainai.policy.change', 'mainai_policy', 'change', 'family', 'always_allow', :fid)
            """
        ),
        {"fid": str(FOUNDER_USER_ID), "pid": str(child.id)},
    )
    with pytest.raises(Exception):
        superuser_db.flush()
    superuser_db.rollback()


def test_remote_approval_list_is_founder_only(superuser_db):
    _ensure_founder(superuser_db)
    child = _family_user(superuser_db, "child-remote")
    add_family_member(
        superuser_db, actor_id=FOUNDER_USER_ID, principal_id=child.id, relationship=FamilyRelationship.CHILD, display_name="X"
    )
    request_family_capability(
        superuser_db,
        principal_id=child.id,
        capability_key="family_calendar.create_event",
        requested_data={"title": "Football training"},
        consequences="adds one family calendar event",
    )
    pending = list_pending_approvals(superuser_db, actor_id=FOUNDER_USER_ID)
    assert len(pending) == 1
    stranger = _family_user(superuser_db, "not-founder")
    _bind_session(superuser_db, stranger.id)
    with pytest.raises(SovereigntyError):
        list_pending_approvals(superuser_db, actor_id=stranger.id)


def test_live_founder_remote_approval_endpoint(client, superuser_db):
    _ensure_founder(superuser_db)
    child = _family_user(superuser_db, "child-api")
    add_family_member(
        superuser_db, actor_id=FOUNDER_USER_ID, principal_id=child.id, relationship=FamilyRelationship.CHILD, display_name="X"
    )
    req = request_family_capability(
        superuser_db,
        principal_id=child.id,
        capability_key="family_calendar.create_event",
        requested_data={"title": "Football training"},
        consequences="adds one family calendar event",
    )
    superuser_db.commit()
    login = client.post("/api/auth/login", json={"email": "founder@lifeos.local", "password": "TestFounderPassword123!"})
    assert login.status_code == 200, login.text
    csrf = login.json()["csrf_token"]
    listed = client.get("/api/founder-sovereignty/approvals", headers={"X-CSRF-Token": csrf})
    assert listed.status_code == 200, listed.text
    assert any(item["id"] == str(req.id) for item in listed.json())
    assert any(item.get("snapshot_hash") for item in listed.json() if item["id"] == str(req.id))
    decided = client.post(
        f"/api/founder-sovereignty/approvals/{req.id}/decide",
        json={"mode": "allow_once", "expected_snapshot_hash": req.snapshot_hash},
        headers={"X-CSRF-Token": csrf},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["decision"] == "allow_once"


def test_history_cannot_be_rewritten(superuser_db):
    _ensure_founder(superuser_db)
    version = apply_founder_policy(
        superuser_db,
        actor_id=FOUNDER_USER_ID,
        actor_kind=ActorKind.FOUNDER,
        policy_key="model_preferences",
        payload={"provider": "local"},
        reason="v1",
    )
    version.payload = {"provider": "rewritten"}
    with pytest.raises(Exception):
        superuser_db.flush()
    superuser_db.rollback()


def test_founder_erasure_clears_policy_history_and_grants(superuser_db):
    _ensure_founder(superuser_db)
    apply_founder_policy(
        superuser_db, actor_kind=ActorKind.FOUNDER, policy_key="erase_rules", payload={"v": 1}, reason="keep"
    )
    child = _family_user(superuser_db, "erase-child")
    add_family_member(
        superuser_db, principal_id=child.id, relationship=FamilyRelationship.CHILD, display_name="E",
    )
    req = request_family_capability(superuser_db, principal_id=child.id, capability_key="tv.control")
    decide_approval(superuser_db, request_id=req.id, mode=ApprovalMode.ALWAYS_ALLOW)
    assert superuser_db.query(FounderPolicyVersion).filter_by(policy_key="erase_rules").count() == 1
    assert superuser_db.query(FamilyCapabilityGrant).count() >= 1
    superuser_db.execute(text("SELECT erase_own_founder_sovereignty_children()"))
    superuser_db.expire_all()
    assert superuser_db.query(FounderPolicyVersion).filter_by(owner_id=FOUNDER_USER_ID).count() == 0
    assert superuser_db.query(FamilyCapabilityGrant).filter_by(owner_id=FOUNDER_USER_ID).count() == 0
    assert superuser_db.query(FounderInstanceBinding).count() == 0
    kernel = superuser_db.execute(text("SELECT count(*) FROM kernel_security_invariants")).scalar()
    assert kernel >= 1


def test_cross_owner_family_rows_are_isolated(superuser_db, db_session):
    _ensure_founder(superuser_db)
    child = _family_user(superuser_db, "iso-child")
    add_family_member(
        superuser_db, principal_id=child.id, relationship=FamilyRelationship.CHILD, display_name="I",
    )
    req = request_family_capability(superuser_db, principal_id=child.id, capability_key="tv.control")
    decide_approval(superuser_db, request_id=req.id, mode=ApprovalMode.ALWAYS_ALLOW)
    superuser_db.commit()
    db_session.execute(text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(child.id)})
    visible = db_session.execute(text("SELECT count(*) FROM family_capability_grants")).scalar()
    assert visible == 0


def test_personal_recall_stays_disabled_and_identity_is_recomputed():
    identity = personal_recall_production_identity()
    assert len(identity) == 40
    assert identity != "1e58046a7435cfead15fd34414808243f16082f8"
    from app.personal_recall.routes_prep import build_recall_router

    assert inspect.signature(build_recall_router).parameters["enabled"].default is False
    print(f"PERSONAL_RECALL_IDENTITY={identity}")
