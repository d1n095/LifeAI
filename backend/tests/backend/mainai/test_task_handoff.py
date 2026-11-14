"""app/mainai_execution/task_handoff.py -- Continuous Build Directive Section B/C: a real
next-task selection and minimal-sufficient-context handoff built from existing durable state
and existing functions, never a parallel reimplementation. Proves:
  - select_next_task() returns the SAME task app.mainai_execution.graph.next_ready_task()
    would, falling back to the highest-urgency unreviewed GithubEvidenceCandidate only when
    no task is ready, and an honest "nothing to do" result otherwise.
  - build_task_handoff_prompt() surfaces real evidence (goal, dependencies, linked GitHub
    evidence, authorized execution scope) and real constraints (approval policy), and asks a
    question ONLY when something genuinely required is actually missing -- never a generic
    filler section."""

import uuid

from app.execution_envelopes.service import (
    authorize_execution_scope,
    propose_execution_scope,
)
from app.github_evidence import (
    authorize_github_evidence_candidate,
    record_github_evidence_candidates,
)
from app.mainai_execution import planner
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.task_handoff import (
    build_task_handoff_prompt,
    select_next_task,
)
from app.models.mainai_execution import MainAITask
from app.models.user import User
from app.safe_planner.github_work_queue import WorkQueueItem


