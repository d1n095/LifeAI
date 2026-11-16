"""`app.mainai_cognitive_ops.situational_awareness` + `duplication_control`. See
docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md."""

from __future__ import annotations

from app.mainai_cognitive_ops.duplication_control import assess_duplication
from app.mainai_cognitive_ops.situational_awareness import (
    assess_program_completion_claim,
    filter_known_active_work,
    is_assignable,
    select_assignable_agents,
)
from app.mainai_cognitive_ops.types import AgentState, DuplicationVerdict, ProgramStatus, WorkItem


def test_busy_agent_is_not_assignable():
    assert is_assignable(AgentState(agent_id="a1", busy=True)) is False


def test_idle_existing_agent_is_assignable():
    agent = AgentState(agent_id="a1", idle=True)
    assert is_assignable(agent) is True
    assert select_assignable_agents((agent, AgentState(agent_id="a2", busy=True))) == (agent,)


def test_nonexistent_agent_is_not_assignable_even_if_not_busy():
    assert is_assignable(AgentState(agent_id="ghost", exists=False)) is False


def test_subtask_complete_is_not_program_complete():
    subtasks = (
        WorkItem(item_id="1", program="P", subtask="a", owner_agent=None, status=ProgramStatus.COMPLETE),
        WorkItem(item_id="2", program="P", subtask="b", owner_agent=None, status=ProgramStatus.ACTIVE),
    )
    claim = assess_program_completion_claim(program="P", subtasks=subtasks)
    assert claim.claimed_status == ProgramStatus.PARTIAL


def test_program_complete_only_when_every_subtask_complete():
    subtasks = (
        WorkItem(item_id="1", program="P", subtask="a", owner_agent=None, status=ProgramStatus.COMPLETE),
        WorkItem(item_id="2", program="P", subtask="b", owner_agent=None, status=ProgramStatus.COMPLETE),
    )
    claim = assess_program_completion_claim(program="P", subtasks=subtasks)
    assert claim.claimed_status == ProgramStatus.COMPLETE


def test_known_active_work_is_not_treated_as_new_task():
    active = (WorkItem(item_id="1", program="P", subtask="x", owner_agent="a1", status=ProgramStatus.ACTIVE, keywords=("egress", "vault")),)
    matches = filter_known_active_work(candidate_task_keywords=("vault", "egress"), active_work=active)
    assert matches == active


def test_independent_examiner_duplication_is_allowed():
    existing = (WorkItem(item_id="1", program="P", subtask="review", owner_agent="codex", status=ProgramStatus.ACTIVE, keywords=("recall", "review")),)
    candidate = WorkItem(item_id="2", program="P", subtask="review", owner_agent="claude", status=ProgramStatus.ACTIVE, keywords=("recall", "review"))
    result = assess_duplication(candidate=candidate, existing_work=existing, is_independent_examiner_context=True)
    assert result.verdict == DuplicationVerdict.INTENTIONAL_EXAMINER_DUPLICATE


def test_accidental_duplicate_is_flagged_as_waste_not_examiner_context():
    existing = (WorkItem(item_id="1", program="P", subtask="build x", owner_agent="claude", status=ProgramStatus.ACTIVE, keywords=("vision", "compiler")),)
    candidate = WorkItem(item_id="2", program="P", subtask="build x again", owner_agent="claude", status=ProgramStatus.ACTIVE, keywords=("vision", "compiler"))
    result = assess_duplication(candidate=candidate, existing_work=existing, is_independent_examiner_context=False)
    assert result.verdict == DuplicationVerdict.ACCIDENTAL_DUPLICATE
    assert result.recommendation == "link"


def test_no_overlap_is_not_duplicate():
    existing = (WorkItem(item_id="1", program="P", subtask="a", owner_agent=None, status=ProgramStatus.ACTIVE, keywords=("alpha",)),)
    candidate = WorkItem(item_id="2", program="P", subtask="b", owner_agent=None, status=ProgramStatus.ACTIVE, keywords=("beta",))
    result = assess_duplication(candidate=candidate, existing_work=existing)
    assert result.verdict == DuplicationVerdict.NOT_DUPLICATE
