"""Lane D — composed systemic workforce attacks + staff loop + first team honesty."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

from app.models.user import User
from app.workforce import (
    FailureTakeoverError,
    ProviderActivationBlocked,
    TaskScopedAuthority,
    VerificationError,
    activation_gate_status,
    alternate_agent_takeover,
    apply_verification_decision,
    bootstrap_first_team,
    cancel_assignment,
    decide_staffing,
    apply_staffing_decision,
    execute_workforce_assignment,
    inspect_first_team,
    mark_failure,
    mark_safety_gate,
    register_workforce_agent,
    reset_safety_gates_for_tests,
    resolve_delegation,
    retire_workforce_agent,
    scrub_authority_mutations,
    submit_delegation_request,
)
from app.workforce.provider_worker import assert_no_credentials_in_context, build_worker_request_envelope
from app.models.workforce import WorkforceContextPackage


def _owner(db):
    u = User(email=f"sys-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(u)
    db.flush()
    return u


def _agents(db, owner_id, suffix):
    a = register_workforce_agent(
        db,
        owner_id=owner_id,
        agent_key=f"a{suffix}",
        name="A",
        role="builder",
        agent_type="CODING",
        capability_tags=["low_risk_classification"],
        allowed_tool_classes=["read_excerpt"],
        trust_zone="LOCAL_INTERNAL",
        status="active",
        cost_class="low",
    )
    b = register_workforce_agent(
        db,
        owner_id=owner_id,
        agent_key=f"b{suffix}",
        name="B",
        role="builder",
        agent_type="CODING",
        capability_tags=["low_risk_classification"],
        allowed_tool_classes=["read_excerpt"],
        trust_zone="LOCAL_INTERNAL",
        status="active",
        cost_class="low",
    )
    v = register_workforce_agent(
        db,
        owner_id=owner_id,
        agent_key=f"v{suffix}",
        name="V",
        role="verifier",
        agent_type="VERIFIER",
        capability_tags=["verification"],
        allowed_tool_classes=["read_excerpt"],
        trust_zone="LOCAL_INTERNAL",
        status="active",
    )
    return a, b, v


def test_composed_fail_takeover_verify_ledger(superuser_db):
    """MainAI→A fails→B takeover→minimized context→no widen→independent verify→ledger."""
    owner = _owner(superuser_db)
    a, b, v = _agents(superuser_db, owner.id, "-cmp")
    req = submit_delegation_request(
        superuser_db,
        owner_id=owner.id,
        goal_text="classify",
        required_capability="low_risk_classification",
        verification_requirement="independent_verifier",
    )
    asg = resolve_delegation(
        superuser_db,
        owner_id=owner.id,
        request=req,
        context_items=[{"kind": "excerpt", "excerpt": "Dentist Friday"}, {"kind": "vault", "ref": "x"}],
        authority=TaskScopedAuthority(
            allowed_read_paths=("notes/**",),
            allowed_write_paths=("notes/out/**",),
            allowed_tool_classes=("read_excerpt",),
            allow_execution_effects=False,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        ),
        verifier_profile_id=v.id,
    )
    mark_failure(
        superuser_db,
        owner_id=owner.id,
        assignment=asg,
        failure_class="agent_crash",
        external_effect_state="none_proven",
        partial_result={"progress": 10},
    )
    asg2 = alternate_agent_takeover(
        superuser_db,
        owner_id=owner.id,
        failed_assignment=asg,
        request=req,
        alternate_profile_id=b.id,
        verifier_profile_id=v.id,
        context_items=[{"kind": "excerpt", "excerpt": "Dentist Friday"}],  # minimized again
    )
    assert asg2.allow_execution_effects is False
    assert asg2.allowed_write_paths == []  # no widen
    assert asg2.provenance["authority_widened"] is False

    receipt = execute_workforce_assignment(
        superuser_db,
        owner_id=owner.id,
        assignment=asg2,
        goal_text="classify",
        capability="low_risk_classification",
        activate_provider=False,
    )
    assert receipt.provider_invoked is False
    assert asg2.verification_status == "UNVERIFIED"

    with pytest.raises(VerificationError):
        apply_verification_decision(
            superuser_db,
            owner_id=owner.id,
            assignment=asg2,
            decision="VERIFIED",
            risk="low",
            verifier_profile_id=b.id,  # builder self-verify
        )
    apply_verification_decision(
        superuser_db,
        owner_id=owner.id,
        assignment=asg2,
        decision="VERIFIED",
        risk="low",
        verifier_profile_id=v.id,
    )
    superuser_db.commit()
    assert asg2.verification_status == "VERIFIED"


def test_unknown_effect_blocks_takeover_duplicate(superuser_db):
    owner = _owner(superuser_db)
    a, b, v = _agents(superuser_db, owner.id, "-unk")
    req = submit_delegation_request(
        superuser_db,
        owner_id=owner.id,
        goal_text="x",
        required_capability="low_risk_classification",
        verification_requirement="independent_verifier",
    )
    asg = resolve_delegation(superuser_db, owner_id=owner.id, request=req, verifier_profile_id=v.id)
    mark_failure(
        superuser_db,
        owner_id=owner.id,
        assignment=asg,
        failure_class="provider_timeout",
        external_effect_state="unknown",
    )
    with pytest.raises(FailureTakeoverError):
        alternate_agent_takeover(
            superuser_db,
            owner_id=owner.id,
            failed_assignment=asg,
            request=req,
            alternate_profile_id=b.id,
            verifier_profile_id=v.id,
        )


def test_retire_and_revoke_while_assigned(superuser_db):
    owner = _owner(superuser_db)
    a, b, v = _agents(superuser_db, owner.id, "-rev")
    req = submit_delegation_request(
        superuser_db,
        owner_id=owner.id,
        goal_text="x",
        required_capability="low_risk_classification",
        verification_requirement="independent_verifier",
    )
    asg = resolve_delegation(superuser_db, owner_id=owner.id, request=req, verifier_profile_id=v.id)
    retire_workforce_agent(superuser_db, owner_id=owner.id, agent_id=a.id)
    cancel_assignment(superuser_db, owner_id=owner.id, assignment=asg, reason="authority_revoked_mid_run")
    superuser_db.commit()
    with pytest.raises(Exception):
        execute_workforce_assignment(
            superuser_db,
            owner_id=owner.id,
            assignment=asg,
            goal_text="x",
            capability="low_risk_classification",
        )


def test_provider_activation_blocked_until_gates(superuser_db):
    reset_safety_gates_for_tests()
    owner = _owner(superuser_db)
    a, b, v = _agents(superuser_db, owner.id, "-gate")
    req = submit_delegation_request(
        superuser_db,
        owner_id=owner.id,
        goal_text="x",
        required_capability="low_risk_classification",
        verification_requirement="independent_verifier",
    )
    asg = resolve_delegation(superuser_db, owner_id=owner.id, request=req, verifier_profile_id=v.id)
    with pytest.raises(ProviderActivationBlocked):
        execute_workforce_assignment(
            superuser_db,
            owner_id=owner.id,
            assignment=asg,
            goal_text="x",
            capability="low_risk_classification",
            activate_provider=True,
        )
    # Mark all gates — still staged (no accidental real invoke)
    for g in activation_gate_status():
        mark_safety_gate(g, satisfied=True)
    with pytest.raises(ProviderActivationBlocked):
        execute_workforce_assignment(
            superuser_db,
            owner_id=owner.id,
            assignment=asg,
            goal_text="x",
            capability="low_risk_classification",
            activate_provider=True,
        )
    reset_safety_gates_for_tests()


def test_credentials_never_in_worker_envelope(superuser_db):
    owner = _owner(superuser_db)
    a, _, v = _agents(superuser_db, owner.id, "-cred")
    req = submit_delegation_request(
        superuser_db,
        owner_id=owner.id,
        goal_text="x",
        required_capability="low_risk_classification",
        verification_requirement="independent_verifier",
    )
    asg = resolve_delegation(
        superuser_db,
        owner_id=owner.id,
        request=req,
        verifier_profile_id=v.id,
        context_items=[{"kind": "excerpt", "excerpt": "hi"}],
    )
    pkg = superuser_db.get(WorkforceContextPackage, asg.context_package_id)
    env = build_worker_request_envelope(
        assignment=asg,
        profile=a,
        package=pkg,
        goal_text="x",
        capability="low_risk_classification",
        credential_reference="ref:openai:default",
    )
    assert "sk-" not in str(env)
    assert env.credential_reference == "ref:openai:default"
    with pytest.raises(Exception):
        assert_no_credentials_in_context([{"api_key": "sk-secret"}])


def test_fake_evidence_and_policy_mutation_scrubbed(superuser_db):
    cleaned, stripped = scrub_authority_mutations(
        {
            "evidence": {"passed": True},
            "fake_evidence": True,
            "modify_system_policy": {"allow": "all"},
            "set_verification_status": "VERIFIED",
            "grant_authority": True,
        }
    )
    assert "modify_system_policy" in stripped
    assert "set_verification_status" in stripped
    assert "grant_authority" in stripped


def test_first_team_honest_no_fake_runtime(superuser_db):
    owner = _owner(superuser_db)
    bootstrap_first_team(superuser_db, owner_id=owner.id)
    snap = inspect_first_team(superuser_db, owner_id=owner.id)
    superuser_db.commit()
    assert len(snap["departments"]) == 9
    for d in snap["departments"]:
        assert d["authority_on_create"] is False
        assert d["what_actually_exists"]["evidence_proves_capability"] is False
        assert d["status"] == "candidate"
        assert d["allowed_tool_classes"] == []


def test_staff_manager_prefers_existing_and_refuses_endless_hire(superuser_db):
    owner = _owner(superuser_db)
    a, _, _ = _agents(superuser_db, owner.id, "-staff")
    # Seed some performance so use_existing wins
    from app.workforce.performance import record_verified_outcome

    record_verified_outcome(
        superuser_db,
        owner_id=owner.id,
        profile_id=a.id,
        capability_tag="low_risk_classification",
        success=True,
    )
    d = decide_staffing(
        superuser_db,
        owner_id=owner.id,
        need_capability="low_risk_classification",
        need_summary="classify notes",
    )
    assert d.action == "use_existing"
    assert d.profile_id == a.id

    # New capability, not recurring → refuse
    d2 = decide_staffing(
        superuser_db,
        owner_id=owner.id,
        need_capability="quantum_knitting",
        need_summary="one-off",
        material=False,
    )
    assert d2.action == "refuse"

    # Material need → create candidate at probation
    d3 = decide_staffing(
        superuser_db,
        owner_id=owner.id,
        need_capability="quantum_knitting",
        need_summary="recurring material",
        material=True,
        complexity_cost=0.1,
    )
    assert d3.action == "create_candidate"
    applied = apply_staffing_decision(
        superuser_db,
        owner_id=owner.id,
        decision=d3,
        need_capability="quantum_knitting",
        need_summary="recurring material",
    )
    superuser_db.commit()
    assert applied.profile_id is not None
    from app.workforce.registry import get_workforce_agent

    p = get_workforce_agent(superuser_db, owner_id=owner.id, agent_id=applied.profile_id)
    assert p.status == "probation"
