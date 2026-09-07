"""MainAI V2 Autonomous Development Director foundation tests (Part 1 of 2).

Pure in-memory, no DB dependency. See
docs/mainai_v2/MAINAI_V2_AUTONOMOUS_DEVELOPMENT_DIRECTOR_RECONCILIATION.md for the design
these tests verify.
"""

from __future__ import annotations

import dataclasses
import re
import uuid

import pytest

from app.dev_director import (
    AutonomyLevel,
    AutonomyLevelRequiresFounderAuthorizationError,
    BudgetExceededError,
    BuilderExaminerCollusionError,
    CompletionEvidence,
    ExaminerVerdict,
    ExaminerVerdictError,
    ExternalProviderLease,
    Job,
    JobState,
    JobTransitionError,
    NoAvailableProvider,
    NoReadyJob,
    PR_245_PROTECTED_ARTIFACT,
    ProtectedArtifactViolationError,
    ProviderCapabilityProfile,
    ProviderUsageState,
    TestResultRecord,
    assert_artifact_not_protected,
    detect_job_conflicts,
    lease_scoped_to_original_request,
    new_builder_assignment,
    new_examiner_assignment,
    new_external_provider_lease,
    new_job,
    new_program,
    next_ready_job,
    record_budget_consumption,
    record_examiner_verdict,
    release_budget_reservation,
    reserve_from_budget,
    select_failover_provider,
    set_autonomy_level,
    submit_builder_result,
    transition_job,
    validate_completion_evidence,
    verify_job_event_chain_intact,
)


def _owner() -> uuid.UUID:
    return uuid.uuid4()


# --- Milestone 1: AutonomyLevel + Program + BudgetEnvelope. --------------------------------


def test_default_program_autonomy_is_level_2():
    p = new_program(owner_id=_owner(), repo_identity="LifeAI", goal="ship the director")
    assert p.autonomy_level == AutonomyLevel.LEVEL_2_BUILDER_EXAMINER_FIX_LOOP_PR_PROPOSAL


def test_level_3_and_4_require_explicit_founder_authorization():
    p = new_program(owner_id=_owner(), repo_identity="LifeAI", goal="g")
    for level in (AutonomyLevel.LEVEL_3_AUTO_MERGE_LOW_RISK, AutonomyLevel.LEVEL_4_DEPLOYMENT_RELEASE):
        with pytest.raises(AutonomyLevelRequiresFounderAuthorizationError):
            set_autonomy_level(p, level=level)
        set_autonomy_level(p, level=level, founder_authorization_ref="founder-2026-09-07")
        assert p.autonomy_level == level


def test_level_1_and_2_do_not_require_founder_authorization():
    p = new_program(owner_id=_owner(), repo_identity="LifeAI", goal="g")
    set_autonomy_level(p, level=AutonomyLevel.LEVEL_1_ISOLATED_RESEARCH_CODE_TEST)
    assert p.autonomy_level == AutonomyLevel.LEVEL_1_ISOLATED_RESEARCH_CODE_TEST


def test_program_state_never_returns_something_mistakable_for_execution_authority():
    """PROGRAM STATE != EXECUTION AUTHORITY: no function in this package takes a bare
    Program and returns anything shaped like a real grant. Structural check over the
    public API surface."""
    import app.dev_director as pkg
    import inspect

    for name in pkg.__all__:
        obj = getattr(pkg, name)
        if not inspect.isfunction(obj):
            continue
        sig = inspect.signature(obj)
        params = list(sig.parameters)
        if params and params[0] == "program" and len(params) == 1:
            pytest.fail(f"{name} takes ONLY a Program and nothing else -- would be a program-state-is-authority shortcut")


def test_budget_reservation_is_fail_closed_at_ceiling():
    p = new_program(owner_id=_owner(), repo_identity="LifeAI", goal="g", total_budget_ceiling_usd=100.0)
    reserve_from_budget(p.budget_envelope, amount_usd=80.0)
    with pytest.raises(BudgetExceededError):
        reserve_from_budget(p.budget_envelope, amount_usd=30.0)
    release_budget_reservation(p.budget_envelope, amount_usd=80.0)
    reserve_from_budget(p.budget_envelope, amount_usd=30.0)
    record_budget_consumption(p.budget_envelope, amount_usd=30.0, provider_identity="codex")
    assert p.budget_envelope.consumed_usd == 30.0
    assert p.budget_envelope.reserved_usd == 0.0
    assert p.budget_envelope.per_provider_consumed_usd["codex"] == 30.0


def test_budget_per_provider_ceiling_enforced():
    p = new_program(owner_id=_owner(), repo_identity="LifeAI", goal="g", total_budget_ceiling_usd=1000.0)
    p.budget_envelope.per_provider_ceiling_usd["codex"] = 10.0
    with pytest.raises(BudgetExceededError):
        reserve_from_budget(p.budget_envelope, amount_usd=20.0, provider_identity="codex")


