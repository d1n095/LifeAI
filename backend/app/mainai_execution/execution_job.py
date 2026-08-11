"""Entry point app/worker.py's poll loop calls once a `task_execution` mainai_jobs row has
been claimed — the actual EXECUTOR of the MainAI Execution Loop V0.1. Mirrors
app/jobs/handlers/corpus_review.py's shape exactly: same signature, same lease-fencing
discipline (every write goes through app/jobs/service.py's guarded functions, a
JobLeaseLostError stops this function immediately with no further writes), same restart-safe
"progress is only ever durable together with the work it describes" rule.

Per-task_type handlers (`_HANDLERS`) do the REAL work:
  - read_only_audit: a real chat_with_fallback() analysis call (same provider chain
    app/agent_orchestration.py's dispatch_task() already uses) — no repo writes.
  - repo_edit: a real chat_with_fallback() code-agent call asking for full-file replacement
    content for a small, explicitly scoped set of files (never a diff to parse/apply — see
    app/integrations/github_client.py's commit_multiple_files() docstring for why), applied
    to the LOCAL checkout first (this backend container's own repo tree — see _repo_root())
    so verify_task() (app/mainai_execution/verify.py) can actually run tests against the new
    content before anything is pushed anywhere.
  - run_tests: runs the exact same local-test mechanism verify.py's targeted_tests step uses,
    as the task's own primary work (for a task whose whole point IS "prove the suite is
    green", not a side effect of verifying some other change).
  - open_pr: opens the real PR for a goal's already-verified repo_edit (looks up the branch
    the repo_edit task recorded on its own `completed` event — see _find_repo_edit_branch()).

GitHub writes (branch creation, the real multi-file commit, PR creation) only ever happen
when `settings.github_write_enabled` is True (default False) — same gate
app/agent_orchestration.py already uses; with it off, repo_edit/open_pr still do everything
up to and including local verification, and record a PROPOSAL (computed branch name, file
contents, commit message / PR title+body) as evidence, never touching the network. This is
what "close the stub gap" means in V0.1: the capability is real and independently testable
(tests/backend/test_github_client.py, tests/backend/test_mainai_execution_executor.py), not
that it defaults to pushing to a live repo."""

import logging
import subprocess
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations.github_client import GitHubClientError, get_github_client
from app.jobs.mainai_job_lease import JobLeaseLostError, renew_mainai_job_lease
from app.jobs.service import mark_completed, mark_failed, update_progress
from app.mainai_execution.checkpoint import latest_checkpoint_for_step, record_checkpoint
from app.mainai_execution.executor import task_for_job
from app.mainai_execution.graph import recompute_task_readiness
from app.mainai_execution.verify import (
    VerificationResult,
    VerificationStepError,
    VerificationStepResult,
    decode_subprocess_output,
    validate_targeted_tests_target,
    verify_task,
)
from app.models.mainai_execution import (
    MainAIGoal,
    MainAITask,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)
from app.models.mainai_job import MainAIJob, MainAIJobErrorCategory
from app.providers.base import Message
from app.providers.registry import chat_with_fallback

logger = logging.getLogger("mainai.execution")


def _repo_root() -> Path:
    """This file lives at <repo>/backend/app/mainai_execution/execution_job.py -- three
    parents up is the repo root, the same directory GitHub paths in a task's
    verification_plan/commit are relative to."""
    return Path(__file__).resolve().parents[3]


def _backend_root() -> Path:
    return _repo_root() / "backend"


class TaskExecutionError(Exception):
    """Raised by a task_type handler for a genuine execution failure (provider error,
    malformed AI response, filesystem error) -- distinct from a verification FAILURE (which is
    not an exception; see verify_task()'s return value), and always caught by
    run_task_execution_job() and turned into a truthful mark_failed()/retryable_failed
    outcome, never left to crash the worker."""


READ_ONLY_AUDIT_SYSTEM_PROMPT = (
    "Du är MainAI:s read-only audit-agent. Du får en beskrivning av vad som ska granskas. "
    "Svara med en kort, konkret sammanfattning av vad du hittar (max 500 ord) -- inga "
    "kodändringar, inga förslag på commits, bara analys."
)

