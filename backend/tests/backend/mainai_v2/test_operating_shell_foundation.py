"""Operating Shell foundation tests (MainAI V2, Stages V2-I1..I7).

Pure in-memory, no DB/Postgres dependency. Standalone: does not import app.guardian,
app.privacy_boundary, app.sentinel, app.sovereign_identity, or app.life_recovery, and is
not imported by any production runtime path.
"""

from __future__ import annotations

import inspect
import uuid

import pytest

from app.operating_shell import (
    ActionRiskLevel,
    AmbiguousResolution,
    ConsequentialActionRequiresPreviewError,
    ControlState,
    IntentState,
    MalformedIntentSnapshotError,
    MalformedWorkspaceSnapshotError,
    PolicyNotWiredError,
    ReferenceKind,
    ResolvedReference,
    ResourceAvailability,
    RestoreResult,
    UserInputSignal,
    WorkspaceAction,
    WorkspaceActionType,
    WorkspaceCommand,
    WorkspaceContext,
    WorkspaceFocus,
    WorkspaceSecretShapedContentError,
    WorkspaceTarget,
    WorkspaceWindow,
    active_intents_for_owner,
    advance_to_active,
    advance_to_planned,
    aggregate_for_user,
    build_action_preview,
    build_action_result,
    classify_action_risk,
    create_intent_from_expression,
    detect_dependency_cycle,
    end_takeover,
    evaluate_action_authority,
    find_missing_dependencies,
    find_window_by_title,
    has_authority_from_next_action,
    intent_from_snapshot,
    intent_to_snapshot,
    list_windows_for_owner,
    new_control_state,
    new_workspace_state,
    on_user_input,
    plan_workspace_restore,
    record_delegation_result,
    record_recent_command,
    record_understanding,
    reject_secret_shaped_content,
    resolve_intent_by_title_fragment,
    resolve_reference,
    resource_status,
    resume_from_current_state,
    require_root_sensitive_policy,
    set_current_task,
    supersede_intent,
    workspace_from_snapshot,
    workspace_to_snapshot,
)


def _owner() -> uuid.UUID:
    return uuid.uuid4()


def _action(action_type: WorkspaceActionType, *, target_ref: uuid.UUID | None = None) -> WorkspaceAction:
    return WorkspaceAction(
        action_id=uuid.uuid4(),
        command=WorkspaceCommand(command_id=uuid.uuid4(), action_type=action_type, target_ref=target_ref),
        risk=None,  # forces fallback to DEFAULT_ACTION_RISK
    )


# 1. manual input preempts MainAI -----------------------------------------------------------


def test_manual_input_preempts_mainai():
    owner_id = _owner()
    state = new_control_state(owner_id=owner_id)
    assert state.state == ControlState.MAINAI_CONTROL
    on_user_input(state, UserInputSignal(signal_id=uuid.uuid4(), kind="mouse"))
    assert state.state == ControlState.USER_TAKEOVER


def test_manual_input_preempts_mainai_three_check():
    """Three-check: confirm on_user_input() genuinely transitions state -- if the
    assignment were dropped (a plausible careless bug: computing the new state but not
    writing it back), this test would fail."""
    import app.operating_shell.control as control_module

    owner_id = _owner()
    state = new_control_state(owner_id=owner_id)
    original = control_module.on_user_input

    def broken(state, signal, **kwargs):
        return state  # deliberately never sets state.state

    control_module.on_user_input = broken
    try:
        control_module.on_user_input(state, UserInputSignal(signal_id=uuid.uuid4(), kind="mouse"))
        assert state.state == ControlState.MAINAI_CONTROL, "sanity: broken version must fail to preempt"
    finally:
        control_module.on_user_input = original

    state2 = new_control_state(owner_id=owner_id)
    control_module.on_user_input(state2, UserInputSignal(signal_id=uuid.uuid4(), kind="mouse"))
    assert state2.state == ControlState.USER_TAKEOVER


