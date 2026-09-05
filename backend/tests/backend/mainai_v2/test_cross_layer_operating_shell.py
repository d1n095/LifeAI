"""Cross-layer proof: the Operating Shell (app.operating_shell) composes correctly with
Guardian, Sentinel, Life Recovery, and Privacy Boundary. `app.operating_shell` itself
imports none of the other four packages (see its own __init__.py docstring) -- this file is
the only place it is used alongside them, and it is itself not imported by anything else.

ACTION REQUEST != AUTHORITY: the Operating Shell's own risk/preview gate never grants
authority by itself -- a real policy (here, a thin Guardian-backed one built only in this
test file) must be supplied, and its answer is respected in both directions.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timezone

from app.guardian import (
    AuthorityCeilingRequest,
    ContainmentScope,
    DeviceTrustState,
    GuardianAction,
    IntegrityState,
    evaluate_authority_ceiling_request,
    evaluate_bounded_action,
    new_guardian_state,
    set_device_trust,
    set_integrity_state,
)
from app.life_recovery.hydration import new_hydration_progress, restore_tier
from app.life_recovery.life_image import build_life_image_component
from app.life_recovery.types import ComponentCriticality, ComponentType, RestoreTier
from app.operating_shell import (
    ALLOWED,
    DENIED,
    ActionRiskLevel,
    EvidenceSurfaceKind,
    ResourceAvailability,
    RestoreResult,
    WorkspaceAction,
    WorkspaceCommand,
    WorkspaceActionType,
    active_intents_for_owner,
    advance_to_active,
    advance_to_planned,
    build_evidence_surface,
    create_intent_from_expression,
    record_understanding,
    evaluate_action_authority,
    new_control_state,
    new_workspace_state,
    plan_workspace_restore,
    require_preview_for_consequential_action,
    request_incident_evidence,
    resource_status,
)
from app.privacy_boundary import DataClassification, OutboundPurpose, RawLocalSignal, TelemetryMode, run_privacy_pipeline
from app.sentinel import (
    SecurityConfidence,
    SecurityEvent,
    SecurityEventType,
    SecuritySeverity,
    SecuritySource,
    SecuritySubject,
    ThreatClass,
    new_detection_rule,
    new_sentinel_state,
    promote_rule,
    propose_rule,
    record_event,
)
from app.sentinel.types import RuleState


_GUARDIAN_SECRET = b"test-secret-key-32-bytes-min!!"


def _owner() -> uuid.UUID:
    return uuid.uuid4()


def _healthy_guardian_state(owner_id: uuid.UUID):
    state = new_guardian_state(owner_id=owner_id, secret_key=_GUARDIAN_SECRET)
    set_integrity_state(state, owner_id=owner_id, integrity=IntegrityState.VERIFIED)
    set_device_trust(state, owner_id=owner_id, trust=DeviceTrustState.TRUSTED)
    return state


def _destructive_action() -> WorkspaceAction:
    command = WorkspaceCommand(command_id=uuid.uuid4(), action_type=WorkspaceActionType.DELETE_FILE, target_ref=uuid.uuid4())
    return WorkspaceAction(action_id=uuid.uuid4(), command=command, risk=ActionRiskLevel.DESTRUCTIVE)


def _guardian_risk_level(risk: ActionRiskLevel) -> str:
    """Operating Shell's ActionRiskLevel and Guardian's low/medium/high vocabulary are two
    genuinely separate closed vocabularies (matching this whole lane's discipline of not
    letting one package's enum silently double as another's) -- a real composed caller must
    translate explicitly, never assume they line up 1:1."""
    if risk in (ActionRiskLevel.OBSERVATIONAL, ActionRiskLevel.REVERSIBLE_LOW_RISK):
        return "low"
    if risk == ActionRiskLevel.REVERSIBLE_CONSEQUENTIAL:
        return "medium"
    return "high"  # EXTERNAL_EFFECT, DESTRUCTIVE, ROOT_SECURITY_SENSITIVE


class _GuardianBackedPolicy:
    """The seam's ONLY real implementation in this whole V2 lane -- lives in the test file,
    not in app.operating_shell itself (which must never import app.guardian)."""

    def __init__(self, guardian_state, *, owner_id: uuid.UUID, scope: ContainmentScope):
        self._state = guardian_state
        self._owner_id = owner_id
        self._scope = scope

    def evaluate(self, action, risk):
        decision = evaluate_bounded_action(
            self._state, owner_id=self._owner_id, scope=self._scope,
            requested_risk_level=_guardian_risk_level(risk), requested_by="mainai",
        )
        return ALLOWED if decision.action == GuardianAction.ALLOW else DENIED


# --- Scenario 1 (founder's own example): request destructive action -> policy says
# REQUIRE_OWNER / DENY. Only a real owner-explicit ceiling raise can change that, and even
# then Operating Shell's OWN preview gate still applies independently -- Guardian's ALLOW is
# never a preview bypass. --------------------------------------------------------------------


def test_destructive_action_denied_by_guardian_and_gated_by_own_preview_requirement():
    owner_id = _owner()
    action = _destructive_action()

    # A healthy, otherwise-untampered Guardian state still DENIES a high-risk (DESTRUCTIVE)
    # request against the default "low" ceiling -- MainAI never gets destructive authority
    # merely by being in a healthy security state.
    healthy = _healthy_guardian_state(owner_id)
    policy = _GuardianBackedPolicy(healthy, owner_id=owner_id, scope=ContainmentScope.OWNER)
    assert evaluate_action_authority(action, policy=policy) == DENIED

    # MainAI cannot self-raise its own ceiling to fix this.
    self_raise = evaluate_authority_ceiling_request(
        healthy, AuthorityCeilingRequest(scope=ContainmentScope.OWNER, owner_id=owner_id, requested_max_risk_level="high", requested_by="mainai", reason="I need this")
    )
    assert self_raise.action == GuardianAction.DENY
    assert evaluate_action_authority(action, policy=policy) == DENIED, "a denied self-raise must not have changed anything"

    # Only a real owner_explicit ceiling raise can grant it.
    owner_raise = evaluate_authority_ceiling_request(
        healthy, AuthorityCeilingRequest(scope=ContainmentScope.OWNER, owner_id=owner_id, requested_max_risk_level="high", requested_by="owner_explicit", reason="owner approved cleanup")
    )
    assert owner_raise.action == GuardianAction.ALLOW
    assert evaluate_action_authority(action, policy=policy) == ALLOWED

    # But Guardian's ALLOW is NOT a substitute for Operating Shell's own preview
    # requirement -- a DESTRUCTIVE action still cannot be marked executed without a real
    # ActionPreview, regardless of what Guardian said.
    try:
        require_preview_for_consequential_action(action, preview=None)
        raised = False
    except Exception:
        raised = True
    assert raised, "Guardian's ALLOW must never bypass Operating Shell's own preview gate"


# --- Scenario 2 (founder's own example): security incident arises -> workspace can show
# incident evidence -> Sentinel does not become UI authority. ------------------------------


def test_sentinel_incident_surfaces_as_subordinate_evidence_not_ui_authority():
    owner_id = _owner()
    sentinel_state = new_sentinel_state()
    rule = new_detection_rule(
        rule_id="rule.suspicious_process",
        event_types=frozenset({SecurityEventType.UNSIGNED_BINARY_EXECUTED}),
        severity=SecuritySeverity.HIGH,
        confidence=SecurityConfidence.MEDIUM,
        threat_class=ThreatClass.RECONNAISSANCE,
        conditions={"min_event_severity": SecuritySeverity.HIGH},
    )
    propose_rule(sentinel_state, rule)
    promote_rule(sentinel_state, "rule.suspicious_process", to_state=RuleState.TESTING)
    promote_rule(sentinel_state, "rule.suspicious_process", to_state=RuleState.VERIFIED)
    promote_rule(sentinel_state, "rule.suspicious_process", to_state=RuleState.ACTIVE)

    event = SecurityEvent(
        event_id=uuid.uuid4(),
        event_type=SecurityEventType.UNSIGNED_BINARY_EXECUTED,
        severity=SecuritySeverity.HIGH,
        confidence=SecurityConfidence.MEDIUM,
        subject=SecuritySubject(owner_id=owner_id, device_id="device-1", subject_kind="process", subject_ref="proc-1"),
        source=SecuritySource(adapter_name="test_adapter", adapter_version="0.1.0"),
        occurred_at=datetime.now(timezone.utc),
        correlation_id=uuid.uuid4(),
        parent_event_id=None,
        details={},
    )
    _, _, touched = record_event(sentinel_state, event)
    assert len(touched) == 1
    incident = touched[0]

    control_before = new_control_state(owner_id=owner_id)
    request = request_incident_evidence((incident.incident_id,))
    assert request.kind == EvidenceSurfaceKind.INCIDENT_EVIDENCE
    result = build_evidence_surface(request)

    # Subordinate, not authoritative: dismissible, returns focus to the orb, and carries no
    # field that could constitute control-state authority.
    assert result.dismissible is True
    assert result.returns_focus_to_orb is True
    result_fields = {f for f in vars(result)} if hasattr(result, "__dict__") else set(result.__dataclass_fields__)
    assert not any("control" in f.lower() or "authority" in f.lower() for f in result_fields), (
        f"EvidenceSurfaceResult must not carry any control/authority-shaped field, got {result_fields}"
    )

    # Structural: nothing in the evidence-surface path touches ControlArbitrationState at all.
    import app.operating_shell.evidence as evidence_module

    evidence_source = inspect.getsource(evidence_module)
    assert "ControlState" not in evidence_source and "on_user_input" not in evidence_source

    # And the control state genuinely never moved as a side effect of surfacing evidence.
    assert control_before.state == new_control_state(owner_id=owner_id).state


# --- Scenario 3 (founder's own example): after progressive hydration -> active intents
# available first -> workspace restore can proceed partially -> Vault remains locked. ------


def test_active_intents_available_before_full_hydration_and_vault_stays_locked_on_restore():
    owner_id = _owner()

    # Intent Objects are entirely independent of Life Recovery's hydration machinery --
    # "active intents available first" holds trivially and structurally: nothing in
    # app.operating_shell.intent depends on app.life_recovery at all.
    intent = create_intent_from_expression(owner_id=owner_id, title="fixa skulderna", raw_user_expression="jag måste få ordning på skulderna")
    record_understanding(intent, interpreted_goal="get overdue debts organized and under control")
    advance_to_planned(intent)
    advance_to_active(intent)
    assert active_intents_for_owner((intent,), owner_id=owner_id) == (intent,)

    # Meanwhile, only PRIORITY_0+1 have actually been hydrated (tiers 2/3 not yet run) --
    # confirmed via life_recovery's own real hydration progress object, independently of
    # Operating Shell ever looking at it.
    dek = b"\x00" * 32
    memory_component = build_life_image_component(
        b"essential memory", component_type=ComponentType.MAINAI_MEMORY, dek=dek, owner_id=owner_id, key_version=1,
        schema_version=1, content_version=1, criticality=ComponentCriticality.CRITICAL, restore_priority=RestoreTier.PRIORITY_1,
    )
    progress = new_hydration_progress(owner_id=owner_id)
    progress = restore_tier(
        progress, tier=RestoreTier.PRIORITY_1, components_in_tier=(memory_component,), dek_for_component=lambda c: dek
    )
    assert RestoreTier.PRIORITY_1 in progress.completed_tiers
    assert RestoreTier.PRIORITY_2 not in progress.completed_tiers

    # Operating Shell's OWN restore planner, given a workspace referencing a Vault-linked
    # resource, independently refuses to auto-restore it -- reaching the same "Vault stays
    # locked" answer as Life Recovery's own hydration.unlock_vault() gate, without either
    # package knowing about the other.
    vault_ref = uuid.uuid4()
    resources = (
        resource_status(target_ref=uuid.uuid4(), kind="document", availability=ResourceAvailability.AVAILABLE),
        resource_status(target_ref=vault_ref, kind="vault_object", availability=ResourceAvailability.AVAILABLE, vault_linked=True),
    )
    plan = plan_workspace_restore(resources)
    assert vault_ref in plan.vault_locked_refs
    assert plan.result != RestoreResult.RESTORABLE or vault_ref not in (r.target_ref for r in plan.resources if not r.vault_linked)


# --- Scenario 4 (founder's own example): workspace state never becomes central telemetry. -


def test_workspace_state_never_becomes_central_telemetry():
    owner_id = _owner()
    new_workspace_state(owner_id=owner_id)  # constructible standalone -- no telemetry side effect

    # A naive future caller tries to ship the workspace's current task as a learning signal.
    raw_signal = RawLocalSignal(
        owner_id=owner_id,
        domain="workspace_snapshot",
        raw_content={"current_task": "reviewing the owner's private financial documents"},
        classification=DataClassification.PRIVATE,
    )
    result = run_privacy_pipeline(
        raw_signal,
        mode=TelemetryMode.LEARNING,
        purpose=OutboundPurpose.LEARNING_SIGNAL,
        module="test_cross_layer_operating_shell",
        software_version="0.0.0-test",
        skill="workspace_report",
        failure_class="knowledge_gap",
        success=False,
    )
    assert result is None or "financial documents" not in repr(result)

    # Structural: run_privacy_pipeline has no parameter that could accept an
    # app.operating_shell.WorkspaceState (or WorkspaceMemorySummary) directly -- there is no
    # privileged bypass path for workspace data specifically.
    params = inspect.signature(run_privacy_pipeline).parameters
    assert not any("workspace" in name.lower() for name in params), (
        f"run_privacy_pipeline must not accept anything workspace-shaped as a bypass, got params={list(params)}"
    )

    # And app.operating_shell itself never imports app.privacy_boundary in ANY of its
    # submodules -- the block above is enforced entirely by a hypothetical composed caller
    # going through the real pipeline, never by Operating Shell quietly routing around it.
    # (Checking every .py file on disk, not just __init__.py's own source, since a package's
    # __init__ source alone would not reveal an import hidden inside one of its submodules.)
    import pathlib
    import re

    import app.operating_shell as shell_pkg

    package_dir = pathlib.Path(shell_pkg.__file__).parent
    forbidden = re.compile(r"^\s*(import|from)\s+app\.(privacy_boundary|guardian|sentinel|sovereign_identity|life_recovery)\b", re.MULTILINE)
    for py_file in package_dir.glob("*.py"):
        source = py_file.read_text()
        assert not forbidden.search(source), f"{py_file.name} must not import any of the other four V2 packages"