CODE_AGENT_SYSTEM_PROMPT = (
    "Du är MainAI:s kodagent. Du får ett litet, avgränsat uppdrag. Svara ENDAST med ett "
    "JSON-objekt på formen {\"files\": [{\"path\": \"relative/path/from/repo/root.py\", "
    "\"content\": \"...hela filens NYA fullständiga innehåll...\"}], \"commit_message\": "
    "\"...\"}. Ändra ALDRIG fler filer än vad uppdraget kräver. `content` är filens KOMPLETTA "
    "nya innehåll, aldrig en diff eller ett utdrag."
)


async def _handle_read_only_audit(db: Session, task: MainAITask) -> dict:
    messages = [Message(role="system", content=READ_ONLY_AUDIT_SYSTEM_PROMPT), Message(role="user", content=task.description)]
    result, _attempted = await chat_with_fallback(db, messages)
    return {"summary": result.content, "provider": result.provider, "model": result.model}


def _validate_repo_edit_file_path(path_value: object) -> str:
    """Hardening pass finding (P0): `path_value` is AI-proposed, untrusted content (the code
    agent's own response, itself derived from a possibly hallucinated or prompt-injected
    task description) that becomes the right-hand operand of `repo_root / path_value` in
    `_handle_repo_edit()` below. The previous check here only rejected `..` segments --
    verify.py's validate_targeted_tests_target() (which this call site's own comment used to
    claim mirrored) additionally rejects ABSOLUTE paths, and that check was missing here. This
    was not a lesser gap: pathlib's `/` operator silently DISCARDS the left operand entirely
    when the right operand is absolute (`Path("/repo/root") / "/etc/cron.d/x" ==
    Path("/etc/cron.d/x")`, not an error) -- so an absolute AI-proposed path was a genuine
    arbitrary-file-WRITE primitive onto the executor host's filesystem, wherever the mainai
    worker process has write access, with no `..` involved at all."""
    if not isinstance(path_value, str) or not path_value:
        raise TaskExecutionError(f"Code agent file entry 'path' must be a non-empty string, got {path_value!r}.")
    path = PurePosixPath(path_value)
    if path.is_absolute() or ".." in path.parts:
        raise TaskExecutionError(f"Refusing an absolute or '..'-escaping file path from the code agent: {path_value!r}")
    return path_value