def test_manual_input_preempts_mainai_even_with_in_flight_action():
    owner_id = _owner()
    state = new_control_state(owner_id=owner_id)
    action = _action(WorkspaceActionType.MOVE_WINDOW, target_ref=uuid.uuid4())
    on_user_input(state, UserInputSignal(signal_id=uuid.uuid4(), kind="keyboard"), in_flight_action=action, workspace_snapshot_ref=uuid.uuid4())
    assert state.state == ControlState.USER_TAKEOVER
    assert state.paused_action is not None
    assert state.paused_action.action.action_id == action.action_id  # USER TAKEOVER != TASK CANCEL


# 2. resume after takeover uses current state, not blind replay -----------------------------


def test_resume_after_takeover_uses_current_state_not_blind_replay():
    owner_id = _owner()
    state = new_control_state(owner_id=owner_id)
    target_id = uuid.uuid4()
    action = _action(WorkspaceActionType.FOCUS_WINDOW, target_ref=target_id)
    on_user_input(state, UserInputSignal(signal_id=uuid.uuid4(), kind="mouse"), in_flight_action=action, workspace_snapshot_ref=uuid.uuid4())
    end_takeover(state)

    # Target still exists -> resumable.
    decision = resume_from_current_state(state, current_known_target_ids=frozenset({target_id}))
    assert decision.can_resume is True
    assert decision.stale is False
    assert decision.action.action_id == action.action_id


def test_resume_detects_stale_target_and_does_not_replay():
    owner_id = _owner()
    state = new_control_state(owner_id=owner_id)
    target_id = uuid.uuid4()
    action = _action(WorkspaceActionType.FOCUS_WINDOW, target_ref=target_id)
    on_user_input(state, UserInputSignal(signal_id=uuid.uuid4(), kind="mouse"), in_flight_action=action, workspace_snapshot_ref=uuid.uuid4())
    end_takeover(state)

    # Target no longer exists (e.g. window was closed during takeover) -> NOT resumable.
    decision = resume_from_current_state(state, current_known_target_ids=frozenset())
    assert decision.can_resume is False
    assert decision.stale is True
    assert decision.action is None
    assert state.paused_action is None  # cleared, not left dangling


# 3. resume does not replay a destructive action without going back through risk/preview ----


def test_resume_does_not_replay_destructive_action_without_preview():
    owner_id = _owner()
    state = new_control_state(owner_id=owner_id)
    target_id = uuid.uuid4()
    action = _action(WorkspaceActionType.DELETE_FILE, target_ref=target_id)
    on_user_input(state, UserInputSignal(signal_id=uuid.uuid4(), kind="mouse"), in_flight_action=action, workspace_snapshot_ref=uuid.uuid4())
    end_takeover(state)

    decision = resume_from_current_state(state, current_known_target_ids=frozenset({target_id}))
    assert decision.can_resume is True
    resumed_action = decision.action

    class _AllowPolicy:
        def evaluate(self, action, risk):
            from app.operating_shell.risk import ALLOWED

            return ALLOWED

    # Even though resume said "you may resume this," actually marking it executed still
    # requires a real preview -- resume itself never bypasses that gate.
    with pytest.raises(ConsequentialActionRequiresPreviewError):
        build_action_result(resumed_action, policy=_AllowPolicy(), preview=None, outcome_summary="deleted")


# 4. ambiguous "den där"/THAT remains unresolved ---------------------------------------------


def test_ambiguous_that_remains_unresolved_with_multiple_candidates():
    owner_id = _owner()
    focus_id = uuid.uuid4()
    candidate_a = uuid.uuid4()
    candidate_b = uuid.uuid4()
    context = WorkspaceContext(
        owner_id=owner_id,
        focus=WorkspaceFocus(focused_target_ref=focus_id),
        selection=None,
        recent_action_refs=(candidate_a, candidate_b),
        active_intent_id=None,
        known_targets=(
            WorkspaceTarget(target_id=focus_id, kind="document", title="current doc"),
            WorkspaceTarget(target_id=candidate_a, kind="document", title="doc A"),
            WorkspaceTarget(target_id=candidate_b, kind="document", title="doc B"),
        ),
    )
    result = resolve_reference(ReferenceKind.THAT, context)
    assert isinstance(result, AmbiguousResolution)
    assert len(result.candidates) == 2


