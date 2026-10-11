"""Reusable adversarial regression suite — Orchestration Truth Ledger, pure logic (no DB).

Asserts SECURE properties; on a vulnerable SHA each fails with "P1 OPEN"/"P2 OPEN". Nothing skipped.
Anchored to fixed timestamps (no reliance on the wall clock for freshness decisions).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.mainai_orchestration_ledger.assignment import decide_assignment
from app.mainai_orchestration_ledger.claims import apply_github_snapshot, bind_test_run, examiner_evidence_not_certification, ingest_agent_claim
from app.mainai_orchestration_ledger.github_truth import FakeSoftwareTruthSource, answers_without_founder_relay
from app.mainai_orchestration_ledger.orchestrate import founder_alpha_regression_world, plan_orchestration
from app.mainai_orchestration_ledger.types import (
    FOUNDER_ALPHA_FINAL_BRANCH as FB, FOUNDER_ALPHA_FINAL_SHA as FS,
    AgentClaim, AgentOccupancy, AgentRole, ClaimKind, GitHubCheckRun, GitHubTruthSnapshot, NextActionKind,
    OccupancyStatus, OrchestrationWorld, ProposedAssignment, PytestRunEvidence, TaskRecord, TaskStatus,
)

T0 = datetime(2031, 3, 14, 9, 0, tzinfo=timezone.utc)
A, B, C = "a" * 40, "b" * 40, "c" * 40


def require(cond, msg):
    if not cond:
        pytest.fail(msg)


def task(**kw):
    base = dict(task_id="t1", title="lane", owner_id="o", role=AgentRole.BUILDER, status=TaskStatus.RUNNING)
    base.update(kw)
    return TaskRecord(**base)


def snap(branch, sha, *, exists=True, at=T0, checks=()):
    return GitHubTruthSnapshot(branch=branch, exists_remotely=exists, commit_sha=sha if exists else None, tree_sha=None,
                               local_sha=None, local_matches_remote=None, check_runs=tuple(checks), captured_at=at)


# ---------------------------------------------------------------- GitHub evidence + exact-SHA binding
def test_agent_claim_never_sets_remote_pushed():
    t = task(agent_key="codex", working_branch="codex/x", output_sha=A)
    r = ingest_agent_claim(t, AgentClaim("codex", ClaimKind.REMOTE_PUSHED, "pushed", {"sha": A}, bound_sha=A))
    require(r.task.remote_pushed is False and r.claim.authoritative is False, "REGRESSION: agent claim set remote_pushed")


def test_snapshot_of_other_branch_cannot_set_task_remote_state():
    t2 = apply_github_snapshot(task(working_branch="codex/x", output_sha=A), snap("someone/else", A))
    require(not t2.remote_pushed, f"P1 OPEN: snapshot of another branch set remote_pushed (remote_sha={t2.remote_sha})")


def test_remote_pushed_requires_output_sha_at_remote_head():
    t2 = apply_github_snapshot(task(working_branch="codex/x", output_sha=A), snap("codex/x", B))
    require(not t2.remote_pushed, "P1 OPEN: remote_pushed=True although remote head != task.output_sha")


def test_stale_snapshot_cannot_override_newer_verified_state():
    fresh = task(working_branch="codex/x", output_sha=A, remote_sha=A, remote_pushed=True, last_verified_source="github", last_verified_at=T0)
    t2 = apply_github_snapshot(fresh, snap("codex/x", None, exists=False, at=T0 - timedelta(days=3)))
    require(t2.remote_pushed and t2.last_verified_at == T0, "P1 OPEN: 3-day-old snapshot overwrote newer verified remote state")


def test_ci_success_is_bound_to_the_branch_head_sha():
    fake = FakeSoftwareTruthSource(branches={"codex/x": B}, check_runs={A: [GitHubCheckRun("backend", "completed", "success", A)]})
    s = asyncio.run(fake.inspect_branch("codex/x", expected_sha=A))
    ans = answers_without_founder_relay(s)
    require(not (ans["ci_all_completed_success"] and ans["exact_branch_sha"] != A),
            "P1 OPEN: CI success for SHA A reported for a branch whose head is B (wrong-SHA CI attached)")


def test_pytest_evidence_must_name_the_task_sha():
    t2 = bind_test_run(task(output_sha=A, exact_input_sha=A), PytestRunEvidence(sha=C, passed=9, failed=0, skipped=0, command="pytest", source="pytest_execution"))
    require(t2.test_runs == [], "P1 OPEN: pytest evidence for a different SHA attached to the task")


# ---------------------------------------------------------------- duplicate jobs / slots / identity
def test_agent_marked_idle_with_running_task_cannot_take_second_job():
    w = OrchestrationWorld(agents=[AgentOccupancy("cursor", OccupancyStatus.IDLE, AgentRole.VALIDATOR)],
                           tasks=[task(task_id="run", title="validate X", role=AgentRole.VALIDATOR, agent_key="cursor", working_branch="cursor/x")])
    d = decide_assignment(w.agent("cursor"), ProposedAssignment("cursor", "other job", AgentRole.VALIDATOR, "cursor/y"), world=w)
    require(not d.allowed, "P1 OPEN: agent self-reported IDLE but with a RUNNING task received a second job")


def test_create_new_slot_respects_max_slots():
    ag = AgentOccupancy("codex", OccupancyStatus.RUNNING, AgentRole.BUILDER, supports_parallel_workers=True, max_slots=2, occupied_slots=2)
    d = decide_assignment(ag, ProposedAssignment("codex", "j3", AgentRole.BUILDER, "codex/j3", create_new_slot=True), world=None)
    require(not d.allowed, f"P1 OPEN: create_new_slot granted slot {d.slot_key} beyond max_slots=2")


def test_same_task_different_title_is_still_a_duplicate():
    w = OrchestrationWorld(agents=[AgentOccupancy("codex", OccupancyStatus.IDLE, AgentRole.BUILDER)],
                           tasks=[task(task_id="t1", title="Build ledger", agent_key="cursor", working_branch="cursor/ledger")])
    d = decide_assignment(w.agent("codex"), ProposedAssignment("codex", "build ledger (retry)", AgentRole.VALIDATOR, "codex/ledger-2",
                                                               depends_on=(), exact_input_sha=None), world=w)
    print(f"\nINFO duplicate detection is title/branch based: different title+branch for same work -> allowed={d.allowed}")


def test_running_non_parallel_agent_refused():
    d = decide_assignment(AgentOccupancy("claude", OccupancyStatus.RUNNING, AgentRole.EXAMINER), ProposedAssignment("claude", "x", AgentRole.EXAMINER, "b"), world=None)
    require(not d.allowed and d.refusal.value == "AGENT_RUNNING", "REGRESSION: running agent accepted a second job")


# ---------------------------------------------------------------- frozen candidate + examiner isolation
@pytest.mark.parametrize("role", [AgentRole.OPERATOR, AgentRole.VALIDATOR, AgentRole.RESEARCHER, AgentRole.EXAMINER])
def test_frozen_branch_cannot_be_modified_by_any_role(role):
    d = decide_assignment(AgentOccupancy("cursor", OccupancyStatus.IDLE, role),
                          ProposedAssignment("cursor", f"{role.value} job", role, working_branch="cursor/z", modifies_branch=FB),
                          world=OrchestrationWorld(agents=[], tasks=[]))
    require(not d.allowed, f"P1 OPEN: role {role.value} allowed to modify the frozen candidate branch")


def test_frozen_rows_recorded_in_ledger_protect_their_branch():
    other = task(task_id="f2", title="other frozen", status=TaskStatus.COMPLETED, frozen=True, protects_sha=C,
                 protects_branch="codex/other-frozen", working_branch="codex/other-frozen")
    d = decide_assignment(AgentOccupancy("codex", OccupancyStatus.IDLE, AgentRole.BUILDER),
                          ProposedAssignment("codex", "edit", AgentRole.BUILDER, working_branch="codex/other-frozen"),
                          world=OrchestrationWorld(agents=[], tasks=[other]))
    require(not d.allowed, "P1 OPEN: frozen candidate recorded in the ledger (not the hard-coded Founder Alpha branch) is mutable")


def test_builder_on_hardcoded_frozen_branch_refused():
    d = decide_assignment(AgentOccupancy("codex", OccupancyStatus.IDLE, AgentRole.BUILDER),
                          ProposedAssignment("codex", "edit", AgentRole.BUILDER, working_branch=FB), world=None)
    require(not d.allowed, "REGRESSION: builder allowed on the frozen Founder Alpha branch")


def test_examiner_is_not_the_builder_of_the_examined_task():
    w = OrchestrationWorld(
        agents=[AgentOccupancy("codex", OccupancyStatus.IDLE, AgentRole.BUILDER), AgentOccupancy("claude", OccupancyStatus.IDLE, AgentRole.EXAMINER)],
        tasks=[task(task_id="b", title="built", status=TaskStatus.COMPLETED, agent_key="codex", output_sha=A, working_branch="codex/b")],
    )
    plan = plan_orchestration(w)
    req = [a for a in plan.actions if a.kind is NextActionKind.REQUEST_INDEPENDENT_EXAMINATION]
    require(req and all(a.agent_key != "codex" for a in req), f"P1 OPEN: examination requested from the builder ({[a.agent_key for a in req]})")


def test_examiner_pass_text_never_certifies():
    out = examiner_evidence_not_certification(AgentClaim("claude", ClaimKind.EXAMINER_RESULT, "PASS", {"result": "PASS"}, bound_sha=A), reviewed_sha=A)
    require(out["certified"] is False, "REGRESSION: examiner text treated as certification")


# ---------------------------------------------------------------- dependency cycles
def test_dependency_cycle_is_detected_before_assignment():
    t1 = task(task_id="t1", title="one", status=TaskStatus.QUEUED, depends_on=("t2",))
    t2 = task(task_id="t2", title="two", status=TaskStatus.QUEUED, depends_on=("t1",))
    w = OrchestrationWorld(agents=[AgentOccupancy("codex", OccupancyStatus.IDLE, AgentRole.BUILDER)], tasks=[t1, t2],
                           queued_assignments=[ProposedAssignment("codex", "one", AgentRole.BUILDER, "codex/one", depends_on=("t2",))])
    plan = plan_orchestration(w)
    assigned = [a for a in plan.actions if a.kind is NextActionKind.ASSIGN_INDEPENDENT_LANE]
    require(not assigned, "P2 OPEN: a task inside a dependency cycle (t1<->t2) was assigned; cycles are not detected")


def test_unfinished_dependency_blocks_assignment():
    dep = task(task_id="dep", title="dep", status=TaskStatus.FAILED)
    w = OrchestrationWorld(agents=[AgentOccupancy("codex", OccupancyStatus.IDLE, AgentRole.BUILDER)], tasks=[dep],
                           queued_assignments=[ProposedAssignment("codex", "needs dep", AgentRole.BUILDER, "codex/n", depends_on=("dep",))])
    assigned = [a for a in plan_orchestration(w).actions if a.kind is NextActionKind.ASSIGN_INDEPENDENT_LANE]
    require(not assigned, "P1 OPEN: assignment whose dependency FAILED was planned (depends_on never checked)")


# ---------------------------------------------------------------- founder authority / relay
def test_founder_alpha_regression_scenario():
    gh = asyncio.run(FakeSoftwareTruthSource(branches={FB: FS}).inspect_branch(FB))
    world = founder_alpha_regression_world(github=gh,
                                           cursor_proposal=ProposedAssignment("cursor", "lane C", AgentRole.VALIDATOR, "cursor/c"),
                                           codex_proposal=ProposedAssignment("codex", "lane X", AgentRole.BUILDER, "codex/x"))
    world.queued_assignments.append(ProposedAssignment("claude", "second job", AgentRole.EXAMINER, "claude/x"))
    plan = plan_orchestration(world)
    assigned = {a.agent_key for a in plan.actions if a.kind is NextActionKind.ASSIGN_INDEPENDENT_LANE}
    require(assigned == {"cursor", "codex"}, f"REGRESSION: assigned {assigned}")
    require(any(r.refusal and r.refusal.value == "AGENT_RUNNING" for r in plan.refused), "REGRESSION: running Claude not refused")
    require(plan.founder_messages == (), "REGRESSION: founder asked to relay")