def _owner(db) -> User:
    user = User(email=f"handoff-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def _goal_and_ready_task(db, owner, *, approval_policy="standard_repo_work", task_type="repo_edit", verification_plan=None):
    goal = planner.create_goal(
        db, owner_id=owner.id, title="Test goal", original_instruction="Do the thing, carefully.",
        created_by="test", approval_policy=approval_policy,
    )
    planner.create_plan(
        db, goal=goal, rationale="one task", created_by="test",
        tasks=[PlannedTaskSpec(description="Do the first part", task_type=task_type, verification_plan=verification_plan or [])],
    )
    db.flush()
    task = db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    return goal, task


def _item(number: int, *, kind: str = "open_pull_request", signals: tuple[str, ...] = ()) -> WorkQueueItem:
    return WorkQueueItem(
        kind=kind, number=number, title=f"PR {number}",
        html_url=f"https://github.com/d1n095/LifeAI/pull/{number}", updated_at="2026-11-14T00:00:00Z", signals=signals,
    )


def test_select_next_task_returns_the_ready_task_when_one_exists(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    goal, task = _goal_and_ready_task(superuser_db, owner)
    superuser_db.commit()

    selection = select_next_task(superuser_db, owner_id=owner.id)

    assert selection.source == "ready_task"
    assert selection.task.id == task.id
    assert selection.goal.id == goal.id
    assert selection.evidence_candidate is None


def test_select_next_task_prefers_a_ready_task_over_unreviewed_github_evidence(superuser_db):
    """A ready, already-planned task must never be skipped in favor of a merely-observed
    GitHub signal -- real planned work outranks a proposal every time."""
    owner = _owner(superuser_db)
    superuser_db.commit()
    _goal, task = _goal_and_ready_task(superuser_db, owner)
    record_github_evidence_candidates(
        superuser_db, owner_id=owner.id, source_repo="d1n095/LifeAI", items=[_item(1, signals=("ci_red",))]
    )
    superuser_db.commit()

    selection = select_next_task(superuser_db, owner_id=owner.id)

    assert selection.source == "ready_task"
    assert selection.task.id == task.id


def test_select_next_task_falls_back_to_the_highest_urgency_unreviewed_evidence(superuser_db):
    """With no ready task, the most urgent unreviewed candidate (ci_red beats a merely stale
    draft) is surfaced -- but ONLY as a proposal, never as if it were already authorized."""
    owner = _owner(superuser_db)
    superuser_db.commit()
    record_github_evidence_candidates(
        superuser_db, owner_id=owner.id, source_repo="d1n095/LifeAI",
        items=[_item(1, signals=("draft",)), _item(2, signals=("ci_red",))],
    )
    superuser_db.commit()

    selection = select_next_task(superuser_db, owner_id=owner.id)

    assert selection.source == "github_evidence_proposal"
    assert selection.evidence_candidate.source_number == 2
    assert selection.task is None
    assert "PROPOSAL" in selection.reason


def test_select_next_task_ignores_already_reviewed_evidence_candidates(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    rows = record_github_evidence_candidates(
        superuser_db, owner_id=owner.id, source_repo="d1n095/LifeAI", items=[_item(1, signals=("ci_red",))]
    )
    superuser_db.commit()
    authorize_github_evidence_candidate(superuser_db, owner_id=owner.id, candidate_id=rows[0].id, authorized_by="founder")
    superuser_db.commit()

    selection = select_next_task(superuser_db, owner_id=owner.id)

    # authorize_github_evidence_candidate() creates a goal, not a planned task (see its own
    # docstring) -- so no ready task exists, and the only evidence candidate is now
    # "authorized", not "unreviewed", so it must not be surfaced again either.
    assert selection.source == "none"
    assert selection.task is None
    assert selection.evidence_candidate is None


def test_select_next_task_reports_an_honest_empty_result_when_there_is_nothing_to_do(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()

    selection = select_next_task(superuser_db, owner_id=owner.id)

    assert selection.source == "none"
    assert selection.task is None
    assert selection.evidence_candidate is None
    assert selection.reason


def test_select_next_task_is_scoped_to_the_given_owner(superuser_db):
    owner_a = _owner(superuser_db)
    owner_b = _owner(superuser_db)
    superuser_db.commit()
    _goal_and_ready_task(superuser_db, owner_a)
    superuser_db.commit()

    selection = select_next_task(superuser_db, owner_id=owner_b.id)

    assert selection.source == "none"


def test_handoff_prompt_carries_the_objective_and_real_acceptance_criteria(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    verification_plan = [{"kind": "targeted_tests", "target": "tests/backend/mainai/test_task_handoff.py"}]
    goal, task = _goal_and_ready_task(superuser_db, owner, verification_plan=verification_plan)
    superuser_db.commit()

    prompt = build_task_handoff_prompt(superuser_db, task=task, goal=goal)

    assert prompt.objective == task.description
    assert prompt.acceptance_criteria == [str(verification_plan[0])]
    assert "Acceptance criteria:" in prompt.render()
    assert "missing" not in prompt.render().lower()


def test_handoff_prompt_flags_missing_acceptance_criteria_when_none_are_recorded(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    goal, task = _goal_and_ready_task(superuser_db, owner, verification_plan=[])
    superuser_db.commit()

    prompt = build_task_handoff_prompt(superuser_db, task=task, goal=goal)

    assert prompt.acceptance_criteria == []
    assert any("No acceptance criteria" in q for q in prompt.missing_information)
    assert "Before starting, please answer:" in prompt.render()


def test_handoff_prompt_surfaces_the_linked_github_evidence_as_real_url_evidence(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    rows = record_github_evidence_candidates(
        superuser_db, owner_id=owner.id, source_repo="d1n095/LifeAI", items=[_item(7, signals=("ci_red",))]
    )
    superuser_db.commit()
    _candidate, goal = authorize_github_evidence_candidate(
        superuser_db, owner_id=owner.id, candidate_id=rows[0].id, authorized_by="founder"
    )
    superuser_db.commit()
    planner.create_plan(
        superuser_db, goal=goal, rationale="fix the red build", created_by="test",
        tasks=[PlannedTaskSpec(description="Fix the failing check", task_type="repo_edit")],
    )
    superuser_db.commit()
    task = superuser_db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()

    prompt = build_task_handoff_prompt(superuser_db, task=task, goal=goal)

    assert any("d1n095/LifeAI#7" in e for e in prompt.evidence)
    assert any("ci_red" in e for e in prompt.evidence)


def test_handoff_prompt_surfaces_an_active_execution_envelope_as_evidence(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    goal, task = _goal_and_ready_task(superuser_db, owner, approval_policy="autonomous_development_work")
    superuser_db.commit()
    proposal = propose_execution_scope(
        superuser_db, owner_id=owner.id, goal_id=goal.id, idempotency_key=f"prop-{uuid.uuid4()}",
        repository_identity="d1n095/LifeAI", proposed_paths=["backend/app"],
    )
    superuser_db.commit()
    authorize_execution_scope(
        superuser_db, owner_id=owner.id, proposal_id=proposal.id, authorized_by="founder",
        authorized_paths=["backend/app"], authorized_capabilities=["repo_edit"], authorized_risk="low",
        envelope_idempotency_key=f"env-{uuid.uuid4()}",
    )
    superuser_db.commit()

    prompt = build_task_handoff_prompt(superuser_db, task=task, goal=goal)

    assert any("d1n095/LifeAI" in e and "Authorized repository scope" in e for e in prompt.evidence)
    assert not any("founder authorization is needed" in q for q in prompt.missing_information)


def test_handoff_prompt_asks_for_founder_authorization_when_autonomous_policy_has_no_envelope_yet(superuser_db):
    """An autonomous-policy goal with no active execution envelope is exactly the "missing
    information" case the directive asks for -- a real, necessary question, not a generic one."""
    owner = _owner(superuser_db)
    superuser_db.commit()
    goal, task = _goal_and_ready_task(superuser_db, owner, approval_policy="autonomous_development_work")
    superuser_db.commit()

    prompt = build_task_handoff_prompt(superuser_db, task=task, goal=goal)

    assert any("founder authorization is needed" in q for q in prompt.missing_information)


def test_handoff_prompt_never_asks_about_envelopes_for_the_standard_human_reviewed_policy(superuser_db):
    """standard_repo_work goals are human-reviewed per-goal already (see
    app.mainai_execution.approval's own module docstring) -- they must never be flagged as
    missing an execution envelope, which is a concept specific to autonomous_development_work."""
    owner = _owner(superuser_db)
    superuser_db.commit()
    goal, task = _goal_and_ready_task(superuser_db, owner, approval_policy="standard_repo_work")
    superuser_db.commit()

    prompt = build_task_handoff_prompt(superuser_db, task=task, goal=goal)

    assert not any("execution authorization envelope" in q for q in prompt.missing_information)


def test_render_omits_sections_with_no_content_rather_than_printing_empty_headings():
    from app.mainai_execution.task_handoff import TaskHandoffPrompt

    prompt = TaskHandoffPrompt(objective="Do the thing.")

    text = prompt.render()
    assert text == "Objective: Do the thing."
    assert "Evidence:" not in text
    assert "Constraints:" not in text
    assert "Acceptance criteria:" not in text
    assert "Before starting" not in text