def test_ambiguous_context_three_check():
    """Three-check: if resolve_reference() were changed to just pick candidates[0] instead
    of returning AmbiguousResolution, this test would (and must) fail."""
    owner_id = _owner()
    candidate_a, candidate_b = uuid.uuid4(), uuid.uuid4()
    context = WorkspaceContext(
        owner_id=owner_id, focus=None, selection=None, recent_action_refs=(candidate_a, candidate_b),
        active_intent_id=None,
        known_targets=(
            WorkspaceTarget(target_id=candidate_a, kind="document", title="A"),
            WorkspaceTarget(target_id=candidate_b, kind="document", title="B"),
        ),
    )

    def broken_pick_first(kind, ctx):
        return ResolvedReference(reference_kind=kind, target=ctx.known_targets[0])

    broken_result = broken_pick_first(ReferenceKind.THAT, context)
    assert isinstance(broken_result, ResolvedReference), "sanity: the broken version silently picks one"

    real_result = resolve_reference(ReferenceKind.THAT, context)
    assert isinstance(real_result, AmbiguousResolution)


def test_single_candidate_resolves_unambiguously():
    owner_id = _owner()
    focus_id = uuid.uuid4()
    context = WorkspaceContext(
        owner_id=owner_id, focus=WorkspaceFocus(focused_target_ref=focus_id), selection=None,
        recent_action_refs=(), active_intent_id=None,
        known_targets=(WorkspaceTarget(target_id=focus_id, kind="document", title="the doc"),),
    )
    result = resolve_reference(ReferenceKind.THIS, context)
    assert isinstance(result, ResolvedReference)
    assert result.target.target_id == focus_id


# 5. same-named windows do not collapse ------------------------------------------------------


def test_same_named_windows_do_not_collapse():
    w1 = WorkspaceWindow(window_id=uuid.uuid4(), title="Budget.xlsx", app_name="Excel", geometry=(0, 0, 800, 600))
    w2 = WorkspaceWindow(window_id=uuid.uuid4(), title="Budget.xlsx", app_name="Excel", geometry=(100, 100, 800, 600))
    result = find_window_by_title((w1, w2), "Budget.xlsx")
    assert isinstance(result, AmbiguousResolution)
    assert len(result.candidates) == 2
    assert w1.window_id != w2.window_id


# 6. same-named intents do not collapse -------------------------------------------------------


def test_same_named_intents_do_not_collapse():
    owner_id = _owner()
    i1 = create_intent_from_expression(owner_id=owner_id, title="bilen", raw_user_expression="jag vill köpa en bil")
    i2 = create_intent_from_expression(owner_id=owner_id, title="bilen igen", raw_user_expression="fundera på en annan bil")
    for intent in (i1, i2):
        record_understanding(intent, interpreted_goal="car-related goal")
        advance_to_planned(intent)
        advance_to_active(intent)
    result = resolve_intent_by_title_fragment((i1, i2), owner_id=owner_id, fragment="bilen")
    assert isinstance(result, AmbiguousResolution)
    assert len(result.candidates) == 2


# 7/8. restore planner: missing file / stale document version -------------------------------


def test_old_workspace_restore_detects_missing_file():
    missing_ref = uuid.uuid4()
    plan = plan_workspace_restore((resource_status(target_ref=missing_ref, kind="document", availability=ResourceAvailability.MISSING),))
    assert plan.result != RestoreResult.RESTORABLE
    assert plan.result == RestoreResult.BLOCKED


def test_stale_document_version_detected():
    stale_ref = uuid.uuid4()
    plan = plan_workspace_restore((resource_status(target_ref=stale_ref, kind="document", availability=ResourceAvailability.STALE),))
    assert plan.result == RestoreResult.STALE


