"""V0.2 recovery pipeline stages 1-3: detect (recovery_inspector.get_or_create_recovery_record),
inspect (recovery_inspector.inspect_recovery_record), classify
(recovery_classifier.classify_recovery_record). Real Postgres, real local git operations
against a real bare repo standing in for GitHub (same pattern as
test_mainai_execution_worktree.py) -- only GitHubClient.get_ref()/list_pull_requests_for_head()
and the remote URL are patched.

Covers each of the founder's A-I classification codes reachable from real evidence, plus:
  - idempotency of detect/inspect/classify (each stage runs its real work exactly once)
  - the fail-closed path: a worktree ownership mismatch blocks the record with
    manual_review_required=True, and classification never proceeds past a blocked record."""

import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text

from app.integrations.github_client import GitHubClient
from app.mainai_execution import executor, planner
from app.mainai_execution.checkpoint import record_checkpoint
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.recovery_classifier import classify_recovery_record
from app.mainai_execution.recovery_inspector import get_or_create_recovery_record, inspect_recovery_record
from app.mainai_execution.worktree import BASE_BRANCH, commit_worktree_changes, create_task_worktree, push_worktree_branch
from app.models.mainai_execution import MainAIGoal, MainAITask, MainAITaskEvent, MainAITaskEventType
from app.models.mainai_job import MainAIJob
from app.models.mainai_recovery import MainAIRecoveryEventType, MainAIRecoveryStatus, RecoveryClassification
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _apply_privilege_policy_before_this_module():
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


@pytest.fixture
def bare_remote(tmp_path) -> Path:
    remote_path = tmp_path / "bare-remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote_path)], check=True)
    seed_path = tmp_path / "seed-clone"
    subprocess.run(["git", "clone", "-q", str(remote_path), str(seed_path)], check=True)
    (seed_path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed_path), "config", "user.email", "seed@test.local"], check=True)
    subprocess.run(["git", "-C", str(seed_path), "config", "user.name", "Seed"], check=True)
    subprocess.run(["git", "-C", str(seed_path), "checkout", "-q", "-b", BASE_BRANCH], check=True)
    subprocess.run(["git", "-C", str(seed_path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(seed_path), "commit", "-q", "-m", "seed"], check=True)
    subprocess.run(["git", "-C", str(seed_path), "push", "-q", "origin", BASE_BRANCH], check=True)
    return remote_path


@pytest.fixture
def patched_github(monkeypatch, bare_remote):
    from app.config import get_settings
    from app.mainai_execution import worktree as worktree_module

    settings = get_settings()
    monkeypatch.setattr(settings, "github_write_enabled", True)
    monkeypatch.setattr(settings, "github_repo", "test-owner/test-repo")
    monkeypatch.setattr(settings, "github_token", "fake-token-not-used-over-network")
    monkeypatch.setattr(worktree_module, "_authed_remote_url", lambda repo, token: str(bare_remote))

    async def _fake_get_ref(self, branch: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(bare_remote), "rev-parse", f"refs/heads/{branch}"], capture_output=True, text=True
        )
        if result.returncode != 0:
            from app.integrations.github_client import GitHubClientError

            raise GitHubClientError(f"unknown ref {branch}")
        return result.stdout.strip()

    _prs: dict[str, list[dict]] = {}

    async def _fake_list_prs(self, *, head: str, base: str, state: str = "all") -> list[dict]:
        return _prs.get(head, [])

    monkeypatch.setattr(GitHubClient, "get_ref", _fake_get_ref)
    monkeypatch.setattr(GitHubClient, "is_configured", lambda self: True)
    monkeypatch.setattr(GitHubClient, "list_pull_requests_for_head", _fake_list_prs)
    return {"remote": bare_remote, "prs": _prs}


def _goal(db_session, owner_id):
    return planner.create_goal(
        db_session, owner_id=owner_id, title="Recovery test goal", original_instruction="Edit a file.", created_by="test"
    )


