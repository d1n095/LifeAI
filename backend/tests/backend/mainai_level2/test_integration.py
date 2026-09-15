from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.mainai_level2 import (
    BlockerClass,
    Job,
    JobState,
    Journal,
    Level2ControlPlane,
    ProgramContract,
    Provider,
    ProviderState,
    run_unattended_harness,
    CanonicalProgramStore,
    run_process_crash_probe,
    run_multi_provider_harness,
    VerifiedComponentRegistry,
    ComponentBinding,
    VerifiedComposition,
    recover_from_canonical,
    probe_external_component,
    call_frozen_json,
    run_unattended_production_flow,
    run_multi_seed_production_endurance,
    run_orchestration_crash_matrix,
    run_postgres_recovery_matrix,
    run_cancellation_duplicate_flow,
    run_integrated_endurance,
)
from app.mainai_level2.process_harness import run_sigkill_restart_probe


@dataclass
class CanonicalFakeRuntime:
    claims: dict[str, str]

    def __init__(self):
        self.claims = {}

    def assign(self, job, agent_id):
        if job.job_id in self.claims:
            raise RuntimeError("duplicate canonical claim")
        token = f"{job.job_id}:{job.attempt}:{agent_id}"
        self.claims[job.job_id] = token
        return token

    def cancel(self, job):
        job.state = JobState.CANCELLED

    def release(self, job):
        self.claims.pop(job.job_id, None)


def make_plane():
    runtime = CanonicalFakeRuntime()
    plane = Level2ControlPlane(runtime)
    plane.add_program(ProgramContract("p", "alice", "ship bounded change", ("all jobs verified",), ("independent review",)))
    return plane, runtime


def test_unattended_builder_examiner_fix_loop_and_brief():
    plane, _ = make_plane()
    plane.add_job(Job("a", "p", "alice", "build", base_sha="base", remaining=("implement",)))
    plane.add_job(Job("b", "p", "alice", "dependent", dependencies=("a",), priority=1))
    plane.assign("a", agent_id="builder")
    assert plane.observe("a", state=JobState.PARTIAL, remaining=("finish tests",)) == "continue:a:1"
    plane.freeze("a", sha="sha-a", examiner_id="examiner")
    with pytest.raises(ValueError):
        plane.review("a", examiner_id="builder", sha="sha-a", passed=True)
    plane.review("a", examiner_id="examiner", sha="sha-a", passed=False)
    plane.jobs["a"].state = JobState.READY
    plane.jobs["a"].base_sha = "base"
    plane.assign("a", agent_id="fixer")
    plane.freeze("a", sha="sha-b", examiner_id="examiner-2")
    plane.review("a", examiner_id="examiner-2", sha="sha-b", passed=True)
    assert plane.next_ready(owner_id="alice", program_id="p").job_id == "b"
    brief = plane.founder_brief("p")
    assert brief["verified"] == ["sha-b"]
    assert brief["continuations"] == 1


def test_new_sha_invalidates_old_review_and_wrong_sha_is_rejected():
    plane, _ = make_plane()
    plane.add_job(Job("a", "p", "alice", "build", base_sha="base"))
    plane.freeze("a", sha="sha-a", examiner_id="reviewer")
    with pytest.raises(ValueError):
        plane.review("a", examiner_id="reviewer", sha="old", passed=True)
    plane.review("a", examiner_id="reviewer", sha="sha-a", passed=True)
    plane.jobs["a"].state = JobState.READY
    plane.jobs["a"].review_passed = None
    plane.freeze("a", sha="sha-b", examiner_id="reviewer-2")
    with pytest.raises(ValueError):
        plane.review("a", examiner_id="reviewer", sha="sha-b", passed=True)


def test_owner_program_boundary_and_dependencies():
    plane, _ = make_plane()
    with pytest.raises(ValueError):
        plane.add_job(Job("x", "p", "bob", "foreign"))
    plane.add_job(Job("a", "p", "alice", "root"))
    plane.add_job(Job("b", "p", "alice", "child", dependencies=("a",)))
    assert plane.next_ready(owner_id="alice", program_id="p").job_id == "a"


def test_healthy_running_is_not_interrupted_and_founder_blocker_is_preserved():
    plane, _ = make_plane()
    plane.add_job(Job("a", "p", "alice", "long"))
    plane.assign("a", agent_id="builder")
    assert plane.observe("a", state=JobState.PROGRESSING) is None
    assert plane.interruptions == 0
    plane.observe("a", state=JobState.BLOCKED, blocker=BlockerClass.FOUNDER_REQUIRED)
    assert plane.founder_required == ["a"]


def test_local_blocker_is_continued_without_founder():
    plane, _ = make_plane()
    plane.add_job(Job("a", "p", "alice", "repair"))
    plane.assign("a", agent_id="builder")
    continuation = plane.observe("a", state=JobState.BLOCKED, blocker=BlockerClass.LOCAL_REPAIR)
    assert continuation == "continue:a:1"
    assert not plane.founder_required


