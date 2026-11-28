"""A1 shadow observation -- read a real external agent, acknowledge a report exactly once,
prepare the next approved task, and never dispatch anything.

The point of these tests is the boundary, not the happy path alone: a malformed or stale report
must never be read as "idle" or "done", a report must never complete an assignment, and the
whole cycle must be able to run without writing a single row.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.agent_coordination.routing import OUTCOME_NO_FEASIBLE_ASSIGNMENT
from app.agent_coordination.service import create_work_assignment, register_agent
from app.agent_coordination.shadow_observation import (
    DECISION_ACKNOWLEDGE_AND_PREPARE_NEXT,
    DECISION_INVESTIGATE,
    DECISION_WAIT,
    STATUS_BLOCKED,
    STATUS_DONE,
    STATUS_STALE,
    STATUS_UNKNOWN,
    STATUS_WORKING,
    AgentReportInvalidError,
    acknowledge_report,
    observe_agents,
    parse_agent_report,
    read_report_files,
    shadow_cycle,
)
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.agent_coordination import (
    AgentWorkAssignmentEvent,
    AgentWorkAssignmentEventType,
    CoordinationAgent,
    WorkAssignmentStatus,
)
from app.models.mainai_execution import MainAITask


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(superuser_db, make_verified_user):
    user, _password = make_verified_user()
    return user.id


def _agent(db, key):
    return register_agent(
        db,
        agent_key=f"{key}-{uuid.uuid4().hex[:8]}",
        display_name=key,
        adapter_kind="cli",
        execution_mode="cli_oneshot",
        supports_read=True,
        supports_write=True,
        concurrency_limit=1,
    )


def _assignment(db, *, owner_id, agent):
    goal = create_goal(
        db,
        owner_id=owner_id,
        title="Shadow observation test",
        original_instruction="Observe an external agent without dispatching to it.",
        created_by="founder",
        approval_policy="autonomous_development_work",
    )
    create_plan(
        db,
        goal=goal,
        rationale="shadow observation test",
        tasks=[PlannedTaskSpec(description="Task 0", task_type="repo_edit")],
        created_by="founder",
    )
    db.commit()
    task = db.query(MainAITask).filter_by(goal_id=goal.id).one()
    assignment = create_work_assignment(
        db,
        owner_id=owner_id,
        goal_id=goal.id,
        task_id=task.id,
        agent_id=agent.id,
        role="builder",
        read_write_mode="read_only",
        repository_identity="lifeai",
        allowed_paths=["backend/app/example.py"],
        requested_by="test",
    )
    db.commit()
    return assignment


def _report_payload(
    *,
    agent_key,
    status,
    assignment_ref=None,
    report_id="r-1",
    reported_at=None,
    extra=None,
):
    payload = {
        "agent_key": agent_key,
        "report_id": report_id,
        "status": status,
        "assignment_ref": assignment_ref,
        "summary": "a bounded summary of what happened",
        "output_refs": [{"kind": "branch", "ref": "freebuff/example"}],
        "reported_at": (reported_at or datetime.now(timezone.utc)).isoformat(),
    }
    if extra:
        payload.update(extra)
    return payload


def _report_events(db):
    """Only the report-acknowledgement events -- create_work_assignment() writes its own
    lifecycle events, which are not what these tests are counting."""

    return (
        db.execute(
            select(AgentWorkAssignmentEvent).where(
                AgentWorkAssignmentEvent.event_type
                == AgentWorkAssignmentEventType.execution_observed
            )
        )
        .scalars()
        .all()
    )


def _write_report(directory, payload, *, name="report.json"):
    path = directory / name
    path.write_text(json.dumps(payload))
    return path


def test_report_schema_is_strict_and_reports_no_repair():
    good = _report_payload(agent_key="freebuff", status=STATUS_WORKING)
    parsed = parse_agent_report(good, source_path="mem", sha256="a" * 64)
    assert parsed.reported_status == STATUS_WORKING
    assert parsed.output_refs == ({"kind": "branch", "ref": "freebuff/example"},)

    with pytest.raises(AgentReportInvalidError, match="unexpected"):
        parse_agent_report(
            {**good, "notes": "extra"}, source_path="mem", sha256="a" * 64
        )
    with pytest.raises(AgentReportInvalidError, match="status must be one of"):
        parse_agent_report(
            {**good, "status": "DONE"}, source_path="mem", sha256="a" * 64
        )
    with pytest.raises(AgentReportInvalidError, match="future"):
        parse_agent_report(
            {
                **good,
                "reported_at": (
                    datetime.now(timezone.utc) + timedelta(hours=2)
                ).isoformat(),
            },
            source_path="mem",
            sha256="a" * 64,
        )


def test_malformed_report_file_is_rejected_not_trusted(superuser_db, owner_id, tmp_path):
    (tmp_path / "broken.json").write_text("{not json")
    _write_report(
        tmp_path,
        _report_payload(agent_key="freebuff", status="DONE"),
        name="unknown-status.json",
    )
    reports, rejected = read_report_files(tmp_path)
    assert reports == ()
    assert len(rejected) == 2
    assert {item["reason"] for item in rejected} == {
        "report is not valid JSON",
        "status must be one of ['ARBETAR', 'BLOCKERAD', 'KLAR'], got 'DONE'",
    }
    cycle = observe_agents(superuser_db, owner_id=owner_id, reports=())
    assert cycle.observations == ()
    assert cycle.rejected_reports == ()


def test_working_agent_is_left_alone_and_nothing_is_written(
    superuser_db, owner_id, tmp_path
):
    agent = _agent(superuser_db, "freebuff")
    assignment = _assignment(superuser_db, owner_id=owner_id, agent=agent)
    _write_report(
        tmp_path,
        _report_payload(
            agent_key=agent.agent_key,
            status=STATUS_WORKING,
            assignment_ref=str(assignment.id),
        ),
    )
    before = _report_events(superuser_db)
    cycle, results = shadow_cycle(
        superuser_db, owner_id=owner_id, report_dir=tmp_path, acknowledge=True
    )
    assert results == ()
    assert len(cycle.observations) == 1
    observation = cycle.observations[0]
    assert observation.status == STATUS_WORKING
    assert observation.decision == DECISION_WAIT
    assert observation.open_assignment_ids == (str(assignment.id),)
    assert len(_report_events(superuser_db)) == len(before)  # ARBETAR never produces a write
    assert assignment.status == WorkAssignmentStatus.ready


def test_stale_report_is_investigate_never_done(superuser_db, owner_id, tmp_path):
    agent = _agent(superuser_db, "freebuff")
    _write_report(
        tmp_path,
        _report_payload(
            agent_key=agent.agent_key,
            status=STATUS_DONE,
            reported_at=datetime.now(timezone.utc) - timedelta(hours=3),
        ),
    )
    cycle = observe_agents(
        superuser_db, owner_id=owner_id, report_dir=tmp_path, fresh_for_seconds=60
    )
    observation = next(
        item for item in cycle.observations if item.agent_key == agent.agent_key
    )
    assert observation.status == STATUS_STALE
    assert observation.decision == DECISION_INVESTIGATE


def test_agent_with_no_report_and_no_work_is_unknown(superuser_db, owner_id):
    agent = _agent(superuser_db, "freebuff")
    cycle = observe_agents(superuser_db, owner_id=owner_id, reports=())
    observation = next(
        item for item in cycle.observations if item.agent_key == agent.agent_key
    )
    assert observation.status == STATUS_UNKNOWN
    assert observation.decision == DECISION_INVESTIGATE


def test_done_report_is_acknowledged_once_and_next_task_only_prepared(
    superuser_db, owner_id, tmp_path
):
    agent = _agent(superuser_db, "freebuff")
    assignment = _assignment(superuser_db, owner_id=owner_id, agent=agent)
    _write_report(
        tmp_path,
        _report_payload(
            agent_key=agent.agent_key,
            status=STATUS_DONE,
            assignment_ref=str(assignment.id),
            report_id="done-1",
        ),
    )
    reports, _rejected = read_report_files(tmp_path)
    first_event, created = acknowledge_report(
        superuser_db, owner_id=owner_id, report=reports[0]
    )
    second_event, created_again = acknowledge_report(
        superuser_db, owner_id=owner_id, report=reports[0]
    )
    assert created is True
    assert created_again is False
    assert first_event == second_event

    cycle, results = shadow_cycle(
        superuser_db, owner_id=owner_id, report_dir=tmp_path, acknowledge=True
    )
    observation = next(
        item for item in cycle.observations if item.agent_key == agent.agent_key
    )
    assert observation.status == STATUS_DONE
    assert observation.decision == DECISION_ACKNOWLEDGE_AND_PREPARE_NEXT
    assert results and results[0]["acknowledged"] is True
    assert results[0]["created"] is False  # replay of the same report
    assert results[0]["dispatch"] == "NOT_PERFORMED"
    # "KLAR" is data: the assignment's canonical state is untouched, so completion still has to
    # come from the real verification chain.
    superuser_db.refresh(assignment)
    assert assignment.status == WorkAssignmentStatus.ready
    events = _report_events(superuser_db)
    assert len(events) == 1
    assert events[0].detail["reported_status"] == STATUS_DONE
    assert events[0].detail["report_sha256"] == reports[0].sha256


@pytest.mark.asyncio
async def test_shadow_adapter_observes_through_the_standard_interface(tmp_path):
    from app.agent_coordination.adapters import (
        ProviderNotConfiguredError,
        ShadowReportAdapter,
    )

    adapter = ShadowReportAdapter(agent_key="freebuff", report_dir=str(tmp_path))
    assert (await adapter.health()).healthy is True
    capabilities = adapter.control_capabilities()
    assert not any(
        (
            capabilities.supports_streaming,
            capabilities.supports_instruction,
            capabilities.supports_resume,
            capabilities.supports_cancel,
            capabilities.supports_structured_events,
        )
    )
    unknown = await adapter.observe(uuid.uuid4())
    assert unknown.raw_status == STATUS_UNKNOWN

    _write_report(
        tmp_path, _report_payload(agent_key="freebuff", status=STATUS_WORKING)
    )
    observed = await adapter.observe(uuid.uuid4())
    assert observed.raw_status == STATUS_WORKING
    assert observed.detail["report_sha256"]

    # A newer report must win over an older one, and an old report on its own must read stale
    # rather than "working": freshness is the only thing that lets a report speak for now.
    stale_dir = tmp_path / "stale"
    stale_dir.mkdir()
    _write_report(
        stale_dir,
        _report_payload(
            agent_key="freebuff",
            status=STATUS_DONE,
            reported_at=datetime.now(timezone.utc) - timedelta(hours=2),
        ),
        name="old.json",
    )
    stale = await ShadowReportAdapter(
        agent_key="freebuff", report_dir=str(stale_dir), fresh_for_seconds=60
    ).observe(uuid.uuid4())
    assert stale.raw_status == "stale"
    _write_report(
        stale_dir,
        _report_payload(agent_key="freebuff", status=STATUS_BLOCKED),
        name="new.json",
    )
    newest = await ShadowReportAdapter(
        agent_key="freebuff", report_dir=str(stale_dir), fresh_for_seconds=60
    ).observe(uuid.uuid4())
    assert newest.raw_status == STATUS_BLOCKED

    # Observation only: the control plane's own capability check already refuses these, and the
    # adapter refuses them too if called directly.
    for coroutine in (
        adapter.start_assignment(uuid.uuid4()),
        adapter.send_instruction(uuid.uuid4(), "do more"),
        adapter.cancel(uuid.uuid4()),
        adapter.resume(uuid.uuid4()),
        adapter.collect_result(uuid.uuid4()),
    ):
        with pytest.raises(ProviderNotConfiguredError, match="observation-only"):
            await coroutine


def test_report_for_another_agents_assignment_is_rejected(superuser_db, owner_id, tmp_path):
    owner_agent = _agent(superuser_db, "freebuff")
    other_agent = _agent(superuser_db, "codex")
    assignment = _assignment(superuser_db, owner_id=owner_id, agent=owner_agent)
    _write_report(
        tmp_path,
        _report_payload(
            agent_key=other_agent.agent_key,
            status=STATUS_DONE,
            assignment_ref=str(assignment.id),
        ),
    )
    reports, _rejected = read_report_files(tmp_path)
    with pytest.raises(AgentReportInvalidError, match="does not own"):
        acknowledge_report(superuser_db, owner_id=owner_id, report=reports[0])
    assert _report_events(superuser_db) == []


def test_unregistered_agent_report_prepares_nothing(superuser_db, owner_id, tmp_path):
    _write_report(
        tmp_path, _report_payload(agent_key="ghost-agent", status=STATUS_DONE)
    )
    reports, _rejected = read_report_files(tmp_path)
    cycle = observe_agents(superuser_db, owner_id=owner_id, reports=reports)
    observation = next(
        item for item in cycle.observations if item.agent_key == "ghost-agent"
    )
    assert observation.decision == DECISION_ACKNOWLEDGE_AND_PREPARE_NEXT
    assert (
        superuser_db.execute(
            select(CoordinationAgent).where(
                CoordinationAgent.agent_key == "ghost-agent"
            )
        ).scalar_one_or_none()
        is None
    )
    assert OUTCOME_NO_FEASIBLE_ASSIGNMENT  # outcome vocabulary is the existing routing one