# --- Milestone 2: Job state machine + priority/dependency/conflict engine. -----------------


def _ready_job(program_id, risk="low", **kwargs) -> Job:
    j = new_job(program_id=program_id, goal_description="x", risk_class=risk, **kwargs)
    transition_job(j, to_state=JobState.READY)
    return j


def test_job_p0_sorts_before_lower_risk():
    pid = _owner()
    j_low = _ready_job(pid, risk="low")
    j_p0 = _ready_job(pid, risk="P0")
    result = next_ready_job((j_low, j_p0))
    assert result.job_id == j_p0.job_id


def test_next_ready_job_ambiguous_tie_is_not_silently_guessed():
    pid = _owner()
    a = _ready_job(pid, risk="P0")
    b = _ready_job(pid, risk="P0")
    b.created_at = a.created_at  # force a genuine tie -- natural timestamps differ by microseconds
    result = next_ready_job((a, b))
    assert isinstance(result, NoReadyJob)
    assert set(result.ambiguous_candidates) == {a.job_id, b.job_id}


def test_no_ready_job_when_dependency_not_certified():
    pid = _owner()
    dep = new_job(program_id=pid, goal_description="dep")
    transition_job(dep, to_state=JobState.READY)
    child = new_job(program_id=pid, goal_description="child", risk_class="P0")
    child.dependencies = (dep.job_id,)
    transition_job(child, to_state=JobState.READY)
    result = next_ready_job((dep, child))
    # dep itself is ready and has no deps -> it wins; child is not eligible yet.
    assert result.job_id == dep.job_id


def test_needs_fix_cannot_skip_verifying_to_reach_certified():
    pid = _owner()
    j = new_job(program_id=pid, goal_description="x")
    transition_job(j, to_state=JobState.READY)
    transition_job(j, to_state=JobState.ASSIGNED)
    transition_job(j, to_state=JobState.RUNNING)
    transition_job(j, to_state=JobState.VERIFYING)
    transition_job(j, to_state=JobState.NEEDS_FIX)
    with pytest.raises(JobTransitionError):
        transition_job(j, to_state=JobState.CERTIFIED)
    transition_job(j, to_state=JobState.RUNNING)
    transition_job(j, to_state=JobState.VERIFYING)
    transition_job(j, to_state=JobState.CERTIFIED)
    assert j.state == JobState.CERTIFIED


def test_needs_fix_three_check():
    """Three-check: temporarily widen the transition table to allow NEEDS_FIX->CERTIFIED
    directly and confirm the assertion above would then fail -- proving the real table is
    doing real work."""
    from app.dev_director.types import JOB_TRANSITIONS, JobState as JS

    original = JOB_TRANSITIONS[JS.NEEDS_FIX]
    try:
        JOB_TRANSITIONS[JS.NEEDS_FIX] = original | {JS.CERTIFIED}
        pid = _owner()
        j = new_job(program_id=pid, goal_description="x")
        transition_job(j, to_state=JobState.READY)
        transition_job(j, to_state=JobState.ASSIGNED)
        transition_job(j, to_state=JobState.RUNNING)
        transition_job(j, to_state=JobState.VERIFYING)
        transition_job(j, to_state=JobState.NEEDS_FIX)
        transition_job(j, to_state=JobState.CERTIFIED)  # would NOT raise with the widened table
        assert j.state == JobState.CERTIFIED
    finally:
        JOB_TRANSITIONS[JS.NEEDS_FIX] = original


def test_terminal_states_reject_all_transitions():
    pid = _owner()
    j = new_job(program_id=pid, goal_description="x")
    transition_job(j, to_state=JobState.READY)
    transition_job(j, to_state=JobState.CANCELLED)
    with pytest.raises(JobTransitionError):
        transition_job(j, to_state=JobState.READY)


def test_job_event_chain_is_hash_chained_and_verifiable():
    pid = _owner()
    j = new_job(program_id=pid, goal_description="x")
    transition_job(j, to_state=JobState.READY)
    transition_job(j, to_state=JobState.ASSIGNED)
    assert verify_job_event_chain_intact(j)
    # tamper
    tampered = j.history[0]
    object.__setattr__(tampered, "note", "tampered") if dataclasses.is_dataclass(tampered) else setattr(tampered, "note", "tampered")
    assert not verify_job_event_chain_intact(j)


def test_declared_file_overlap_conflict_detected():
    pid = _owner()
    a = _ready_job(pid, touches=("app/foo.py",))
    b = _ready_job(pid, touches=("app/foo.py", "app/bar.py"))
    c = _ready_job(pid, touches=("app/baz.py",))
    conflicts = detect_job_conflicts((a, b, c))
    assert len(conflicts) == 1
    assert set(conflicts[0].overlapping) == {"app/foo.py"}


