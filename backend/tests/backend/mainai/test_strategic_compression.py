"""app.mainai_executive.strategic_compression -- proposes ONE coherent grouping over several
related WorkCandidate/IntelligenceIdea rows via the real app.memory_threads mechanism, rather
than treating them as N independent items. Don't-spam bar (reconciliation doc §9): 8 related
suggestions must not become 8 separately-recommended active items by default."""

from __future__ import annotations

import uuid

import pytest

from app.intelligence_governance.service import record_execution, record_idea
from app.mainai_executive.strategic_compression import (
    StrategicCompressionError,
    compress_into_program,
)
from app.memory_threads.service import thread_members
from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask
from app.work_candidates.service import record_work_candidate

from tests.backend.mainai.test_work_candidates import _owner_with_entity


def _task(db, owner_id):
    goal = MainAIGoal(owner_id=owner_id, title="p", original_instruction="s", created_by="test")
    db.add(goal)
    db.flush()
    plan = MainAIPlan(owner_id=owner_id, goal_id=goal.id, version=1, rationale="r", created_by="test")
    db.add(plan)
    db.flush()
    task = MainAITask(owner_id=owner_id, goal_id=goal.id, plan_id=plan.id, description="d", task_type="repo_edit")
    db.add(task)
    db.flush()
    return task


def test_eight_related_suggestions_become_one_proposal_not_eight(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    candidate_ids = []
    for i in range(8):
        candidate = record_work_candidate(
            superuser_db, owner_id=owner.id, source_entity_id=entity.id, title=f"related suggestion {i}",
            idempotency_key=f"sc-wc-{uuid.uuid4()}", classifier_strategy="test",
            provenance={"tags": ["same-program"]},
        )
        candidate_ids.append(candidate.id)
    superuser_db.commit()

    result = compress_into_program(
        superuser_db, owner_id=owner.id, idempotency_key=f"sc-prog-{uuid.uuid4()}",
        work_candidate_ids=candidate_ids,
    )
    superuser_db.commit()

    # The don't-spam bar itself: exactly ONE proposal, not 8.
    assert result["proposal_count"] == 1
    assert result["member_count"] == 8
    assert len(result["work_candidate_ids"]) == 8

    # None of the 8 underlying candidates were authorized as a side effect of grouping them.
    from sqlalchemy import select
    from app.models.work_candidate import WorkCandidate

    rows = superuser_db.execute(select(WorkCandidate).where(WorkCandidate.id.in_(candidate_ids))).scalars().all()
    assert len(rows) == 8
    assert all(r.status == "unreviewed" for r in rows)
    assert all(r.authorized_goal_id is None for r in rows)

    # The real MemoryThread actually exists and contains all 8 as members.
    members = thread_members(superuser_db, owner_id=owner.id, thread_id=uuid.UUID(result["program_thread_id"]))
    active_member_refs = {m.member_ref_id for m in members if m.state == "active"}
    assert active_member_refs == {str(cid) for cid in candidate_ids}


def test_compresses_mixed_work_candidates_and_ideas(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    candidate = record_work_candidate(
        superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="wc side",
        idempotency_key=f"sc-wc-{uuid.uuid4()}", classifier_strategy="test",
    )
    task = _task(superuser_db, owner.id)
    execution = record_execution(superuser_db, owner_id=owner.id, task_id=task.id, idempotency_key=f"sc-exec-{uuid.uuid4()}", role="builder")
    idea = record_idea(
        superuser_db, owner_id=owner.id, execution_id=execution.id, idea_kind="idea",
        content="idea side", idempotency_key=f"sc-idea-{uuid.uuid4()}",
    )
    superuser_db.commit()

    result = compress_into_program(
        superuser_db, owner_id=owner.id, idempotency_key=f"sc-prog-{uuid.uuid4()}",
        work_candidate_ids=[candidate.id], idea_ids=[idea.id],
    )
    superuser_db.commit()

    assert result["proposal_count"] == 1
    assert result["member_count"] == 2
    assert result["work_candidate_ids"] == [str(candidate.id)]
    assert result["idea_ids"] == [str(idea.id)]


def test_requires_at_least_two_items(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    candidate = record_work_candidate(
        superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="lone item",
        idempotency_key=f"sc-wc-{uuid.uuid4()}", classifier_strategy="test",
    )
    superuser_db.commit()

    with pytest.raises(StrategicCompressionError):
        compress_into_program(
            superuser_db, owner_id=owner.id, idempotency_key=f"sc-prog-{uuid.uuid4()}",
            work_candidate_ids=[candidate.id],
        )


def test_rejects_foreign_owner_work_candidate(superuser_db):
    owner_a, entity_a = _owner_with_entity(superuser_db)
    owner_b, entity_b = _owner_with_entity(superuser_db)
    superuser_db.commit()
    candidate_a = record_work_candidate(
        superuser_db, owner_id=owner_a.id, source_entity_id=entity_a.id, title="a",
        idempotency_key=f"sc-wc-{uuid.uuid4()}", classifier_strategy="test",
    )
    candidate_b = record_work_candidate(
        superuser_db, owner_id=owner_b.id, source_entity_id=entity_b.id, title="b",
        idempotency_key=f"sc-wc-{uuid.uuid4()}", classifier_strategy="test",
    )
    superuser_db.commit()

    with pytest.raises(StrategicCompressionError):
        compress_into_program(
            superuser_db, owner_id=owner_a.id, idempotency_key=f"sc-prog-{uuid.uuid4()}",
            work_candidate_ids=[candidate_a.id, candidate_b.id],
        )


def test_never_calls_authorize_work_candidate():
    """Structural: this module must never import or call the one function that can create a
    real MainAIGoal -- grouping is not authorization. Uses `ast` (not a plain substring search)
    so the module's own prose -- which legitimately DISCUSSES authorize_work_candidate() in
    English, to explain why it is absent -- cannot produce a false failure."""
    import ast
    import inspect

    import app.mainai_executive.strategic_compression as module

    tree = ast.parse(inspect.getsource(module))
    names_referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names_referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            names_referenced.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names_referenced.add((alias.asname or alias.name).split(".")[-1])
    assert "authorize_work_candidate" not in names_referenced