def _parse_code_agent_response(raw: str) -> dict:
    import json

    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[len("json") :]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TaskExecutionError(f"Code agent response was not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("files"), list) or not parsed["files"]:
        raise TaskExecutionError("Code agent response must be an object with a non-empty 'files' array.")
    for f in parsed["files"]:
        if not isinstance(f, dict) or "path" not in f or "content" not in f:
            raise TaskExecutionError(f"Malformed file entry in code agent response: {f!r}")
        _validate_repo_edit_file_path(f["path"])
    return parsed


async def _handle_repo_edit(db: Session, task: MainAITask) -> dict:
    messages = [Message(role="system", content=CODE_AGENT_SYSTEM_PROMPT), Message(role="user", content=task.description)]
    result, _attempted = await chat_with_fallback(db, messages)
    parsed = _parse_code_agent_response(result.content)

    repo_root = _repo_root().resolve()
    previous_contents: dict[str, str | None] = {}
    for file in parsed["files"]:
        # Defense in depth beyond _validate_repo_edit_file_path()'s absolute/'..' rejection --
        # resolves symlinks too, so a pre-existing symlink inside the repo tree pointing outside
        # it (a separate, lower-likelihood attack surface -- would require a malicious symlink
        # already merged into the repo, not just a malicious AI response) still can't be used to
        # escape repo_root via a syntactically clean relative path.
        abs_path = (repo_root / file["path"]).resolve()
        if abs_path != repo_root and repo_root not in abs_path.parents:
            raise TaskExecutionError(f"Refusing to write outside the repo root: {file['path']!r} resolved to {abs_path}.")
        previous_contents[file["path"]] = abs_path.read_text() if abs_path.exists() else None
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(file["content"])

    return {
        "files": parsed["files"],
        "commit_message": parsed.get("commit_message", task.description[:200]),
        "previous_contents": previous_contents,
        "provider": result.provider,
        "model": result.model,
    }


def _run_pytest(target: str, *, cwd: Path, timeout_seconds: int = 300) -> dict:
    """Hardening pass finding (P1): `target` is AI-proposed, untrusted content
    (app/mainai_execution/planner.py's propose_plan_via_ai()) that becomes a real subprocess
    argv element -- validated with the SAME check verify.py's own targeted_tests step uses
    (validate_targeted_tests_target()), so a `..`-escaping or absolute target can never reach
    this subprocess call, matching the discipline _parse_code_agent_response() already applies
    to AI-proposed file WRITE paths in _handle_repo_edit() above.

    Hardening pass finding (P1, sibling of verify.py's identical fix): subprocess.run(...,
    timeout=...) raises TimeoutExpired, not returns a returncode -- left uncaught, this
    propagated past _handle_run_tests()'s own `except VerificationStepError` (wrong exception
    type) and run_task_execution_job()'s `except (TaskExecutionError, VerificationStepError,
    GitHubClientError)` (also wrong type), landing at app/worker.py's generic handler, which
    can only mark the JOB failed -- the TASK itself was left stuck at `running` forever, with
    no retry or cancel path (see verify.py's own fix for the full consequence chain). Treated
    as an ordinary failed result here too, for the same reason."""
    validate_targeted_tests_target(target)
    try:
        result = subprocess.run(["python", "-m", "pytest", "-q", target], cwd=str(cwd), capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        return {
            "target": target,
            "returncode": None,
            "passed": False,
            "error": "timeout",
            "timeout_seconds": timeout_seconds,
            "stdout_tail": decode_subprocess_output(exc.stdout)[-4000:],
            "stderr_tail": decode_subprocess_output(exc.stderr)[-2000:],
        }
    return {"target": target, "returncode": result.returncode, "passed": result.returncode == 0, "stdout_tail": result.stdout[-4000:], "stderr_tail": result.stderr[-2000:]}


def _handle_run_tests(task: MainAITask) -> dict:
    targets = [step["target"] for step in task.verification_plan if step.get("kind") == "targeted_tests" and step.get("target")]
    if not targets:
        raise TaskExecutionError("run_tests task has no 'targeted_tests' entries in its verification_plan to run.")
    try:
        results = [_run_pytest(target, cwd=_backend_root()) for target in targets]
    except VerificationStepError as exc:
        raise TaskExecutionError(str(exc)) from exc
    return {"results": results}


def _find_repo_edit_branch(db: Session, *, goal_id: uuid.UUID) -> dict | None:
    """The branch/commit a sibling repo_edit task in this goal already produced -- open_pr's
    handler looks this up rather than the planner needing to pass it explicitly, since it
    doesn't exist until the repo_edit task actually completes. A repo_edit's own `completed`
    event detail is `{**verification.evidence(), "work_result": {...branch, base_branch,
    files, ...}}` (see _finalize_task_outcome()) -- "branch" lives inside `work_result`, never
    at the top level of the event detail itself."""
    events = db.execute(
        select(MainAITaskEvent)
        .join(MainAITask, MainAITask.id == MainAITaskEvent.task_id)
        .where(MainAITask.goal_id == goal_id, MainAITaskEvent.event_type == MainAITaskEventType.completed)
        .order_by(MainAITaskEvent.created_at.desc())
    ).scalars().all()
    for event in events:
        work_result = event.detail.get("work_result", {})
        if "branch" in work_result:
            return work_result
    return None


async def _handle_open_pr(db: Session, task: MainAITask, goal: MainAIGoal) -> dict:
    """Crash-matrix finding (P1, hardening pass): open_pr's real PR creation happens inside
    the SAME "work_result" computation run_task_execution_job() checkpoints -- a crash between
    a successful create_pull_request() call and that checkpoint's commit would, on resume,
    call this function again from scratch (no separate "finalized" checkpoint exists for
    open_pr the way repo_edit's own push does). create_pull_request() is NOT naturally
    idempotent -- GitHub allows only one open PR per head/base pair and rejects a second with a
    422, which would land the task on retryable_failed/failed even though the first PR had
    already durably succeeded. Fixed the same way _finalize_repo_edit() was: check whether a
    PR for this exact head/base already exists FIRST and reuse it instead of creating a
    second one."""
    settings = get_settings()
    edit_info = _find_repo_edit_branch(db, goal_id=goal.id)
    if edit_info is None:
        raise TaskExecutionError("open_pr task found no completed repo_edit sibling with a recorded branch to open a PR for.")

    title = f"MainAI: {goal.title}"[:250]
    body = f"Automatiskt genererad av MainAI Execution Loop V0.1 för mål: {goal.title}\n\n{goal.original_instruction}"
    head_branch = edit_info["branch"]
    base_branch = edit_info.get("base_branch", "main")

    if not settings.github_write_enabled:
        return {"proposed": True, "title": title, "body": body, "head": head_branch, "base": base_branch}

    client = get_github_client()
    repo_owner = settings.github_repo.split("/", 1)[0]
    existing = await client.list_pull_requests_for_head(head=f"{repo_owner}:{head_branch}", base=base_branch, state="open")
    if existing:
        pr = existing[0]
    else:
        pr = await client.create_pull_request(title=title, body=body, head=head_branch, base=base_branch)
    return {"proposed": False, "pull_request_number": pr.get("number"), "pull_request_url": pr.get("html_url")}


async def _finalize_repo_edit(db: Session, task: MainAITask, work_result: dict) -> dict:
    """Only called once verify_task() has already passed (see run_task_execution_job()) --
    the actual GitHub push, gated behind github_write_enabled exactly like
    app/agent_orchestration.py's own prepare_github_pr().

    Crash-matrix finding (P1, hardening pass): run_task_execution_job() checkpoints this
    function's result under step="finalized" specifically so a crash AFTER a real GitHub push
    succeeds but BEFORE that checkpoint commits doesn't push a second time on resume -- but the
    original version of this function still had a bug for exactly that crash window. On resume,
    with no "finalized" checkpoint found, this function ran again from scratch: it re-read
    `base_sha` from `base_branch` (unchanged by the first push, which only ever touches the
    TASK's own branch), found the task branch already existed (from the successful first push)
    and swallowed that as "a retry, fine, commit on top" -- but then built the new commit on
    top of the STALE `base_sha` instead of the branch's actual current tip. GitHub's real
    fast-forward-only update_ref() correctly rejected that (the new commit's parent wasn't the
    branch's current HEAD) and raised GitHubClientError -- which run_task_execution_job()'s own
    outer handler catches and turns into `retryable_failed`/`failed`, even though the FIRST
    attempt's push had already durably succeeded. A task could end up permanently `failed` in
    the final report while the real code change sat live on its branch the whole time.

    Fixed by checking whether `branch` ALREADY exists first (`get_ref(branch)` -- a real 404
    raises GitHubClientError exactly like a missing ref should) and, if so, building the new
    commit on top of ITS current tip instead of blindly recomputing from `base_branch`. A
    genuinely first attempt (branch doesn't exist yet) still creates it from `base_branch` as
    before. This does not make the whole operation perfectly idempotent (a resume after a
    successful-but-uncheckpointed push still creates one extra, redundant commit with the same
    file contents on top of the first) -- but it always succeeds and the task's final state is
    truthful, which is what actually matters here; a byte-for-byte duplicate-content detection
    to avoid even that redundant commit is out of scope for V0.1."""
    settings = get_settings()
    branch = f"claude/mainai-task-{task.id}"
    base_branch = "claude/det-kommer-mer-879lcm"

    if not settings.github_write_enabled:
        return {"branch": branch, "base_branch": base_branch, "proposed": True, "files": [f["path"] for f in work_result["files"]]}

    client = get_github_client()
    try:
        base_sha = await client.get_ref(branch)  # already exists -- a resume after a prior push, build on its real current tip
    except GitHubClientError:
        base_sha = await client.get_ref(base_branch)
        await client.create_branch(new_branch=branch, from_sha=base_sha)
    commit_result = await client.commit_multiple_files(
        branch=branch, base_sha=base_sha, files=[{"path": f["path"], "content": f["content"]} for f in work_result["files"]], message=work_result["commit_message"]
    )
    return {"branch": branch, "base_branch": base_branch, "proposed": False, **commit_result}


async def run_task_execution_job(db: Session, job_id: uuid.UUID, owner_id: uuid.UUID, *, worker_id: str, lease_generation: int, lease_seconds: int) -> None:
    """`db` must already be scoped to `owner_id`'s RLS context (app/worker.py's
    _set_mainai_job_rls_owner) before this is called, exactly like run_corpus_review_job()."""
    job = db.get(MainAIJob, job_id)
    if job is None:
        return
    task = task_for_job(db, job)
    if task is None:
        mark_failed(db, job, worker_id=worker_id, lease_generation=lease_generation, error_category=MainAIJobErrorCategory.unexpected)
        return
    goal = db.get(MainAIGoal, task.goal_id)

    try:
        renew_mainai_job_lease(db, job_id, worker_id, lease_generation, lease_seconds)
        update_progress(db, job, worker_id=worker_id, lease_generation=lease_generation, current=0, total=1, phase=task.task_type)
        db.commit()
    except JobLeaseLostError:
        logger.warning("task_execution job %s: lease lost before dispatch.", job_id)
        return

    db.add(MainAITaskEvent(task_id=task.id, owner_id=task.owner_id, event_type=MainAITaskEventType.verification_started, detail={}))
    db.commit()

    try:
        # Checkpoint/resume (app/mainai_execution/checkpoint.py): if THIS job_id already has a
        # durable `work_result` checkpoint -- e.g. a previous worker process computed it (a
        # real, possibly expensive, possibly non-deterministic AI call) and then crashed before
        # finishing -- reuse it instead of repeating the call. The checkpoint, not this
        # function's own in-memory state, is the only thing trusted for "was this already done".
        work_checkpoint = latest_checkpoint_for_step(db, task_id=task.id, job_id=job_id, step="work_result")
        if work_checkpoint is not None:
            logger.info("task_execution job %s (task %s): resuming work_result from durable checkpoint.", job_id, task.id)
            work_result = work_checkpoint.executor_state["work_result"]
        else:
            if task.task_type == "read_only_audit":
                work_result = await _handle_read_only_audit(db, task)
            elif task.task_type == "repo_edit":
                work_result = await _handle_repo_edit(db, task)
            elif task.task_type == "run_tests":
                work_result = _handle_run_tests(task)
            elif task.task_type == "open_pr":
                work_result = await _handle_open_pr(db, task, goal)
            else:
                raise TaskExecutionError(f"Unknown task_type '{task.task_type}'.")
            # V0.2 fencing finding: unlike task-state/verification writes (which reach
            # durability only together with mark_completed()/mark_failed()'s own lease-checked
            # commit, in the except/success blocks below), this checkpoint commit used to be
            # its OWN, earlier, entirely unchecked commit -- reachable after a real AI call
            # that can run long enough for the lease to have already expired and been reclaimed
            # by someone else. Re-verifying (and, as a useful side effect, extending) the lease
            # immediately before this commit closes that window; a stale worker whose lease
            # is already gone stops here, before writing anything, exactly like the dispatch-
            # time check above.
            try:
                renew_mainai_job_lease(db, job_id, worker_id, lease_generation, lease_seconds)
            except JobLeaseLostError:
                logger.warning("task_execution job %s: lease lost before work_result checkpoint commit.", job_id)
                db.rollback()
                return
            record_checkpoint(db, task=task, goal=goal, job_id=job_id, step="work_result", data={"work_result": work_result})
            db.commit()

        if task.task_type == "run_tests":
            # run_tests' own primary work IS its verification. Re-derive the VerificationResult
            # directly from the real pytest outcomes already captured in work_result rather
            # than also calling verify_task() -- which would independently re-run the exact
            # same targets from task.verification_plan a second time (correctly catching a
            # failure either way, since both read the same plan, but doubling every run_tests
            # task's real subprocess pytest cost for no benefit).
            steps = [VerificationStepResult(kind="targeted_tests", passed=r["passed"], detail=r) for r in work_result["results"]]
            verification = VerificationResult(passed=all(s.passed for s in steps), steps=steps)
        else:
            verification = verify_task(task, cwd=str(_backend_root()))

        if verification.passed and task.task_type == "repo_edit":
            # Same resume discipline for the GitHub push itself -- once github_write_enabled is
            # on, this is the one step that is NOT safely repeatable (a second push creates a
            # second commit), so a crash between a successful push and this checkpoint landing
            # must not cause resume to push again.
            finalize_checkpoint = latest_checkpoint_for_step(db, task_id=task.id, job_id=job_id, step="finalized")
            if finalize_checkpoint is not None:
                finalize_info = finalize_checkpoint.executor_state["finalize_info"]
            else:
                # V0.2 fencing finding (same reasoning as the work_result checkpoint above,
                # applied before the ACTUAL irreversible side effect this time, not just
                # before its checkpoint): a stale worker whose lease already expired must
                # never reach _finalize_repo_edit()'s real GitHub push at all -- a second,
                # concurrent claimant could already be mid-flight on the exact same branch.
                try:
                    renew_mainai_job_lease(db, job_id, worker_id, lease_generation, lease_seconds)
                except JobLeaseLostError:
                    logger.warning("task_execution job %s: lease lost before GitHub push.", job_id)
                    db.rollback()
                    return
                finalize_info = await _finalize_repo_edit(db, task, work_result)
                try:
                    renew_mainai_job_lease(db, job_id, worker_id, lease_generation, lease_seconds)
                except JobLeaseLostError:
                    logger.warning("task_execution job %s: lease lost before finalized checkpoint commit (push already happened).", job_id)
                    db.rollback()
                    return
                record_checkpoint(db, task=task, goal=goal, job_id=job_id, step="finalized", data={"finalize_info": finalize_info})
                db.commit()
            work_result = {**work_result, **finalize_info}
            work_result.pop("previous_contents", None)  # not durable evidence -- was only needed transiently for a future diff/rollback view

    except (TaskExecutionError, VerificationStepError, GitHubClientError) as exc:
        logger.warning("task_execution job %s (task %s): %s", job_id, task.id, exc)
        db.rollback()
        job = db.get(MainAIJob, job_id)
        task = db.get(MainAITask, task.id)
        # Crash-matrix finding (P1, found by testing a REAL crash point rather than reasoning
        # about it): mark_failed()/mark_completed() (app/jobs/service.py) each end with their
        # OWN real db.commit() -- the same "shared helper commits internally" shape that
        # dispatch_ready_task()/create_job() already had to be fixed for earlier in this
        # hardening pass. Calling mark_failed() BEFORE _finalize_task_outcome() meant the JOB
        # became durably `failed` (a TERMINAL mainai_jobs status -- claim_next_mainai_job()
        # only ever reclaims `queued` or `running`-with-expired-lease rows, never a terminal
        # one) before the TASK's own outcome was ever made durable. A crash in that exact gap
        # left the task stuck at `running` PERMANENTLY: not retryable, not cancellable, and --
        # unlike the job's own stale-lease reclaim path -- there is no mechanism that ever
        # revisits a task whose job already reached a terminal status. Fixed the same way as
        # the dispatch/create_job race: the task's own finalization is flushed (not committed)
        # FIRST, and the shared helper's own commit is called LAST, so it becomes the ONE
        # atomic commit for both effects -- a crash before it loses nothing durable (the job
        # is still `running`, correctly reclaimable and resumable from its existing
        # checkpoints); a crash after it has nothing left to lose.
        _finalize_task_outcome(db, task, passed=False, evidence={"error": str(exc)})
        try:
            mark_failed(db, job, worker_id=worker_id, lease_generation=lease_generation, error_category=MainAIJobErrorCategory.unexpected)
        except JobLeaseLostError:
            logger.warning("task_execution job %s: lease lost while recording failure.", job_id)
            db.rollback()
            return
        return

    # Crash-matrix finding (P1) -- see the identical fix and full explanation in the except
    # block above; same reordering, same reasoning, applied to the success path.
    _finalize_task_outcome(db, task, passed=verification.passed, evidence={**verification.evidence(), "work_result": work_result})
    try:
        mark_completed(db, job, worker_id=worker_id, lease_generation=lease_generation, public_message="Task attempt completed; see task status for the verified outcome.")
    except JobLeaseLostError:
        logger.warning("task_execution job %s: lease lost while recording completion.", job_id)
        db.rollback()
        return


def _finalize_task_outcome(db: Session, task: MainAITask, *, passed: bool, evidence: dict) -> None:
    """THE completion gate — a task becomes `completed` if and only if verification passed
    (or genuinely had nothing to verify). A failed verification never produces `completed`: it
    produces `retryable_failed` (if attempts remain) or `failed` (attempts exhausted), and
    downstream tasks are re-evaluated via recompute_task_readiness() so a task depending on
    this one is correctly moved to `blocked`, never left silently `pending`."""
    db.add(
        MainAITaskEvent(
            task_id=task.id,
            owner_id=task.owner_id,
            event_type=MainAITaskEventType.verification_passed if passed else MainAITaskEventType.verification_failed,
            detail=evidence,
        )
    )

    if passed:
        task.status = MainAITaskStatus.completed
        task.completed_at = datetime.utcnow()
        db.add(MainAITaskEvent(task_id=task.id, owner_id=task.owner_id, event_type=MainAITaskEventType.completed, detail=evidence))
    else:
        if task.attempts < task.max_attempts:
            task.status = MainAITaskStatus.retryable_failed
        else:
            task.status = MainAITaskStatus.failed
            task.completed_at = datetime.utcnow()
        db.add(MainAITaskEvent(task_id=task.id, owner_id=task.owner_id, event_type=MainAITaskEventType.failed, detail=evidence))

    db.flush()
    recompute_task_readiness(db, goal_id=task.goal_id)