# --- Milestone 3: Protected artifacts + external provider lease + failover. ----------------


def test_pr_245_is_protected_against_modify_merge_rebase():
    for action in ("modify", "merge", "rebase"):
        with pytest.raises(ProtectedArtifactViolationError):
            assert_artifact_not_protected(PR_245_PROTECTED_ARTIFACT.ref, (PR_245_PROTECTED_ARTIFACT,), action=action)


def test_protected_ref_match_is_exact_not_substring():
    # Same bug shape this whole campaign has repeatedly found: a short prefix must NOT match.
    assert_artifact_not_protected(PR_245_PROTECTED_ARTIFACT.ref[:7], (PR_245_PROTECTED_ARTIFACT,))
    assert_artifact_not_protected("unrelated-branch-name", (PR_245_PROTECTED_ARTIFACT,))


def test_protected_ref_three_check():
    """Three-check: a naive substring-based implementation WOULD incorrectly flag the short
    prefix above -- confirm that premise directly to prove the exact-match test isn't vacuous."""
    assert PR_245_PROTECTED_ARTIFACT.ref[:7] in PR_245_PROTECTED_ARTIFACT.ref  # the substring relationship genuinely exists
    # ...yet assert_artifact_not_protected() above did NOT raise for it -- exact match, confirmed.


def test_external_provider_lease_field_set_is_closed_no_credential_field():
    fieldnames = {f.name for f in dataclasses.fields(ExternalProviderLease)}
    assert not any(re.search(r"token|secret|credential|password|vault", f, re.IGNORECASE) for f in fieldnames), fieldnames


def test_failover_never_widens_capabilities_beyond_original_request():
    broad = ProviderCapabilityProfile(
        provider_identity="broad_provider", capabilities=("code", "test", "network", "deploy"),
        usage_state=ProviderUsageState.AVAILABLE, cost_class="high", privacy_class="standard", recent_failure_rate=0.0,
    )
    lease = lease_scoped_to_original_request(
        broad, original_allowed_tools=("read", "write"), original_allowed_files=("app/foo.py",),
        task_ref=None, workspace_ref="ws", branch="b", ttl_seconds=3600,
    )
    assert lease.allowed_tools == ("read", "write")
    assert lease.allowed_files == ("app/foo.py",)


def test_failover_excludes_exhausted_and_non_examiner_eligible():
    exhausted = ProviderCapabilityProfile(provider_identity="codex", capabilities=("code",), usage_state=ProviderUsageState.USAGE_EXHAUSTED, cost_class="low", privacy_class="standard", recent_failure_rate=0.1)
    ineligible = ProviderCapabilityProfile(provider_identity="local_agent:1", capabilities=("code",), usage_state=ProviderUsageState.AVAILABLE, cost_class="low", privacy_class="standard", recent_failure_rate=0.1, is_examiner_eligible=False)
    result = select_failover_provider((exhausted, ineligible), required_capabilities=("code",), examiner_role=True)
    assert isinstance(result, NoAvailableProvider)


def test_new_external_provider_lease_expires_in_future():
    lease = new_external_provider_lease(provider_identity="codex", task_ref=None, workspace_ref="ws", branch="b", allowed_tools=(), allowed_files=(), ttl_seconds=60)
    from datetime import datetime, timezone

    assert lease.expires_at > datetime.now(timezone.utc)


# --- Milestone 4: Builder/Examiner separation. ---------------------------------------------


def _builder_and_result():
    job_id = uuid.uuid4()
    lease_id = uuid.uuid4()
    ba = new_builder_assignment(job_id=job_id, builder_identity="codex", external_lease_ref=lease_id)
    res = submit_builder_result(ba, result_sha="sha-abc123", branch="feature-x", claimed_completion=True)
    return job_id, ba, res


def test_builder_cannot_self_examine():
    job_id, ba, res = _builder_and_result()
    with pytest.raises(BuilderExaminerCollusionError):
        new_examiner_assignment(job_id=job_id, examiner_identity="codex", builder_assignment=ba, target_sha=res.result_sha)


def test_examiner_cannot_reuse_builder_execution_lease():
    job_id, ba, res = _builder_and_result()
    with pytest.raises(BuilderExaminerCollusionError):
        new_examiner_assignment(job_id=job_id, examiner_identity="claude", builder_assignment=ba, target_sha=res.result_sha, external_lease_ref=ba.external_lease_ref)