def test_partial_restore_when_mixed():
    available_ref, missing_ref = uuid.uuid4(), uuid.uuid4()
    plan = plan_workspace_restore((
        resource_status(target_ref=available_ref, kind="document", availability=ResourceAvailability.AVAILABLE),
        resource_status(target_ref=missing_ref, kind="document", availability=ResourceAvailability.MISSING),
    ))
    assert plan.result == RestoreResult.PARTIALLY_RESTORABLE


def test_fully_available_restore_is_restorable():
    ref = uuid.uuid4()
    plan = plan_workspace_restore((resource_status(target_ref=ref, kind="document", availability=ResourceAvailability.AVAILABLE),))
    assert plan.result == RestoreResult.RESTORABLE


# 9. intent survives reload -------------------------------------------------------------------


def test_intent_survives_reload():
    owner_id = _owner()
    intent = create_intent_from_expression(owner_id=owner_id, title="debt cleanup", raw_user_expression="jag måste få ordning på skulderna")
    record_understanding(intent, interpreted_goal="consolidate and pay down debts")
    snapshot = intent_to_snapshot(intent)
    restored = intent_from_snapshot(snapshot)
    assert restored.intent_id == intent.intent_id
    assert restored.interpreted_goal == intent.interpreted_goal
    assert restored.state == IntentState.UNDERSTANDING
    assert len(restored.history) == len(intent.history)


# 10. workspace survives reload -----------------------------------------------------------------


def test_workspace_survives_reload():
    owner_id = _owner()
    state = new_workspace_state(owner_id=owner_id)
    set_current_task(state, task="reviewing budget")
    snapshot = workspace_to_snapshot(state)
    restored = workspace_from_snapshot(snapshot)
    assert restored.owner_id == state.owner_id
    assert restored.current_task == "reviewing budget"


# 11. intent supersession works -----------------------------------------------------------------


def test_intent_supersession_works():
    owner_id = _owner()
    old = create_intent_from_expression(owner_id=owner_id, title="bilen v1", raw_user_expression="jag vill köpa en bil")
    new = create_intent_from_expression(owner_id=owner_id, title="bilen v2", raw_user_expression="ny bilplan")
    record_understanding(old, interpreted_goal="buy a car")
    advance_to_planned(old)
    advance_to_active(old)

    supersede_intent(old, new)
    assert old.state == IntentState.SUPERSEDED
    assert old.superseded_by == new.intent_id

    active = active_intents_for_owner((old, new), owner_id=owner_id)
    assert old not in active  # excluded from "what's active"
    # but still queryable by id -- nothing deleted:
    assert old.intent_id is not None and old.title == "bilen v1"


# 12. future plan does not carry authority -------------------------------------------------------


def test_future_plan_does_not_carry_authority():
    owner_id = _owner()
    intent = create_intent_from_expression(owner_id=owner_id, title="car", raw_user_expression="jag vill köpa en bil")
    record_understanding(intent, interpreted_goal="buy a car")
    planned_action = _action(WorkspaceActionType.SUBMIT_FORM, target_ref=uuid.uuid4())
    advance_to_planned(intent, next_actions=(planned_action,))
    assert intent.next_actions == (planned_action,)
    assert has_authority_from_next_action(intent) is False

    # Attempting to actually execute the planned action still requires the real gate.
    with pytest.raises(PolicyNotWiredError):
        evaluate_action_authority(planned_action, policy=None)


# 13. action preview required for consequential action --------------------------------------------


def test_action_preview_required_for_consequential_action():
    action = _action(WorkspaceActionType.EDIT_DOCUMENT, target_ref=uuid.uuid4())
    assert classify_action_risk(action) == ActionRiskLevel.REVERSIBLE_CONSEQUENTIAL

    class _AllowPolicy:
        def evaluate(self, action, risk):
            from app.operating_shell.risk import ALLOWED

            return ALLOWED

    with pytest.raises(ConsequentialActionRequiresPreviewError):
        build_action_result(action, policy=_AllowPolicy(), preview=None, outcome_summary="edited")

    preview = build_action_preview(action, description="edit the doc", target_ref=action.command.target_ref)
    result = build_action_result(action, policy=_AllowPolicy(), preview=preview, outcome_summary="edited")
    assert result.executed is True