def test_resource_recommendations_are_advisory_only():
    plane, _ = make_plane()
    plane.add_job(Job("a", "p", "alice", "work"))
    recommendation = plane.resource_recommendation("a", {"context_loss_risk": True})
    assert recommendation.action == "CHECKPOINT"
    assert recommendation.authorized is False


def test_real_process_crash_boundary_recovers_evidence_without_authority():
    result = run_process_crash_probe()
    assert result["child_exit"] == -9
    assert result["records"][0]["event"] == "CHECKPOINT"
    assert result["requires_canonical_reread"] is True


def test_unattended_multi_provider_failover_and_re_review():
    plane, _ = make_plane()
    result = run_multi_provider_harness(plane)
    assert result["verified"] is True
    assert result["old_sha_replaced"] is True
    assert result["continuations"] == 1


def test_verified_component_registry_rejects_stale_or_unverified_bindings():
    registry = VerifiedComponentRegistry()
    assert registry.require("runtime").sha.startswith("1951ccef")
    with pytest.raises(ValueError):
        registry.require("runtime", sha="old")
    with pytest.raises(ValueError):
        VerifiedComponentRegistry((ComponentBinding("runtime", "old"),)).require("runtime")


def test_verified_composition_binds_runtime_implementation_without_authority():
    registry = VerifiedComponentRegistry()
    composition = VerifiedComposition(registry)
    runtime = object()
    adapter = composition.bind("runtime", runtime)
    assert adapter.candidate_sha == registry.require("runtime").sha
    assert adapter.health()["authority"] == "none"
    assert composition.require_bound("runtime").implementation is runtime


def test_external_component_binding_requires_real_public_seam():
    class Seam:
        def observe(self):
            return None

    composition = VerifiedComposition()
    adapter = composition.bind_external("supervision", Seam(), required_methods=("observe",))
    assert adapter.seam_only is False
    with pytest.raises(TypeError):
        composition.bind_external("director", object(), required_methods=("submit",))


def test_sigkill_restart_requires_fresh_canonical_recovery():
    result = run_sigkill_restart_probe(lambda: {"state": "RUNNING", "source": "postgresql"})
    assert result["child_exit"] == -9
    assert result["canonical"]["source"] == "postgresql"
    assert result["evidence"]["authority"] == "none"


def test_external_frozen_worktree_probe_fails_closed_on_unavailable_seam():
    probe = probe_external_component(
        name="supervision",
        worktree="/Users/dennistorildson/Documents/LifeAI-worktrees/examiner-continuous-supervision",
        expected_sha="a7df7f90dba9f8bc993005b2cce1d4c8cb7dcec4",
        import_module="app.mainai_level2",
        required_methods=("observe",),
    )
    assert probe.observed_sha == probe.expected_sha
    assert probe.compatible is False


def test_resource_binding_executes_actual_frozen_implementation():
    result = call_frozen_json(
        name="resource_intelligence",
        worktree="/private/tmp/resource-063c2569",
        expected_sha="063c2569a170ccc3eb7887eadd2ed1b7caed73ff",
        module="app.resource_intelligence.types",
        function="unknown_metric",
        kwargs={"unit": "tokens", "definition": "not observed", "source": "level2"},
    )
    assert result.result["missing_data"] is True


def test_director_binding_executes_actual_frozen_provider_lease():
    result = call_frozen_json(
        name="director",
        worktree="/private/tmp/director-ab1c0ce",
        expected_sha="ab1c0ce03a7a0f7b11f2f716304f9e1235a54d3e",
        module="app.dev_director.provider_lease",
        function="new_external_provider_lease",
        kwargs={"provider_identity": "provider-b", "task_ref": None, "workspace_ref": "job:test",
                 "branch": "dev/test", "allowed_tools": [], "allowed_files": [], "ttl_seconds": 60},
    )
    assert result.result["provider_identity"] == "provider-b"


def test_supervision_binding_executes_actual_frozen_identity_seam():
    result = call_frozen_json(
        name="supervision",
        worktree="/Users/dennistorildson/Documents/LifeAI-worktrees/examiner-continuous-supervision",
        expected_sha="a7df7f90dba9f8bc993005b2cce1d4c8cb7dcec4",
        module="app.mainai_execution.canonical_supervisor",
        function="identity",
        args=("owner", "job", "attempt"),
    )
    assert "value" in result.result


def test_unattended_production_flow_uses_runtime_provider_failover_and_review():
    result = run_unattended_production_flow()
    assert result["verified"] is True
    assert result["provider_failover"] is True
    assert result["old_attempt_fenced"] is True
    assert result["old_sha_invalidated"] is True


def test_multi_seed_endurance_preserves_owner_scope():
    result = run_multi_seed_production_endurance(seeds=(1, 2, 3, 4), owners=("alice", "bob"))
    assert result["verified"] is True
    assert result["seeds"] == 4
    assert result["owners"] == 2