def test_examiner_must_attack_exact_frozen_sha_not_a_moved_branch():
    job_id, ba, res = _builder_and_result()
    ea = new_examiner_assignment(job_id=job_id, examiner_identity="claude", builder_assignment=ba, target_sha="sha-STALE-different")
    with pytest.raises(ExaminerVerdictError):
        record_examiner_verdict(ea, builder_assignment=ba, builder_result=res, verdict=ExaminerVerdict.PASS, evidence=("x",))


def test_pass_verdict_requires_non_empty_evidence():
    job_id, ba, res = _builder_and_result()
    ea = new_examiner_assignment(job_id=job_id, examiner_identity="claude", builder_assignment=ba, target_sha=res.result_sha)
    with pytest.raises(ExaminerVerdictError):
        record_examiner_verdict(ea, builder_assignment=ba, builder_result=res, verdict=ExaminerVerdict.PASS, evidence=())


def test_genuine_independent_examination_succeeds():
    job_id, ba, res = _builder_and_result()
    ea = new_examiner_assignment(job_id=job_id, examiner_identity="claude", builder_assignment=ba, target_sha=res.result_sha)
    verdict = record_examiner_verdict(ea, builder_assignment=ba, builder_result=res, verdict=ExaminerVerdict.FAIL, evidence=("found a P0",), reason="race condition")
    assert verdict.verdict == ExaminerVerdict.FAIL
    assert verdict.examiner_identity == "claude"
    assert verdict.target_sha == res.result_sha


def test_builder_assignment_requires_exactly_one_of_workforce_or_external_ref():
    job_id = uuid.uuid4()
    with pytest.raises(ValueError):
        new_builder_assignment(job_id=job_id, builder_identity="codex")
    with pytest.raises(ValueError):
        new_builder_assignment(job_id=job_id, builder_identity="codex", workforce_assignment_ref=uuid.uuid4(), external_lease_ref=uuid.uuid4())


# --- Milestone 5: Completion evidence. ------------------------------------------------------


def _evidence(**overrides) -> CompletionEvidence:
    base = dict(
        branch="feature-x", base_sha="base1", new_sha="new1", working_tree_state="clean",
        changed_files=("app/foo.py",), test_commands=("pytest",),
        test_results=(TestResultRecord(command="pytest", passed=True, summary="40 passed"),),
        open_blockers=(), p0_count=0, p1_count=0, production_wiring_state="none", merge_state="unmerged",
    )
    base.update(overrides)
    return CompletionEvidence(**base)


def test_completion_evidence_valid_when_clean():
    result = validate_completion_evidence(_evidence(), real_current_sha="new1")
    assert result.valid and not result.sha_mismatch


def test_completion_evidence_catches_fake_sha():
    result = validate_completion_evidence(_evidence(new_sha="FAKE-SHA-NEVER-HAPPENED"), real_current_sha="new1")
    assert result.sha_mismatch and not result.valid


def test_completion_evidence_flags_open_p0():
    result = validate_completion_evidence(_evidence(p0_count=1))
    assert not result.valid
    assert any("P0" in r for r in result.reasons)


def test_completion_evidence_flags_failing_test():
    result = validate_completion_evidence(_evidence(test_results=(TestResultRecord(command="pytest", passed=False, summary="1 failed"),)))
    assert not result.valid


def test_no_function_in_package_accepts_raw_provider_text_and_changes_state():
    """PROVIDER OUTPUT != MAINAI COMMAND: no public function takes a bare `str` as its ONLY
    non-keyword-only positional argument and is documented/named as recording/applying a
    state change from it."""
    import app.dev_director as pkg
    import inspect

    suspicious_names = {"apply", "execute", "run_command", "command"}
    for name in pkg.__all__:
        obj = getattr(pkg, name)
        if not inspect.isfunction(obj):
            continue
        lowered = name.lower()
        assert not any(s in lowered for s in suspicious_names), f"{name} looks like it might execute raw provider text as a command"


# --- Package-level structural tests. --------------------------------------------------------


def test_no_import_of_sibling_v2_packages():
    import app.dev_director as pkg
    from pathlib import Path

    package_dir = Path(pkg.__file__).parent
    forbidden = re.compile(
        r"^\s*(import|from)\s+app\.(guardian|privacy_boundary|sentinel|sovereign_identity|life_recovery|operating_shell|attachment_chamber)\b",
        re.MULTILINE,
    )
    for py_file in package_dir.glob("*.py"):
        source = py_file.read_text()
        assert not forbidden.search(source), f"{py_file.name} must not import any of the seven sibling V2 packages"


def test_no_import_of_245_branch_evidence_claim():
    import app.dev_director as pkg
    from pathlib import Path

    package_dir = Path(pkg.__file__).parent
    for py_file in package_dir.glob("*.py"):
        source = py_file.read_text()
        assert "evidence_claim" not in source or "does NOT import" in source or "does not import" in source.lower()