def _task_and_job(db_session, owner_id, *, task_type="repo_edit") -> tuple[MainAITask, MainAIJob]:
    goal = _goal(db_session, owner_id)
    planner.create_plan(
        db_session, goal=goal, rationale="single task", tasks=[PlannedTaskSpec(description="do it", task_type=task_type)], created_by="test"
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    db_session.refresh(task)
    return task, job


def _goal_for(db_session, task) -> MainAIGoal:
    return db_session.query(MainAIGoal).filter(MainAIGoal.id == task.goal_id).one()


async def _detect_and_inspect(db_session, task, job):
    record = get_or_create_recovery_record(db_session, task=task, job=job)
    db_session.commit()
    record = await inspect_recovery_record(db_session, task=task, job=job, record=record)
    db_session.commit()
    return record


def _mark_verification_passed(db_session, task):
    event = MainAITaskEvent(task_id=task.id, owner_id=task.owner_id, event_type=MainAITaskEventType.verification_passed, detail={})
    db_session.add(event)
    db_session.commit()


# ---------------------------------------------------------------- detect


def test_get_or_create_recovery_record_is_idempotent_and_records_dead_detected(db_session, owner_id):
    task, job = _task_and_job(db_session, owner_id)

    first = get_or_create_recovery_record(db_session, task=task, job=job)
    db_session.commit()
    second = get_or_create_recovery_record(db_session, task=task, job=job)

    assert first.id == second.id
    assert first.status == MainAIRecoveryStatus.detected
    events = db_session.execute(
        sa_text("SELECT event_type FROM mainai_recovery_events WHERE recovery_record_id = :id"), {"id": str(first.id)}
    ).all()
    assert [row[0] for row in events] == [MainAIRecoveryEventType.dead_detected.value]


# ---------------------------------------------------------------- A. NOTHING_DONE


@pytest.mark.asyncio
async def test_classify_nothing_done_when_no_evidence_at_all(db_session, owner_id, patched_github):
    task, job = _task_and_job(db_session, owner_id, task_type="read_only_audit")
    record = await _detect_and_inspect(db_session, task, job)
    assert record.status == MainAIRecoveryStatus.inspected

    record = classify_recovery_record(db_session, record=record)
    db_session.commit()
    assert record.classification == RecoveryClassification.nothing_done
    assert record.status == MainAIRecoveryStatus.classified


# ---------------------------------------------------------------- B. CHECKPOINTED_WORK


@pytest.mark.asyncio
async def test_classify_checkpointed_work_when_work_result_recorded_but_nothing_else(db_session, owner_id, patched_github):
    task, job = _task_and_job(db_session, owner_id, task_type="read_only_audit")
    goal = _goal_for(db_session, task)
    record_checkpoint(db_session, task=task, goal=goal, job_id=job.id, step="work_result", data={"work_result": {"ok": True}})
    db_session.commit()

    record = await _detect_and_inspect(db_session, task, job)
    record = classify_recovery_record(db_session, record=record)
    db_session.commit()
    assert record.classification == RecoveryClassification.checkpointed_work


# ---------------------------------------------------------------- C. LOCAL_UNCOMMITTED_WORK


@pytest.mark.asyncio
async def test_classify_local_uncommitted_work(db_session, owner_id, patched_github):
    task, job = _task_and_job(db_session, owner_id)
    wt = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    db_session.commit()
    (Path(wt.path) / "draft.txt").write_text("uncommitted\n", encoding="utf-8")

    record = await _detect_and_inspect(db_session, task, job)
    record = classify_recovery_record(db_session, record=record)
    db_session.commit()
    assert record.classification == RecoveryClassification.local_uncommitted_work


# ---------------------------------------------------------------- D. LOCAL_COMMITTED_NOT_PUSHED


@pytest.mark.asyncio
async def test_classify_local_committed_not_pushed(db_session, owner_id, patched_github):
    task, job = _task_and_job(db_session, owner_id)
    wt = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    db_session.commit()
    (Path(wt.path) / "committed.txt").write_text("local commit\n", encoding="utf-8")
    commit_worktree_changes(db_session, wt, message="committed.txt")
    db_session.commit()

    record = await _detect_and_inspect(db_session, task, job)
    record = classify_recovery_record(db_session, record=record)
    db_session.commit()
    assert record.classification == RecoveryClassification.local_committed_not_pushed


# ---------------------------------------------------------------- E. PUSHED_NO_PR


@pytest.mark.asyncio
async def test_classify_pushed_no_pr(db_session, owner_id, patched_github):
    task, job = _task_and_job(db_session, owner_id)
    wt = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    db_session.commit()
    (Path(wt.path) / "pushed.txt").write_text("pushed content\n", encoding="utf-8")
    commit_worktree_changes(db_session, wt, message="pushed.txt")
    db_session.commit()
    await push_worktree_branch(wt)

    record = await _detect_and_inspect(db_session, task, job)
    record = classify_recovery_record(db_session, record=record)
    db_session.commit()
    assert record.classification == RecoveryClassification.pushed_no_pr


# --------- E2. PUSHED_NO_PR with NO worktree at all (the real production repo_edit path)


@pytest.mark.asyncio
async def test_classify_pushed_no_pr_is_reachable_with_no_worktree_at_all(db_session, owner_id, patched_github):
    """Hardening-pass regression (P0): execution_job.py's actual production `repo_edit` handler
    (`_handle_repo_edit()`/`_finalize_repo_edit()`) never calls `create_task_worktree()` -- it
    still writes to the shared checkout and pushes via the GitHub Git Data API (see
    docs/MAINAI_DEAD_AGENT_RECOVERY_V0_2.md's LIMITED section). Before this fix, `_classify()`
    could ONLY reach PUSHED_NO_PR/PR_EXISTS via `worktree_local_head_sha == remote_branch_sha`
    -- a signal that is always None for every real repo_edit job, since none of them ever
    create a worktree. This is the real-production shape: a branch pushed directly to the
    remote (no worktree, no local git commit object anywhere) plus the real `finalized`
    checkpoint execution_job.py itself records right after a confirmed push -- exactly what a
    dead repo_edit job that already pushed looks like in practice."""
    task, job = _task_and_job(db_session, owner_id)
    goal = _goal_for(db_session, task)
    branch = f"claude/mainai-task-{task.id}"

    # Simulates the real Git Data API push _finalize_repo_edit() performs -- a commit lands on
    # the task's branch on the remote, with NO local worktree/clone involved anywhere.
    other_clone = Path(patched_github["remote"]).parent / "pushed-via-api-clone"
    subprocess.run(["git", "clone", "-q", str(patched_github["remote"]), str(other_clone)], check=True)
    subprocess.run(["git", "-C", str(other_clone), "checkout", "-q", "-b", branch, f"origin/{BASE_BRANCH}"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "config", "user.email", "api@test.local"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "config", "user.name", "API"], check=True)
    (other_clone / "pushed.txt").write_text("real content pushed via the Git Data API\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(other_clone), "add", "pushed.txt"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "commit", "-q", "-m", "pushed.txt"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "push", "-q", "origin", branch], check=True)

    record_checkpoint(db_session, task=task, goal=goal, job_id=job.id, step="work_result", data={"work_result": {"files": ["pushed.txt"]}})
    record_checkpoint(db_session, task=task, goal=goal, job_id=job.id, step="finalized", data={"finalize_info": {"branch": branch, "proposed": False}})
    db_session.commit()

    record = await _detect_and_inspect(db_session, task, job)
    assert record.evidence["worktree_exists"] is False
    assert record.evidence.get("worktree_local_head_sha") is None  # no worktree -- exactly what real production evidence looks like
    assert record.evidence["remote_branch_exists"] is True
    assert record.evidence["has_finalized_checkpoint"] is True

    record = classify_recovery_record(db_session, record=record)
    db_session.commit()
    assert record.classification == RecoveryClassification.pushed_no_pr
    assert record.manual_review_required is False  # auto-salvageable, but see recovery_approval.py -- still gated behind founder approval


@pytest.mark.asyncio
async def test_classify_pr_exists_is_reachable_with_no_worktree_at_all(db_session, owner_id, patched_github):
    """Same real-production shape as the PUSHED_NO_PR regression above, but a PR already exists
    for the branch too -- must classify PR_EXISTS, not PUSHED_NO_PR or CHECKPOINTED_WORK."""
    task, job = _task_and_job(db_session, owner_id)
    goal = _goal_for(db_session, task)
    branch = f"claude/mainai-task-{task.id}"

    other_clone = Path(patched_github["remote"]).parent / "pushed-via-api-clone-2"
    subprocess.run(["git", "clone", "-q", str(patched_github["remote"]), str(other_clone)], check=True)
    subprocess.run(["git", "-C", str(other_clone), "checkout", "-q", "-b", branch, f"origin/{BASE_BRANCH}"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "config", "user.email", "api@test.local"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "config", "user.name", "API"], check=True)
    (other_clone / "pr.txt").write_text("real content behind a real PR, pushed via the Git Data API\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(other_clone), "add", "pr.txt"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "commit", "-q", "-m", "pr.txt"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "push", "-q", "origin", branch], check=True)
    patched_github["prs"][f"test-owner:{branch}"] = [{"number": 99, "html_url": "https://example.invalid/pr/99"}]

    record_checkpoint(db_session, task=task, goal=goal, job_id=job.id, step="work_result", data={"work_result": {"files": ["pr.txt"]}})
    record_checkpoint(db_session, task=task, goal=goal, job_id=job.id, step="finalized", data={"finalize_info": {"branch": branch, "proposed": False}})
    db_session.commit()

    record = await _detect_and_inspect(db_session, task, job)
    record = classify_recovery_record(db_session, record=record)
    db_session.commit()
    assert record.classification == RecoveryClassification.pr_exists


# ---------------------------------------------------------------- F. PR_EXISTS


@pytest.mark.asyncio
async def test_classify_pr_exists(db_session, owner_id, patched_github):
    task, job = _task_and_job(db_session, owner_id)
    wt = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    db_session.commit()
    (Path(wt.path) / "pr.txt").write_text("pr content\n", encoding="utf-8")
    commit_worktree_changes(db_session, wt, message="pr.txt")
    db_session.commit()
    await push_worktree_branch(wt)

    patched_github["prs"][f"test-owner:{wt.branch}"] = [{"number": 42, "html_url": "https://example.invalid/pr/42"}]

    record = await _detect_and_inspect(db_session, task, job)
    record = classify_recovery_record(db_session, record=record)
    db_session.commit()
    assert record.classification == RecoveryClassification.pr_exists
    assert record.evidence["pr_numbers"] == [42]


# ---------------------------------------------------------------- G. VERIFIED_WORK


@pytest.mark.asyncio
async def test_classify_verified_work_for_a_non_git_task_type(db_session, owner_id, patched_github):
    task, job = _task_and_job(db_session, owner_id, task_type="read_only_audit")
    _mark_verification_passed(db_session, task)

    record = await _detect_and_inspect(db_session, task, job)
    record = classify_recovery_record(db_session, record=record)
    db_session.commit()
    assert record.classification == RecoveryClassification.verified_work


# ---------------------------------------------------------------- H. CONFLICTED_STATE


@pytest.mark.asyncio
async def test_classify_conflicted_state_on_genuine_divergence(db_session, owner_id, patched_github, tmp_path):
    task, job = _task_and_job(db_session, owner_id)
    wt = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    db_session.commit()
    (Path(wt.path) / "mine.txt").write_text("mine\n", encoding="utf-8")
    commit_worktree_changes(db_session, wt, message="mine.txt")
    db_session.commit()

    other_clone = tmp_path / "other-clone"
    remote = patched_github["remote"]
    subprocess.run(["git", "clone", "-q", str(remote), str(other_clone)], check=True)
    subprocess.run(["git", "-C", str(other_clone), "checkout", "-q", "-b", wt.branch, wt.base_sha], check=True)
    subprocess.run(["git", "-C", str(other_clone), "config", "user.email", "other@test.local"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "config", "user.name", "Other"], check=True)
    (other_clone / "theirs.txt").write_text("theirs\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(other_clone), "add", "theirs.txt"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "commit", "-q", "-m", "theirs"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "push", "-q", "origin", wt.branch], check=True)

    record = await _detect_and_inspect(db_session, task, job)
    record = classify_recovery_record(db_session, record=record)
    db_session.commit()
    assert record.classification == RecoveryClassification.conflicted_state
    assert record.manual_review_required is True


# ---------------------------------------------------------------- I. fail-closed / UNSAFE


@pytest.mark.asyncio
async def test_inspect_blocks_with_manual_review_when_two_worktree_rows_claim_the_same_job(db_session, superuser_db, owner_id, patched_github):
    task, job = _task_and_job(db_session, owner_id)
    await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    db_session.commit()

    # Simulate a second, conflicting worktree row for the same job (should never happen via
    # the real code path -- create_task_worktree() is idempotent per job_id -- but the
    # inspector must still fail closed if it ever does, e.g. from a bug or manual DB edit).
    # Uses superuser_db (table owner) for the schema tweak + insert since db_session runs as
    # the restricted mainai_app runtime role, which cannot ALTER TABLE.
    superuser_db.execute(sa_text("SET app.current_user_id = :uid"), {"uid": str(owner_id)})
    superuser_db.execute(sa_text("ALTER TABLE mainai_task_worktrees DROP CONSTRAINT mainai_task_worktrees_job_id_key"))
    superuser_db.execute(
        sa_text(
            """
            INSERT INTO mainai_task_worktrees
                (id, task_id, owner_id, job_id, lease_generation, executor_id, repo, base_sha, branch, path, marker_token, created_at)
            VALUES
                (gen_random_uuid(), :task_id, :owner_id, :job_id, 1, 'worker-2', 'test-owner/test-repo', :base_sha,
                 'claude/mainai-task-dup', '/tmp/does-not-exist', 'x', now())
            """
        ),
        {"task_id": str(task.id), "owner_id": str(owner_id), "job_id": str(job.id), "base_sha": "0" * 40},
    )
    superuser_db.commit()

    record = await _detect_and_inspect(db_session, task, job)
    assert record.status == MainAIRecoveryStatus.blocked
    assert record.manual_review_required is True
    assert "multiple_worktree_rows_for_job" in record.blocker

    # classify must never advance a blocked record past inspection.
    unchanged = classify_recovery_record(db_session, record=record)
    assert unchanged.classification is None
    assert unchanged.status == MainAIRecoveryStatus.blocked

    # The DROP CONSTRAINT above is real DDL against the session-scoped test database, not
    # data the autouse `_clean_tables` truncation between tests will ever undo -- restore it
    # exactly as migration 0033 defines it so the schema this test leaves behind for every
    # later test (including tests/backend/test_migration_roundtrip.py's whole-schema
    # snapshot comparison, which runs later in the same session and would otherwise see a
    # permanently mutated "before" schema that no longer matches what the migration chain
    # itself produces) is the one the migration chain actually specifies. The duplicate row
    # this test inserted above must go first -- it would otherwise violate the very
    # uniqueness the constraint re-adds.
    superuser_db.execute(sa_text("DELETE FROM mainai_task_worktrees WHERE job_id = :job_id"), {"job_id": str(job.id)})
    superuser_db.execute(
        sa_text(
            "ALTER TABLE mainai_task_worktrees "
            "ADD CONSTRAINT mainai_task_worktrees_job_id_key UNIQUE (job_id)"
        )
    )
    superuser_db.commit()


# ---------------------------------------------------------------- idempotency


@pytest.mark.asyncio
async def test_inspect_and_classify_are_idempotent(db_session, owner_id, patched_github):
    task, job = _task_and_job(db_session, owner_id, task_type="read_only_audit")
    record = await _detect_and_inspect(db_session, task, job)
    evidence_after_first_inspect = dict(record.evidence)

    again = await inspect_recovery_record(db_session, task=task, job=job, record=record)
    assert again.evidence == evidence_after_first_inspect
    assert again.status == MainAIRecoveryStatus.inspected

    classified_once = classify_recovery_record(db_session, record=again)
    db_session.commit()
    classified_twice = classify_recovery_record(db_session, record=classified_once)
    assert classified_twice.classification == classified_once.classification

    events = db_session.execute(
        sa_text("SELECT event_type FROM mainai_recovery_events WHERE recovery_record_id = :id"), {"id": str(record.id)}
    ).all()
    event_types = [row[0] for row in events]
    assert event_types.count(MainAIRecoveryEventType.recovery_classified.value) == 1
    assert event_types.count(MainAIRecoveryEventType.recovery_inspected.value) == 1


# ---------------------------------------------------------------- engineering lessons reuse


@pytest.mark.asyncio
async def test_inspect_surfaces_applicable_engineering_lessons_for_the_task_type(db_session, owner_id, patched_github):
    """V0.2 engineering-lessons reuse: a lesson already recorded (from any source -- a prior
    incident, a hardening pass, a prior recovery) for this task's task_type must show up in
    the recovery record's own durable evidence, so a human reviewing it has the same context a
    founder would otherwise have to separately query for."""
    from app.mainai_execution import lessons
    from app.models.mainai_execution import EngineeringLessonConfidence, EngineeringLessonSeverity

    task, job = _task_and_job(db_session, owner_id, task_type="repo_edit")

    lesson = lessons.record_lesson(
        db_session,
        problem="repo_edit tasks sometimes push mid-file-write and leave a half-applied change",
        root_cause="worker crash between local commit and push",
        affected_component="execution_job.py",
        severity=EngineeringLessonSeverity.high,
        evidence="observed in a prior dead-agent recovery",
        fix="verify worktree ownership before any future push",
        general_rule="always verify worktree ownership before trusting a local commit as complete",
        applies_to=["repo_edit"],
        source_type="test",
        source_ref="unit-test",
        created_by="test",
        first_seen_at=datetime.utcnow(),
        confidence=EngineeringLessonConfidence.likely,
    )
    db_session.commit()

    record = await _detect_and_inspect(db_session, task, job)

    applicable = record.evidence["applicable_lessons"]
    assert len(applicable) == 1
    assert applicable[0]["lesson_id"] == str(lesson.id)
    assert applicable[0]["severity"] == "high"
    assert applicable[0]["general_rule"] == "always verify worktree ownership before trusting a local commit as complete"


@pytest.mark.asyncio
async def test_inspect_applicable_lessons_is_empty_when_none_apply(db_session, owner_id, patched_github):
    task, job = _task_and_job(db_session, owner_id, task_type="read_only_audit")
    record = await _detect_and_inspect(db_session, task, job)
    assert record.evidence["applicable_lessons"] == []


# ---------------------------------------------------------------- privilege policy (hardening pass)


def test_apply_mainai_execution_privileges_narrows_mainai_recovery_events_even_from_a_blanket_grant(superuser_db):
    """Hardening-pass finding (P1): migration 0033's own module docstring already claimed
    mainai_app's grants on the three new V0.2 tables are "applied and verified by app/rls.py's
    apply_mainai_execution_privileges(), extended to cover these three tables" -- but that
    extension never actually happened; app/rls.py was untouched by the original V0.2 branch.
    The append-only deny-mutation trigger on mainai_recovery_events still functionally blocked
    every UPDATE/DELETE regardless (see the trigger tests below), but relying on that single
    layer alone -- without also revoking the raw grant -- is exactly the class of gap this
    project's own S1A privilege-policy series (and Pass 44's TRUNCATE finding) established the
    "narrow every write path to exactly what it needs, never one layer alone" doctrine to close.
    Same "survives a worst-case blanket GRANT ALL from ensure_app_role.py's own boot-time
    default" proof test_apply_mainai_job_runtime_privileges_survives_reboots_blanket_grant_all
    already established for mainai_job_events."""
    from app.db import migration_engine
    from app.rls import _MAINAI_RECOVERY_EVENT_TABLE_ALLOWED_PRIVILEGES, _effective_table_privileges, apply_mainai_execution_privileges

    superuser_db.execute(sa_text("GRANT ALL PRIVILEGES ON TABLE mainai_recovery_events TO mainai_app"))
    superuser_db.commit()

    apply_mainai_execution_privileges(migration_engine, require_complete=True)  # must not raise

    conn = superuser_db.connection()
    assert _effective_table_privileges(conn, "mainai_app", "mainai_recovery_events") == _MAINAI_RECOVERY_EVENT_TABLE_ALLOWED_PRIVILEGES
    assert _MAINAI_RECOVERY_EVENT_TABLE_ALLOWED_PRIVILEGES == frozenset({"SELECT", "INSERT"})


def test_mainai_recovery_events_mainai_app_lacks_update_delete_privilege_at_the_grant_level(db_session, superuser_db, owner_id, patched_github):
    """Distinguishes "the grant itself is gone" from "merely trigger-blocked" -- the same
    distinction test_mainai_job_events_mainai_app_lacks_update_delete_privilege draws for its
    own sibling table. Checks has_table_privilege() directly (the real, effective-privilege
    check Postgres itself uses), not just that an UPDATE/DELETE attempt raises."""
    task, job = _task_and_job(db_session, owner_id, task_type="read_only_audit")
    record = get_or_create_recovery_record(db_session, task=task, job=job)
    db_session.commit()
    event_id = superuser_db.execute(
        sa_text("SELECT id FROM mainai_recovery_events WHERE recovery_record_id = :id LIMIT 1"), {"id": str(record.id)}
    ).scalar()
    assert event_id is not None

    for priv in ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        has_it = superuser_db.execute(
            sa_text("SELECT has_table_privilege('mainai_app', 'public.mainai_recovery_events', :priv)"), {"priv": priv}
        ).scalar()
        assert has_it is False, f"mainai_app must not hold {priv} on mainai_recovery_events"
