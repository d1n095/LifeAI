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
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations.github_client import GitHubClientError, get_github_client
from app.jobs.mainai_job_lease import JobLeaseLostError, renew_mainai_job_lease
from app.jobs.retry import compute_backoff_seconds
from app.jobs.service import mark_cancelled, mark_completed, mark_failed, update_progress
from app.mainai_execution.checkpoint import latest_checkpoint_for_step, record_checkpoint
from app.mainai_execution.ci_wait import poll_ci_wait, start_ci_wait
from app.mainai_execution.executor import _lock_task, task_for_job
from app.mainai_execution.graph import recompute_task_readiness
from app.mainai_execution.verify import (
    VerificationResult,
    VerificationStepError,
    VerificationStepResult,
    decode_subprocess_output,
    validate_targeted_tests_target,
    verify_task,
)
from app.mainai_execution.worktree import (
    WorktreeError,
    commit_worktree_changes,
    create_task_worktree,
    push_worktree_branch,
    verify_worktree_ownership,
    worktree_git_status,
)
from app.models.mainai_execution import (
    MainAIGoal,
    MainAITask,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)
from app.models.mainai_job import MainAIJob, MainAIJobErrorCategory
from app.models.mainai_recovery import MainAITaskWorktree
from app.models.mainai_wait import MainAITaskWait, MainAITaskWaitStatus
from app.providers.base import Message
from app.providers.registry import chat_with_fallback

logger = logging.getLogger("mainai.execution")

# V0.3 retry-with-backoff: same compute_backoff_seconds() formula every other retry policy in
# this codebase uses (app/jobs/retry.py), task-appropriate base/cap -- a task retry re-runs a
# real AI call and possibly a real git push, not a cheap I/O operation, so this is minutes, not
# STEG 11's import-job seconds. See app/worker.py's `_advance_mainai_execution_retries` for the
# worker tick that actually acts on `next_retry_at` once it elapses.
TASK_RETRY_BASE_SECONDS = 30.0
TASK_RETRY_CAP_SECONDS = 900.0


class TaskCancelledCooperatively(Exception):
    """V0.3: raised internally by `_check_cancel_requested()` when it finds
    `job.cancel_requested` True at one of this function's safe checkpoints (see
    app/mainai_execution/executor.py's `cancel_task()` for how a `running` task's cancel
    request gets there in the first place -- the SAME `job.cancel_requested`/`request_cancel()`
    primitive `corpus_review.py` already uses, not a second cancellation mechanism). Caught by
    `run_task_execution_job()`'s own outer try/except and turned into a durable `cancelled`
    outcome for both the task and the job -- never `retryable_failed`/`failed` (a cancel is not
    a failure) and never silently ignored."""


def _check_cancel_requested(db: Session, job_id: uuid.UUID) -> None:
    """A safe checkpoint: never called mid-write, always between one durable fact and the
    next. Re-reads the job row fresh (`populate_existing=True`) rather than trusting a
    possibly-stale in-memory `job` -- the founder's cancel click can land at any real wall-clock
    moment, including between this function's own checks."""
    job = db.get(MainAIJob, job_id, populate_existing=True)
    if job is not None and job.cancel_requested:
        raise TaskCancelledCooperatively()


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


async def _propose_repo_edit(db: Session, task: MainAITask) -> dict:
    """github_write_enabled is off (or GitHub isn't configured): the ORIGINAL V0.1 proposal-only
    model, completely unchanged -- writes to the single shared local checkout this backend
    process itself runs from, purely so real local verification (verify_task()) can still run
    against real content, never touching the network or creating any isolated per-attempt
    state. Kept as a separate function (not a branch inside _handle_repo_edit()) specifically so
    the extensive existing path-traversal/symlink-escape hardening tests targeting this exact
    behavior (test_run_task_execution_job_repo_edit_with_an_absolute_ai_proposed_path_..., et
    al.) keep exercising the identical code, unchanged."""
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