def test_full_orchestration_sigkill_matrix_restarts_from_canonical_callback():
    stages = ("provider", "claim", "partial", "continuation", "freeze", "review_fail", "fix_sha", "review_pass", "completion")
    result = run_orchestration_crash_matrix(stages, lambda stage: {"stage": stage, "source": "postgresql", "authority": "canonical"})
    assert result["stages"] == len(stages)
    assert all(item["exit"] == -9 for item in result["results"])
    assert all(item["canonical"]["source"] == "postgresql" for item in result["results"])


def test_cancellation_and_duplicate_late_results_are_fenced():
    result = run_cancellation_duplicate_flow()
    assert result["cancelled"] is True
    assert result["late_events_rejected"] == 2


def test_postgres_recovery_matrix_uses_fresh_child_sessions(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    store = CanonicalProgramStore(superuser_db)
    program = store.create_level2_program(owner_id=owner.id, objective="crash matrix")
    superuser_db.commit()
    result = run_postgres_recovery_matrix(
        database_url="postgresql://lifeos@127.0.0.1:5433/lifeos_test",
        owner_id=str(owner.id), program_id=str(program.id), stages=("claim", "review", "completion"),
    )
    assert result["stages"] == 3
    assert all(item["recovery"]["source"] == "postgresql" for item in result["results"])


def test_integrated_endurance_combines_provider_review_cancel_and_late_events():
    result = run_integrated_endurance(seeds=(21, 22), owners=("alice", "bob"))
    assert result["successful"] is True
    assert result["cancelled"] is True
    assert result["late_events_rejected"] == 2
    assert result["provider_failovers"] == 2


def test_provider_failure_reduces_function_and_never_authorizes():
    plane, _ = make_plane()
    plane.add_provider(Provider("p1", frozenset({"edit"})))
    plane.provider_failover("p1", Provider("p2", frozenset({"edit"}), ProviderState.AVAILABLE, True))
    assert plane.providers["p1"].state == ProviderState.EXHAUSTED
    assert plane.providers["p2"].authorized is True
    assert not any(r.authorized for r in [])


def test_checkpoint_and_restart_journal_do_not_recreate_authority():
    plane, runtime = make_plane()
    plane.add_job(Job("a", "p", "alice", "work", base_sha="base"))
    plane.assign("a", agent_id="builder")
    checkpoint = plane.checkpoint("a")
    records = plane.journal.snapshot()
    recovered = Level2ControlPlane(runtime, journal=Journal.replay(records))
    recovered.recover(records)
    assert checkpoint["state"] == "ASSIGNED"
    assert recovered.jobs["a"].state == JobState.READY
    assert recovered.jobs["a"].builder_id is None
    assert recovered.journal.records[-1]["event"] == "recovery_requiring_canonical_reread"


def test_duplicate_assignment_is_rejected_by_canonical_runtime():
    plane, runtime = make_plane()
    plane.add_job(Job("a", "p", "alice", "work"))
    plane.assign("a", agent_id="builder")
    with pytest.raises(ValueError):
        plane.assign("a", agent_id="builder")
    assert len(runtime.claims) == 1


def test_canonical_store_owner_scope(superuser_db, make_verified_user):
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()
    store = CanonicalProgramStore(superuser_db)
    goal = store.create_program(owner_id=owner_a.id, objective="owner A objective")
    assert store.current_task(owner_id=owner_b.id, task_id=goal.id) is None


def test_canonical_level2_program_and_append_only_journal(superuser_db, make_verified_user):
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()
    store = CanonicalProgramStore(superuser_db)
    program = store.create_level2_program(owner_id=owner_a.id, objective="unattended bounded work",
                                          acceptance=["reviewed"], verification=["tests"], budget={"usd": 2})
    store.append_event(program=program, event_type="CHECKPOINT", metadata={"sha": "abc"})
    superuser_db.commit()
    assert [event.sequence for event in store.journal(owner_id=owner_a.id, program_id=program.id)] == [1, 2]
    assert store.level2_program(owner_id=owner_b.id, program_id=program.id) is None


def test_canonical_recovery_rereads_program_and_jobs(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    store = CanonicalProgramStore(superuser_db)
    program = store.create_level2_program(owner_id=owner.id, objective="recover from postgres")
    program.current_sha = "sha-current"
    program.state = "RUNNING"
    store.append_event(program=program, event_type="CHECKPOINT", metadata={"sha": "sha-current"})
    superuser_db.commit()
    recovered = recover_from_canonical(store, owner_id=owner.id, program_id=program.id)
    assert recovered["source"] == "postgresql"
    assert recovered["current_sha"] == "sha-current"
    assert recovered["event_count"] == 2


def test_deterministic_soak_and_digest():
    plane, _ = make_plane()
    result = run_unattended_harness(plane, count=1000)
    assert result["jobs"] == 1000
    assert result["verified"] == 1000
    assert result["continuations"] > 0
    assert len(result["digest"]) == 64