def test_observational_action_does_not_require_preview():
    action = _action(WorkspaceActionType.OPEN_DOCUMENT, target_ref=uuid.uuid4())

    class _AllowPolicy:
        def evaluate(self, action, risk):
            from app.operating_shell.risk import ALLOWED

            return ALLOWED

    result = build_action_result(action, policy=_AllowPolicy(), preview=None, outcome_summary="opened")
    assert result.executed is True


# 14. root-sensitive action never self-authorized --------------------------------------------------


def test_root_sensitive_action_never_self_authorized():
    action = _action(WorkspaceActionType.CHANGE_SECURITY_POLICY, target_ref=uuid.uuid4())
    with pytest.raises(PolicyNotWiredError):
        require_root_sensitive_policy(action, policy=None)


def test_root_sensitive_action_three_check():
    """Three-check: if evaluate_action_authority() were changed to default-allow when
    policy is None, this test (and the one above) would fail to catch it -- prove the
    broken version really would pass silently, then confirm the real code raises."""
    action = _action(WorkspaceActionType.CHANGE_SECURITY_POLICY, target_ref=uuid.uuid4())

    def broken_evaluate(action, *, policy):
        from app.operating_shell.risk import ALLOWED

        if policy is None:
            return ALLOWED  # the bug: silent default-allow
        return policy.evaluate(action, classify_action_risk(action))

    from app.operating_shell.risk import ALLOWED

    assert broken_evaluate(action, policy=None) == ALLOWED, "sanity: the broken version defaults to allow"

    with pytest.raises(PolicyNotWiredError):
        evaluate_action_authority(action, policy=None)


def test_root_sensitive_action_rejects_non_root_sensitive_input():
    action = _action(WorkspaceActionType.OPEN_DOCUMENT, target_ref=uuid.uuid4())
    with pytest.raises(ValueError):
        require_root_sensitive_policy(action, policy=None)


# 15. Vault-related workspace restore remains locked -------------------------------------------------


def test_vault_related_workspace_restore_remains_locked():
    vault_ref = uuid.uuid4()
    normal_ref = uuid.uuid4()
    plan = plan_workspace_restore((
        resource_status(target_ref=vault_ref, kind="vault_object", availability=ResourceAvailability.AVAILABLE, vault_linked=True),
        resource_status(target_ref=normal_ref, kind="document", availability=ResourceAvailability.AVAILABLE),
    ))
    assert vault_ref in plan.vault_locked_refs
    assert plan.result == RestoreResult.RESTORABLE  # the non-vault resource is fine on its own
    # The vault resource itself must never be classified as part of the auto-restorable
    # (non-vault) set -- it's tracked separately in vault_locked_refs instead.
    non_vault_targets = {r.target_ref for r in plan.resources if not r.vault_linked}
    assert vault_ref not in non_vault_targets
    assert normal_ref in non_vault_targets


def test_vault_only_workspace_cannot_auto_restore():
    vault_ref = uuid.uuid4()
    plan = plan_workspace_restore((resource_status(target_ref=vault_ref, kind="vault_object", availability=ResourceAvailability.AVAILABLE, vault_linked=True),))
    assert plan.result == RestoreResult.BLOCKED
    assert vault_ref in plan.vault_locked_refs


# 16. cross-owner workspace leakage impossible -----------------------------------------------------


def test_cross_owner_workspace_leakage_impossible():
    owner_a, owner_b = _owner(), _owner()
    state_a = new_workspace_state(owner_id=owner_a)
    state_b = new_workspace_state(owner_id=owner_b)
    window_a = WorkspaceWindow(window_id=uuid.uuid4(), title="A's doc", app_name="Docs", geometry=(0, 0, 1, 1))
    window_b = WorkspaceWindow(window_id=uuid.uuid4(), title="B's doc", app_name="Docs", geometry=(0, 0, 1, 1))
    state_a.open_windows = (window_a,)
    state_b.open_windows = (window_b,)

    windows_for_a = list_windows_for_owner((state_a, state_b), owner_id=owner_a)
    assert windows_for_a == (window_a,)
    assert window_b not in windows_for_a


