"""PI-1d — a failed proposal must not leave the backend's live checkout modified.

Proposal mode (`GITHUB_WRITE_ENABLED` off) is the original V0.1 behaviour: the code agent's
content is written straight into the single checkout this backend process runs from, so
`verify_task()` can verify against real content. That is a deliberate design decision, not the
defect. The defect (found by reading the code, reproduced here) is what happened *afterwards*:

    previous_contents[file["path"]] = abs_path.read_text() if abs_path.exists() else None
    ...
    work_result.pop("previous_contents", None)  # "not durable evidence"

The pre-write content was captured and then thrown away. Nothing restored it. So when a
proposal failed verification — or raised, or lost its lease — the running deployment's own
source tree stayed modified by a proposal that was, by definition, never accepted. For the
Founder Alpha that is an isolation failure with a persistence side effect: the next restart
executes code an unaccepted proposal wrote.

These tests drive the real `run_task_execution_job()` end to end against real Postgres and a
real temporary checkout; only the LLM provider and `verify_task()`'s verdict are faked (the
verification machinery itself is covered by `test_mainai_execution_executor.py`, and faking the
verdict is what isolates the rollback property being tested here).

Long-term boundary: the safer execution boundary is the per-attempt `git worktree` this same
module already builds for `github_write_enabled` mode (`mainai_execution/worktree.py`) — the
shared checkout should eventually not be written to at all, even in proposal mode. That is a
larger change than a defect fix; until then, restoration is the property that must hold.
"""

import json
import uuid

import pytest
from sqlalchemy import text as sa_text

from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import executor, planner
from app.mainai_execution.execution_job import run_task_execution_job
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.verify import VerificationResult, VerificationStepResult
from app.models.mainai_execution import MainAITask, MainAITaskStatus
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_and_job_privilege_policy_before_this_module():
    """Same ordering requirement as test_mainai_execution_executor.py: dispatch_ready_task()
    creates a real mainai_jobs row, so both privilege sets must be in place before any test
    here runs."""
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls_user(db_session, user.id)
    return user.id


def _fake_chat(response_text: str):
    async def _chat(self, messages, model, **kwargs):
        return ChatResult(content=response_text, provider="openai", model=model, raw_usage={})

    return _chat


def _proposal(*files: tuple[str, str]) -> str:
    return json.dumps(
        {
            "files": [{"path": path, "content": content} for path, content in files],
            "commit_message": "proposal",
        }
    )


def _failed_verification(*_args, **_kwargs) -> VerificationResult:
    return VerificationResult(
        passed=False,
        steps=[VerificationStepResult(kind="targeted_tests", passed=False, detail={"error": "boom"})],
    )


def _passing_verification(*_args, **_kwargs) -> VerificationResult:
    return VerificationResult(passed=True, steps=[])


async def _run_one_repo_edit(db_session, superuser_db, owner_id, monkeypatch, repo_root, response_text, verification):
    from app.mainai_execution import execution_job

    monkeypatch.setattr(execution_job, "_repo_root", lambda: repo_root)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat(response_text))
    monkeypatch.setattr(execution_job, "verify_task", verification)

    goal = planner.create_goal(
        db_session,
        owner_id=owner_id,
        title="PI-1d proposal rollback",
        original_instruction="Propose an edit.",
        created_by="test",
        approval_policy="standard_repo_work",
    )
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="single repo_edit",
        tasks=[PlannedTaskSpec(description="propose an edit", task_type="repo_edit")],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()

    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, owner_id)
    await run_task_execution_job(
        db_session, job.id, owner_id, worker_id="worker-1", lease_generation=generation, lease_seconds=120
    )
    return superuser_db.get(MainAITask, task.id)


@pytest.mark.asyncio
async def test_a_failed_proposal_restores_the_shared_checkout_exactly(
    db_session, superuser_db, owner_id, monkeypatch, tmp_path
):
    """The regression this file exists for: the proposal writes an existing file and creates a
    new one (in a directory that did not exist), verification fails, and BOTH effects must be
    undone — verbatim content back, created file gone, created directory gone."""
    existing = tmp_path / "README.md"
    existing.write_text("ORIGINAL CONTENT\n", encoding="utf-8")
    created = tmp_path / "new_area" / "proposal.md"
    assert not created.exists()

    task = await _run_one_repo_edit(
        db_session,
        superuser_db,
        owner_id,
        monkeypatch,
        tmp_path,
        _proposal(("README.md", "PROPOSED CONTENT\n"), ("new_area/proposal.md", "BRAND NEW\n")),
        _failed_verification,
    )

    assert existing.read_text(encoding="utf-8") == "ORIGINAL CONTENT\n", "the pre-proposal content must be reinstated verbatim"
    assert not created.exists(), "a file the failed proposal created must not survive it"
    assert not (tmp_path / "new_area").exists(), "an empty directory created by the proposal must be removed too"
    assert task.status in (MainAITaskStatus.retryable_failed, MainAITaskStatus.failed)


@pytest.mark.asyncio
async def test_a_failed_proposal_also_rolls_back_when_verification_raises(
    db_session, superuser_db, owner_id, monkeypatch, tmp_path
):
    """The second hook: `verify_task()` raises (its documented `VerificationStepError` path)
    instead of returning a failed verdict. The write already happened, so the checkout must be
    restored on the exception path as well — not only on the explicit failure path."""
    existing = tmp_path / "notes.md"
    existing.write_text("keep me\n", encoding="utf-8")

    def _raising_verification(*_args, **_kwargs):
        from app.mainai_execution.verify import VerificationStepError

        raise VerificationStepError("targeted tests could not be run at all")

    task = await _run_one_repo_edit(
        db_session,
        superuser_db,
        owner_id,
        monkeypatch,
        tmp_path,
        _proposal(("notes.md", "overwritten\n")),
        _raising_verification,
    )

    assert existing.read_text(encoding="utf-8") == "keep me\n"
    assert task.status in (MainAITaskStatus.retryable_failed, MainAITaskStatus.failed)


@pytest.mark.asyncio
async def test_a_verified_proposal_keeps_its_edits(db_session, superuser_db, owner_id, monkeypatch, tmp_path):
    """The other direction, so the fix cannot pass by simply undoing everything: a proposal that
    verifies successfully must keep exactly what it wrote, including a newly created file."""
    existing = tmp_path / "README.md"
    existing.write_text("ORIGINAL CONTENT\n", encoding="utf-8")
    created = tmp_path / "new_area" / "proposal.md"

    task = await _run_one_repo_edit(
        db_session,
        superuser_db,
        owner_id,
        monkeypatch,
        tmp_path,
        _proposal(("README.md", "ACCEPTED CONTENT\n"), ("new_area/proposal.md", "BRAND NEW\n")),
        _passing_verification,
    )

    assert existing.read_text(encoding="utf-8") == "ACCEPTED CONTENT\n"
    assert created.read_text(encoding="utf-8") == "BRAND NEW\n"
    assert task.status == MainAITaskStatus.completed