async def _handle_repo_edit(
    db: Session, task: MainAITask, *, job: MainAIJob, worker_id: str, lease_generation: int, lease_seconds: int
) -> dict:
    """Hardening-pass finding (real production-scope gap, not a security bug per se): V0.1/
    early V0.2 always wrote a repo_edit's real content to the single shared backend checkout
    and pushed exclusively via the GitHub Git Data API -- the per-task worktree isolation
    worktree.py already built (real local `git commit`, ownership-verified per attempt) was
    never actually wired into this, the ONE handler it exists for. That meant
    LOCAL_UNCOMMITTED_WORK/LOCAL_COMMITTED_NOT_PUSHED, while fully proven correct at the
    recovery-pipeline level, could never actually occur for a real dead repo_edit task -- a
    genuine V0.2 scope gap, not merely a documentation one. Fixed here: when
    `github_write_enabled` is on and GitHub is configured, every real repo_edit attempt now
    creates (or, on a takeover's rebound worktree, reuses) its own isolated, ownership-verified
    worktree, edits ONLY inside it, and commits locally -- exactly the state
    recovery_inspector.py/recovery_classifier.py already read.

    Salvage continuation (dedup, section 5 of the hardening pass): checked BEFORE any AI call.
    If this job's worktree already carries real, ownership-verified progress (uncommitted edits
    OR a local commit -- both durable git facts, never a DB checkpoint) from a prior dead
    attempt that recovery_salvage.py rebound here, that progress is reused as-is; the AI is
    never called a second time for work that already, genuinely exists on disk. Ownership
    mismatch/ambiguity for an existing worktree row fails closed (TaskExecutionError), per the
    founder's explicit instruction -- never silently treated as "nothing here" and overwritten.

    Durability finding (this pass, found by driving a REAL crash through this exact function
    rather than constructing recovery state by hand): every fact this function produces that
    recovery_classifier.py depends on must be committed to durable storage BEFORE the next,
    riskier step runs. A genuine process crash (connection dropped, not a caught exception)
    rolls back whatever this db session had only flushed, not committed -- so without these
    explicit commits, LOCAL_UNCOMMITTED_WORK/LOCAL_COMMITTED_NOT_PUSHED would still be
    unreachable from a real crash even with worktree.py fully wired in: a crashed worker's
    worktree ROW itself (and, separately, `worktree.current_commit`, which
    recovery_classifier.py reads directly from this column rather than re-deriving it live --
    see its own docstring) would simply never have existed as far as recovery is concerned,
    even though real git state sits on disk. Each commit point below is preceded by the exact
    same lease-renewal check every other real side effect in this file already uses, for the
    exact same reason: a stale worker must never durably write anything, including "just" a
    worktree row -- JobLeaseLostError propagates out of this function uncaught so the one call
    site in run_task_execution_job() can stop exactly like every other fenced-out write here."""
    settings = get_settings()
    if not settings.github_write_enabled:
        return await _propose_repo_edit(db, task)

    client = get_github_client()
    if not client.is_configured():
        raise TaskExecutionError(
            "GITHUB_WRITE_ENABLED är på men GITHUB_TOKEN/GITHUB_REPO saknas -- kan inte skapa en verklig, "
            "ownership-verifierad worktree för denna repo_edit-task."
        )

    existing_worktree = db.query(MainAITaskWorktree).filter(MainAITaskWorktree.job_id == job.id).one_or_none()
    if existing_worktree is not None:
        if not verify_worktree_ownership(existing_worktree):
            raise TaskExecutionError(
                f"Worktree ownership verification failed for job {job.id} -- refusing to trust unverifiable local state (fail closed)."
            )
        status = worktree_git_status(existing_worktree)
        if status["has_uncommitted_changes"]:
            logger.info(
                "task_execution job %s (task %s): committing salvaged uncommitted worktree progress -- no new AI call.", job.id, task.id
            )
            renew_mainai_job_lease(db, job.id, worker_id, lease_generation, lease_seconds)
            commit_message = f"MainAI: {task.description[:180]}"
            commit_worktree_changes(db, existing_worktree, message=commit_message)
            db.commit()
            return {
                "worktree_id": str(existing_worktree.id),
                "worktree_path": existing_worktree.path,
                "branch": existing_worktree.branch,
                "resumed_from_worktree": True,
                "commit_message": commit_message,
            }
        if existing_worktree.current_commit:
            logger.info(
                "task_execution job %s (task %s): reusing already-committed salvaged worktree progress -- no new AI call.", job.id, task.id
            )
            return {
                "worktree_id": str(existing_worktree.id),
                "worktree_path": existing_worktree.path,
                "branch": existing_worktree.branch,
                "resumed_from_worktree": True,
                "commit_message": f"MainAI: {task.description[:180]}",
            }
        worktree = existing_worktree
    else:
        worktree = await create_task_worktree(db, task=task, job=job, lease_generation=job.lease_generation, executor_id=worker_id)
        renew_mainai_job_lease(db, job.id, worker_id, lease_generation, lease_seconds)
        db.commit()

    messages = [Message(role="system", content=CODE_AGENT_SYSTEM_PROMPT), Message(role="user", content=task.description)]
    result, _attempted = await chat_with_fallback(db, messages)
    parsed = _parse_code_agent_response(result.content)

    worktree_root = Path(worktree.path).resolve()
    for file in parsed["files"]:
        # Identical two-layer defense _propose_repo_edit() already uses (syntactic guard at
        # parse time + this resolve()-and-compare confinement check), now against the isolated
        # worktree root instead of the shared repo root.
        abs_path = (worktree_root / file["path"]).resolve()
        if abs_path != worktree_root and worktree_root not in abs_path.parents:
            raise TaskExecutionError(f"Refusing to write outside the worktree root: {file['path']!r} resolved to {abs_path}.")
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(file["content"])

    commit_message = parsed.get("commit_message", task.description[:200])
    renew_mainai_job_lease(db, job.id, worker_id, lease_generation, lease_seconds)
    commit_worktree_changes(db, worktree, message=commit_message)
    db.commit()

    return {
        "worktree_id": str(worktree.id),
        "worktree_path": worktree.path,
        "branch": worktree.branch,
        "files": [f["path"] for f in parsed["files"]],
        "commit_message": commit_message,
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
    return {
        "proposed": False,
        "pull_request_number": pr.get("number"),
        "pull_request_url": pr.get("html_url"),
        # V0.3: the exact commit the PR's checks run against -- ci_wait.py's start_ci_wait()
        # polls THIS sha, never "the branch's current tip" (see that module's docstring for why
        # that distinction matters). `edit_info["commit_sha"]` is only present once a real push
        # happened (see _finalize_repo_edit()'s own return); in proposal mode it's absent, and
        # this dict's own "proposed": False above is only reachable when github_write_enabled
        # is on and a real PR (existing or freshly created) exists, so a real commit_sha is
        # always expected here in that case.
        "head_sha": edit_info.get("commit_sha"),
    }


async def _finalize_repo_edit(db: Session, task: MainAITask, work_result: dict) -> dict:
    """Only called once verify_task() has already passed (see run_task_execution_job()).

    Hardening-pass rewrite: the real push now goes through worktree.py's real local
    `git push` (worktree.py's own `push_worktree_branch()`) against the SAME isolated,
    ownership-verified worktree `_handle_repo_edit()` already committed to -- never the GitHub
    Git Data API's remote-only commit-construction this function used before wiring worktree.py
    into the live path. `work_result["worktree_id"]` is only ever absent in proposal mode
    (github_write_enabled off, see `_propose_repo_edit()`), which this function still serves
    identically to V0.1.

    Crash-matrix finding (P1, ORIGINAL hardening pass, still true and now simpler to satisfy):
    a crash AFTER a real push succeeds but BEFORE the "finalized" checkpoint commits must never
    cause a resume to push a second, redundant commit. The OLD Git-Data-API version needed a
    "does the branch already exist, build on its real tip" workaround for this, because it
    reconstructed a brand new commit from `work_result["files"]` on every call. A real local
    `git push` of the SAME, already-committed local branch has no such problem: pushing a
    branch whose tip already matches origin's tip is a genuine no-op ("Everything up-to-date",
    exit 0) -- calling this function twice for the same worktree is naturally idempotent,
    without needing any tip-tracking logic at all."""
    branch = f"claude/mainai-task-{task.id}"
    base_branch = "claude/det-kommer-mer-879lcm"

    worktree_id = work_result.get("worktree_id")
    if worktree_id is None:
        # Proposal mode (github_write_enabled off) -- unchanged from V0.1.
        return {"branch": branch, "base_branch": base_branch, "proposed": True, "files": [f["path"] for f in work_result["files"]]}

    worktree = db.query(MainAITaskWorktree).filter(MainAITaskWorktree.id == uuid.UUID(worktree_id)).one()
    if not verify_worktree_ownership(worktree):
        raise TaskExecutionError(
            f"Worktree ownership verification failed for job {worktree.job_id} at push time -- refusing to trust unverifiable local state."
        )
    remote_sha = await push_worktree_branch(worktree)
    return {"branch": worktree.branch, "base_branch": base_branch, "proposed": False, "commit_sha": remote_sha}


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
            # V0.3 cooperative-cancel checkpoint ("before edit"/"before PR"): a resumed step
            # (the `if` branch above) never re-checks here -- real work already legitimately
            # started for it, and this checkpoint's whole point is to stop BEFORE the next
            # real side effect, not to abandon one already durably in flight.
            _check_cancel_requested(db, job_id)
            if task.task_type == "read_only_audit":
                work_result = await _handle_read_only_audit(db, task)
            elif task.task_type == "repo_edit":
                # _handle_repo_edit() makes its own internal, lease-checked commits (worktree
                # creation, real local git commits) since those facts must be durable before
                # this function's own AI call / longer-running steps run -- see its docstring.
                # A lease lost during one of those internal steps propagates JobLeaseLostError
                # here uncaught, handled exactly like every other lease-checked commit point in
                # this function: log, roll back, stop silently (another worker now owns this).
                try:
                    work_result = await _handle_repo_edit(
                        db, task, job=job, worker_id=worker_id, lease_generation=lease_generation, lease_seconds=lease_seconds
                    )
                except JobLeaseLostError:
                    logger.warning("task_execution job %s: lease lost during repo_edit worktree operations.", job_id)
                    db.rollback()
                    return
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

        # V0.3 cooperative-cancel checkpoint ("after edit"/"after local commit"): any real
        # local git commit repo_edit made is already durable at this point (see
        # _handle_repo_edit()'s own commit inside the block above) -- stopping here never
        # destroys it, it only stops BEFORE the (real, subprocess-spawning) verification step.
        _check_cancel_requested(db, job_id)

        # V0.2 duplicate-side-effect finding: verification was never checkpointed at all --
        # even when work_result itself resumed from a durable checkpoint above (recovery's own
        # salvaged evidence, or a plain crash-after-work_result-before-verification resume),
        # this function still called verify_task() again from scratch every time, silently
        # re-running whatever real subprocess side effects the task's verification_plan has
        # (targeted_tests -> a real `python -m pytest` invocation). A "verification" checkpoint
        # is only ever recorded for a PASS (see below) -- a durably recorded FAILURE is
        # deliberately never reused, since retrying is supposed to give a fresh attempt a real
        # chance to pass, not have it fail from a stale, possibly now-inapplicable verdict.
        verification_checkpoint = latest_checkpoint_for_step(db, task_id=task.id, job_id=job_id, step="verification")
        if verification_checkpoint is not None:
            logger.info("task_execution job %s (task %s): resuming verification from durable checkpoint.", job_id, task.id)
            evidence = verification_checkpoint.executor_state["verification"]
            verification = VerificationResult(
                passed=evidence["passed"],
                steps=[VerificationStepResult(kind=s["kind"], passed=s["passed"], detail=s["detail"]) for s in evidence["steps"]],
            )
        elif task.task_type == "run_tests":
            # run_tests' own primary work IS its verification. Re-derive the VerificationResult
            # directly from the real pytest outcomes already captured in work_result rather
            # than also calling verify_task() -- which would independently re-run the exact
            # same targets from task.verification_plan a second time (correctly catching a
            # failure either way, since both read the same plan, but doubling every run_tests
            # task's real subprocess pytest cost for no benefit).
            steps = [VerificationStepResult(kind="targeted_tests", passed=r["passed"], detail=r) for r in work_result["results"]]
            verification = VerificationResult(passed=all(s.passed for s in steps), steps=steps)
        else:
            # Hardening-pass finding: a repo_edit task whose real edits now live inside an
            # isolated worktree (see _handle_repo_edit()) must be verified against THAT
            # worktree's own backend/ subdirectory -- the exact same repo layout as
            # _backend_root(), just at a different, per-attempt path -- never against the
            # shared checkout, which never received these edits at all once worktree.py is
            # wired into the live path. Proposal mode (no worktree_path in work_result) keeps
            # verifying against the shared checkout, unchanged from V0.1.
            worktree_path = work_result.get("worktree_path") if task.task_type == "repo_edit" else None
            verify_cwd = str(Path(worktree_path) / "backend") if worktree_path else str(_backend_root())
            verification = verify_task(task, cwd=verify_cwd)

        if verification_checkpoint is None and verification.passed:
            try:
                renew_mainai_job_lease(db, job_id, worker_id, lease_generation, lease_seconds)
            except JobLeaseLostError:
                logger.warning("task_execution job %s: lease lost before verification checkpoint commit.", job_id)
                db.rollback()
                return
            record_checkpoint(db, task=task, goal=goal, job_id=job_id, step="verification", data={"verification": verification.evidence()})
            db.commit()

        if verification.passed and task.task_type == "repo_edit":
            # V0.3 cooperative-cancel checkpoint ("before push"): the local commit already
            # stands regardless; this is the last chance to stop before the one NOT-safely-
            # repeatable real network side effect (a real `git push`) in this whole function.
            _check_cancel_requested(db, job_id)
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

        entered_ci_wait = False
        if verification.passed and task.task_type == "open_pr" and not work_result.get("proposed", True) and work_result.get("head_sha"):
            # V0.3: a real PR now exists on GitHub for a real commit -- the task is not actually
            # DONE yet (its own goal is "land verified work", not merely "open a PR"), so it
            # moves to `waiting_ci` instead of `completed`. Deliberately does NOT go through the
            # work_result/finalized checkpoint machinery above (this isn't a resumable step --
            # start_ci_wait() is its own, separately durable commit) and deliberately does NOT
            # call _finalize_task_outcome() below -- that would prematurely mark the task
            # completed. The mainai_jobs row this function is running as still reaches its own
            # terminal `completed` state at the bottom of this function: THIS job's own work
            # (open the PR) is genuinely done; only the TASK is still waiting. See
            # app/mainai_execution/ci_wait.py's module docstring and app/worker.py's
            # `_poll_mainai_task_waits` for how the wait later resolves back into this same
            # task's own terminal outcome via resume_waiting_ci_task() below.
            try:
                renew_mainai_job_lease(db, job_id, worker_id, lease_generation, lease_seconds)
            except JobLeaseLostError:
                logger.warning("task_execution job %s: lease lost before starting CI wait.", job_id)
                db.rollback()
                return
            start_ci_wait(db, task=task, job_id=job_id, repo=get_settings().github_repo, sha=work_result["head_sha"])
            db.commit()
            entered_ci_wait = True

    except TaskCancelledCooperatively:
        logger.info("task_execution job %s (task %s): cooperative cancel acknowledged at a safe checkpoint.", job_id, task.id)
        db.rollback()
        job = db.get(MainAIJob, job_id)
        task = db.get(MainAITask, task.id)
        # Same ordering discipline as the outcome except-block below: this function's own
        # write is flushed (not committed) FIRST, and mark_cancelled()'s own commit is called
        # LAST, so a crash between the two loses nothing durable either way.
        _finalize_cancelled_task(db, task, reason="cooperative_cancel")
        try:
            mark_cancelled(db, job, worker_id=worker_id, lease_generation=lease_generation)
        except JobLeaseLostError:
            logger.warning("task_execution job %s: lease lost while recording cooperative cancellation.", job_id)
            db.rollback()
            return
        return

    except (TaskExecutionError, VerificationStepError, GitHubClientError, WorktreeError) as exc:
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

    if entered_ci_wait:
        # V0.3: the TASK stays `waiting_ci` (see above) -- only this JOB's own work (opening
        # the PR) is done, so only the job reaches a terminal status here.
        try:
            mark_completed(db, job, worker_id=worker_id, lease_generation=lease_generation, public_message="Pull request opened; waiting for CI.")
        except JobLeaseLostError:
            logger.warning("task_execution job %s: lease lost while recording PR-opened completion.", job_id)
            db.rollback()
        return

    # Crash-matrix finding (P1) -- see the identical fix and full explanation in the except
    # block above; same reordering, same reasoning, applied to the success path.
    _finalize_task_outcome(
        db,
        task,
        passed=verification.passed,
        evidence={**verification.evidence(), "work_result": work_result},
        job_id=job_id,
    )
    try:
        mark_completed(db, job, worker_id=worker_id, lease_generation=lease_generation, public_message="Task attempt completed; see task status for the verified outcome.")
    except JobLeaseLostError:
        logger.warning("task_execution job %s: lease lost while recording completion.", job_id)
        db.rollback()
        return


def _finalize_task_outcome(
    db: Session, task: MainAITask, *, passed: bool, evidence: dict, job_id: uuid.UUID | None = None
) -> None:
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
        task.next_retry_at = None
        db.add(MainAITaskEvent(task_id=task.id, owner_id=task.owner_id, event_type=MainAITaskEventType.completed, detail=evidence))
    else:
        if task.attempts < task.max_attempts:
            task.status = MainAITaskStatus.retryable_failed
            # V0.3: schedule the automatic retry-with-backoff scan (app/worker.py's
            # `_advance_mainai_execution_retries`) rather than leaving this task sitting
            # `retryable_failed` until a founder manually retries it -- app/mainai_execution/
            # executor.py's retry_task() still exists unchanged for that manual path too.
            task.next_retry_at = datetime.utcnow() + timedelta(
                seconds=compute_backoff_seconds(task.attempts, base=TASK_RETRY_BASE_SECONDS, cap=TASK_RETRY_CAP_SECONDS)
            )
        else:
            task.status = MainAITaskStatus.failed
            task.completed_at = datetime.utcnow()
            task.next_retry_at = None
            # Closed-loop learning: only exhausted + structured verification evidence may
            # become an EngineeringLesson (never exception str / provider prose).
            from app.mainai_execution.lesson_from_verification import maybe_record_lesson_from_exhausted_verification

            maybe_record_lesson_from_exhausted_verification(db, task=task, evidence=evidence, job_id=job_id)
        db.add(MainAITaskEvent(task_id=task.id, owner_id=task.owner_id, event_type=MainAITaskEventType.failed, detail=evidence))

    # Closed-loop learning, the back-edge: every lesson this task's plan actually applied gets
    # ONE append-only observation of what happened to its own regression target here -- on
    # success as well as failure, since only recording failures would bias every lesson's
    # evidence negative. Never mutates the lesson itself; no-op unless the evidence carries
    # structured verification steps. See lesson_effectiveness.py's module docstring.
    from app.mainai_execution.lesson_effectiveness import record_lesson_effectiveness_from_finalize

    record_lesson_effectiveness_from_finalize(db, task=task, evidence=evidence, passed=passed, job_id=job_id)

    db.flush()
    recompute_task_readiness(db, goal_id=task.goal_id)


def _finalize_cancelled_task(db: Session, task: MainAITask, *, reason: str) -> None:
    """V0.3: the cooperative-cancel counterpart to `_finalize_task_outcome()` -- a genuinely
    DIFFERENT terminal outcome, never routed through that function, because a cancel is not a
    failure: it must never count against `task.attempts`, schedule a `next_retry_at`, or record
    a `verification_failed` event that didn't actually happen. Records `cancelling` (the
    in-flight acknowledgment) then `cancelled` (the durable terminal event), matching
    MainAIJobEventType's own cancel_requested -> cancel_acknowledged -> cancelled shape."""
    db.add(MainAITaskEvent(task_id=task.id, owner_id=task.owner_id, event_type=MainAITaskEventType.cancelling, detail={"reason": reason}))
    task.status = MainAITaskStatus.cancelled
    task.completed_at = datetime.utcnow()
    task.next_retry_at = None
    db.add(MainAITaskEvent(task_id=task.id, owner_id=task.owner_id, event_type=MainAITaskEventType.cancelled, detail={"reason": reason}))
    db.flush()
    recompute_task_readiness(db, goal_id=task.goal_id)


async def resume_waiting_ci_task(db: Session, wait: MainAITaskWait) -> None:
    """Called from app/worker.py's `_poll_mainai_task_waits` tick for one due (`pending`,
    `next_poll_at` elapsed) wait -- polls it for real (app/mainai_execution/ci_wait.py's
    poll_ci_wait()) and, ONLY once it reaches a terminal outcome, folds that outcome back into
    the owning task via `_finalize_task_outcome()` (the SAME single completion gate every other
    task_type's outcome goes through -- CI-wait does not get its own, separate notion of
    "done"). Deliberately does not commit -- the caller commits (or rolls back) per-wait, the
    same per-item isolation `_advance_mainai_execution_tasks`/`_retry_storage_deletion_tasks`
    (app/worker.py) already use, so one wait's failure can never abort the whole tick.

    Defensive no-op if the task is no longer `waiting_ci` (e.g. a cooperative cancel already
    resolved it, see app/mainai_execution/ci_wait.py's cancel_ci_wait()) -- this function is
    never the only path that can move a task off `waiting_ci`.

    Locks the task row FIRST (`_lock_task()`, the same primitive dispatch_ready_task()/
    retry_task()/cancel_task() already use for every other task-status transition in this
    codebase) before checking status -- two concurrent worker processes both polling the SAME
    due wait (app/worker.py's `_poll_mainai_task_waits` has no row-level claim of its own, only
    a `status == pending` filter) could otherwise both read `waiting_ci`, both poll GitHub, and
    both call `_finalize_task_outcome()` on the same task -- a double-dispatch-shaped race, not
    just a wasted API call, since a second finalize on an already-`completed`/`retryable_failed`
    task would double-increment `attempts` or double-schedule `next_retry_at`. The loser blocks
    on the lock, re-reads the FRESH row after the winner commits, and finds `task.status` no
    longer `waiting_ci` -- a clean no-op, same shape as every other lock-then-recheck guard."""
    task = _lock_task(db, wait.task_id)
    if task.status != MainAITaskStatus.waiting_ci:
        return

    wait = await poll_ci_wait(db, wait)
    if wait.status == MainAITaskWaitStatus.pending:
        return

    goal = db.get(MainAIGoal, task.goal_id)
    if wait.status == MainAITaskWaitStatus.satisfied:
        db.add(MainAITaskEvent(task_id=task.id, owner_id=task.owner_id, event_type=MainAITaskEventType.wait_satisfied, detail=wait.evidence))
        _finalize_task_outcome(db, task, passed=True, evidence={"ci_wait": wait.evidence})
    elif wait.status == MainAITaskWaitStatus.timed_out:
        db.add(
            MainAITaskEvent(
                task_id=task.id, owner_id=task.owner_id, event_type=MainAITaskEventType.wait_timed_out, detail=wait.evidence
            )
        )
        _finalize_task_outcome(db, task, passed=False, evidence={"ci_wait": wait.evidence, "reason": "timed_out"})
    else:
        # failed or cancelled -- both mean "this attempt did not produce verified, green work".
        db.add(MainAITaskEvent(task_id=task.id, owner_id=task.owner_id, event_type=MainAITaskEventType.wait_failed, detail=wait.evidence))
        _finalize_task_outcome(db, task, passed=False, evidence={"ci_wait": wait.evidence})
    if goal is not None:
        record_checkpoint(db, task=task, goal=goal, job_id=wait.job_id, step="ci_wait_resolved", data={"wait_status": wait.status.value})