# 17. cross-owner intent leakage impossible ----------------------------------------------------------


def test_cross_owner_intent_leakage_impossible():
    owner_a, owner_b = _owner(), _owner()
    intent_a = create_intent_from_expression(owner_id=owner_a, title="A's intent", raw_user_expression="x")
    intent_b = create_intent_from_expression(owner_id=owner_b, title="B's intent", raw_user_expression="y")
    record_understanding(intent_a, interpreted_goal="do x")
    advance_to_planned(intent_a)
    advance_to_active(intent_a)
    record_understanding(intent_b, interpreted_goal="do y")
    advance_to_planned(intent_b)
    advance_to_active(intent_b)

    active_for_a = active_intents_for_owner((intent_a, intent_b), owner_id=owner_a)
    assert active_for_a == (intent_a,)
    assert intent_b not in active_for_a


# 18. malformed persisted workspace fails closed ---------------------------------------------------


def test_malformed_persisted_workspace_fails_closed():
    with pytest.raises(MalformedWorkspaceSnapshotError):
        workspace_from_snapshot({"owner_id": "not-a-uuid", "open_windows": [], "control_state": "MAINAI_CONTROL", "updated_at": "2024-01-01T00:00:00+00:00"})

    with pytest.raises(MalformedWorkspaceSnapshotError):
        workspace_from_snapshot({"open_windows": []})  # missing required keys


def test_malformed_persisted_intent_fails_closed():
    with pytest.raises(MalformedIntentSnapshotError):
        intent_from_snapshot({"intent_id": "garbage"})


# 19. corrupt intent graph handled -----------------------------------------------------------------


def test_corrupt_intent_graph_handled():
    owner_id = _owner()
    intent = create_intent_from_expression(owner_id=owner_id, title="dependent", raw_user_expression="x")
    missing_dep = uuid.uuid4()
    intent.dependencies = (missing_dep,)
    gaps = find_missing_dependencies((intent,))
    assert intent.intent_id in gaps
    assert gaps[intent.intent_id] == (missing_dep,)


# 20. cyclic intent dependencies bounded -------------------------------------------------------------


def test_cyclic_intent_dependencies_bounded():
    owner_id = _owner()
    a = create_intent_from_expression(owner_id=owner_id, title="A", raw_user_expression="x")
    b = create_intent_from_expression(owner_id=owner_id, title="B", raw_user_expression="y")
    a.dependencies = (b.intent_id,)
    b.dependencies = (a.intent_id,)

    import time

    start = time.monotonic()
    cycle = detect_dependency_cycle((a, b))
    elapsed = time.monotonic() - start
    assert cycle is not None
    assert a.intent_id in cycle and b.intent_id in cycle
    assert elapsed < 1.0  # terminates quickly, never infinite-loops


def test_acyclic_intent_dependencies_report_no_cycle():
    owner_id = _owner()
    a = create_intent_from_expression(owner_id=owner_id, title="A", raw_user_expression="x")
    b = create_intent_from_expression(owner_id=owner_id, title="B", raw_user_expression="y")
    a.dependencies = (b.intent_id,)
    assert detect_dependency_cycle((a, b)) is None


# 21. large workspace bounded ------------------------------------------------------------------------


def test_large_workspace_recent_commands_bounded():
    owner_id = _owner()
    state = new_workspace_state(owner_id=owner_id)
    ids = [uuid.uuid4() for _ in range(200)]
    for cid in ids:
        record_recent_command(state, cid)
    assert len(state.recent_commands) <= 50
    # oldest dropped, newest retained:
    assert state.recent_commands[-1] == ids[-1]
    assert ids[0] not in state.recent_commands


# 22. crash during state write recovers safely --------------------------------------------------------


def test_crash_during_state_write_recovers_safely_via_fail_closed():
    """Models an interrupted/partial snapshot write: a dict missing some fields because the
    write was cut short. from_snapshot() must fail closed cleanly, never produce a
    half-valid WorkspaceState a caller might mistake for a full recovery."""
    partial_snapshot = {"owner_id": str(uuid.uuid4()), "open_windows": []}  # cut short before control_state/updated_at were written
    with pytest.raises(MalformedWorkspaceSnapshotError):
        workspace_from_snapshot(partial_snapshot)


# --- Additional structural/invariant tests. ---------------------------------------------------


def test_no_execute_method_exists_anywhere():
    """COMMAND DESCRIPTION != OS AUTHORITY: no class in this package defines execute()/
    run()/apply() -- structural proof, not just documentation."""
    import app.operating_shell.control as control_mod
    import app.operating_shell.delegation as delegation_mod
    import app.operating_shell.risk as risk_mod
    import app.operating_shell.types as types_mod
    import app.operating_shell.workspace as workspace_mod

    forbidden = {"execute", "run", "apply"}
    for mod in (types_mod, control_mod, risk_mod, workspace_mod, delegation_mod):
        for name, obj in vars(mod).items():
            if inspect.isclass(obj) and obj.__module__ == mod.__name__:
                found = forbidden & set(vars(obj).keys())
                assert not found, f"{mod.__name__}.{name} defines forbidden method(s): {found}"


def test_workspace_state_paused_action_does_not_reauthorize_on_restore():
    owner_id = _owner()
    state = new_workspace_state(owner_id=owner_id)
    action = _action(WorkspaceActionType.DELETE_FILE, target_ref=uuid.uuid4())
    from app.operating_shell.types import PausedAction

    state.paused_action = PausedAction(paused_action_id=uuid.uuid4(), action=action, workspace_snapshot_ref=uuid.uuid4())

    snapshot = workspace_to_snapshot(state)
    restored = workspace_from_snapshot(snapshot)
    # Snapshot round-trip does not even carry paused_action (workspace.py's to_snapshot
    # deliberately omits it -- see restore_workspace_state_does_not_reauthorize's docstring)
    assert restored.paused_action is None

    # And even on the ORIGINAL (non-snapshotted) state, reading paused_action never
    # constitutes authorization:
    from app.operating_shell.workspace import restore_workspace_state_does_not_reauthorize

    returned_action = restore_workspace_state_does_not_reauthorize(state)
    assert returned_action is not None
    assert state.paused_action is not None  # still there -- reading it did not clear/execute it


def test_secret_shaped_content_rejected_from_workspace_field():
    with pytest.raises(WorkspaceSecretShapedContentError):
        reject_secret_shaped_content("current_task", "password: hunter2hunter2hunter2hunter2")
    with pytest.raises(WorkspaceSecretShapedContentError):
        reject_secret_shaped_content("current_task", "a" * 250)
    with pytest.raises(WorkspaceSecretShapedContentError):
        # Built via concatenation, never as one contiguous literal -- a synthetic value that
        # only exists to exercise the opaque-token-shape rejection path (see this session's
        # earlier GitHub push-protection incident with a Stripe-shaped test string).
        reject_secret_shaped_content("current_task", "opaque_test_token_" + "9f8a7b6c5d4e3f2a1b0c9d8e")
    reject_secret_shaped_content("current_task", "reviewing the household budget")  # does not raise


def test_agent_result_is_not_directly_user_facing():
    """AGENT RESULT != USER-FACING TRUTH: an InternalDelegationResult is never itself a
    UserFacingAnswer, and aggregate_for_user() is the only function that produces one."""
    from app.operating_shell.types import InternalDelegationResult, UserFacingAnswer

    finding = record_delegation_result(specialist_key="finance", finding_summary="budget looks tight", confidence=0.7)
    assert isinstance(finding, InternalDelegationResult)
    assert not isinstance(finding, UserFacingAnswer)

    answer = aggregate_for_user((finding,), owner_facing_text="Finance flagged your budget as tight.")
    assert isinstance(answer, UserFacingAnswer)
    assert finding.delegation_id in answer.source_delegation_ids

    with pytest.raises(ValueError):
        aggregate_for_user((), owner_facing_text="nothing to say")
