"""MainAI V0.3 — CI-wait: the first (and, for V0.3, only) concrete implementation of the
general-but-minimal `mainai_task_waits` abstraction (app/models/mainai_wait.py). Owns exactly
two things: starting a durable wait record for a task's real, already-pushed GitHub commit,
and polling GitHub's Checks API (app/integrations/github_client.py's `list_check_runs()`,
already fully implemented since V0.1 but never called anywhere until now) to decide whether
that commit's checks have concluded.

Deliberately knows NOTHING about `mainai_jobs`/leases/fencing, and nothing about how a
resolved wait's outcome gets folded back into the owning MainAITask's own status — that
belongs to app/mainai_execution/execution_job.py (which already owns `_finalize_task_outcome`,
the one place a task's terminal outcome is ever decided) and app/worker.py's poll tick
(`_poll_mainai_task_waits`), exactly the same "durable state here, orchestration decision
there" split V0.2's worktree/recovery-record split already established. This keeps ci_wait.py
importable from execution_job.py with no import cycle.

Stale-SHA defense: `poll_ci_wait()` always re-reads `wait.resource_ref["sha"]` from the
DURABLE row and polls exactly that SHA — never a SHA passed in by a caller, and never "the
branch's current tip" (which could have moved since the wait was created, e.g. a force-push or
a second, unrelated commit) — a security-relevant distinction: polling a DIFFERENT commit's
checks and treating them as this task's own would be a real integrity bug, not just an
inefficiency."""

import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.integrations.github_client import GitHubClientError, get_github_client
from app.jobs.retry import compute_backoff_seconds
from app.models.mainai_execution import MainAITask, MainAITaskEvent, MainAITaskEventType, MainAITaskStatus
from app.models.mainai_wait import MainAITaskWait, MainAITaskWaitSourceType, MainAITaskWaitStatus

# Deliberately minutes, not STEG 11's 2s/30s import-job values — a CI run realistically takes
# minutes, not seconds, so polling that fast would only waste GitHub API quota. Same
# compute_backoff_seconds() formula (full-jitter exponential), different, CI-appropriate
# base/cap passed explicitly — no second backoff policy, just different parameters for a
# genuinely different kind of wait.
CI_POLL_BASE_SECONDS = 20.0
CI_POLL_CAP_SECONDS = 300.0
# A real CI run that hasn't concluded after 6 hours is being treated as failed-to-converge, not
# silently waited on forever — the founder's explicit "a task must never become falsely
# completed by waiting forever" requirement.
DEFAULT_CI_WAIT_TIMEOUT_SECONDS = 6 * 60 * 60

# GitHub Checks API conclusions that mean "this check run is done, and it did NOT pass" --
# closed vocabulary, not "anything that isn't literally 'success'", so a conclusion this code
# has never seen before (a future GitHub API addition) is NOT silently treated as a pass (see
# evaluate_check_runs()'s own fail-closed default below).
_FAILING_CONCLUSIONS = frozenset({"failure", "timed_out", "cancelled", "action_required", "stale"})
_PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})


class CIWaitError(Exception):
    """Base for ci_wait.py-raised errors."""


def start_ci_wait(db: Session, *, task: MainAITask, job_id: uuid.UUID, repo: str, sha: str) -> MainAITaskWait:
    """Creates the durable wait record and moves `task` to `waiting_ci` -- called from
    execution_job.py's open_pr handling once a REAL PR has actually been created (never in
    proposal mode, which has no real commit on GitHub for any check run to attach to).
    `job_id` is only ever used for `mainai_task_waits.job_id`'s own FK/UNIQUE constraint (one
    job attempt creates at most one wait, matching mainai_task_worktrees/
    mainai_recovery_records precedent) -- never re-used for lease fencing here, since the
    owning job is marked `completed` immediately after this call (see execution_job.py) and no
    further job-authorized write ever happens on this wait."""
    now = datetime.utcnow()
    wait = MainAITaskWait(
        task_id=task.id,
        owner_id=task.owner_id,
        job_id=job_id,
        source_type=MainAITaskWaitSourceType.github_check_runs,
        resource_ref={"repo": repo, "sha": sha},
        condition={},
        status=MainAITaskWaitStatus.pending,
        poll_count=0,
        next_poll_at=now + timedelta(seconds=CI_POLL_BASE_SECONDS),
        backoff_seconds=int(CI_POLL_BASE_SECONDS),
        deadline_at=now + timedelta(seconds=DEFAULT_CI_WAIT_TIMEOUT_SECONDS),
        evidence={},
        created_at=now,
    )
    db.add(wait)
    task.status = MainAITaskStatus.waiting_ci
    db.add(
        MainAITaskEvent(
            task_id=task.id,
            owner_id=task.owner_id,
            event_type=MainAITaskEventType.wait_started,
            detail={"source_type": "github_check_runs", "repo": repo, "sha": sha},
        )
    )
    db.flush()
    return wait


def evaluate_check_runs(check_runs: list[dict]) -> tuple[bool, bool, dict]:
    """Pure decision function -- no I/O, unit-testable independent of the real GitHub API.
    Returns (done, passed, evidence_summary).

    No check runs at all is NOT "done, passed" -- a commit that hasn't been picked up by CI yet
    (or a repo with no CI configured, which V0.3 cannot distinguish from "not started yet" from
    this API alone) must never be silently treated as green; it stays `not done` until either a
    check run appears or the wait's own deadline_at times it out.

    Fails CLOSED on an unrecognized conclusion string (not in either closed set): treated as
    NOT passed once done, rather than assumed to be a pass -- a future GitHub API addition this
    code has never seen must never be silently interpreted as success."""
    if not check_runs:
        return False, False, {"check_run_count": 0}

    summary = [{"name": cr.get("name"), "status": cr.get("status"), "conclusion": cr.get("conclusion")} for cr in check_runs]
    if any(cr.get("status") != "completed" for cr in check_runs):
        return False, False, {"check_run_count": len(check_runs), "runs": summary}

    conclusions = {cr.get("conclusion") for cr in check_runs}
    if conclusions <= _PASSING_CONCLUSIONS:
        return True, True, {"check_run_count": len(check_runs), "runs": summary}
    return True, False, {"check_run_count": len(check_runs), "runs": summary}


async def poll_ci_wait(db: Session, wait: MainAITaskWait) -> MainAITaskWait:
    """Real, single poll attempt against GitHub's Checks API for `wait`'s OWN durable
    (repo, sha) -- never a caller-supplied one (see this module's docstring). Idempotent to
    call on an already-terminal wait (a no-op returning it unchanged) so a caller never needs
    to check status before calling.

    A transient GitHubClientError (rate limit, network blip) does NOT fail the wait -- it
    just reschedules the next poll at the same backoff step, exactly like any other transient
    retry in this codebase; only a real check-run conclusion or the wait's own deadline_at can
    make it terminal."""
    if wait.status != MainAITaskWaitStatus.pending:
        return wait

    now = datetime.utcnow()
    repo = wait.resource_ref.get("repo")
    sha = wait.resource_ref.get("sha")

    try:
        # Hardening-pass finding (P3, no optimistic success): a wait record missing its own
        # repo/sha (should be structurally impossible given start_ci_wait()'s own callers, but
        # never assumed) must fail closed here too, exactly like a genuine repo-drift -- never
        # silently skip the identity check just because the field it's comparing against is
        # itself falsy.
        if not repo or not sha:
            raise GitHubClientError(f"wait {wait.id} is missing its own durable repo/sha -- refusing to poll rather than guess.")
        client = get_github_client()
        # Defense in depth beyond just polling the right SHA (see module docstring): if this
        # backend's currently-configured repo has drifted from the repo this wait was created
        # for (a config change between wait creation and poll time), refuse to poll rather than
        # silently asking the WRONG repository whether an unrelated commit's checks passed.
        if client.settings.github_repo != repo:
            raise GitHubClientError(f"configured github_repo '{client.settings.github_repo}' no longer matches this wait's own repo '{repo}'.")
        check_runs = await client.list_check_runs(sha)
        done, passed, evidence = evaluate_check_runs(check_runs)
    except GitHubClientError as exc:
        done, passed, evidence = False, False, {"poll_error": str(exc)}

    wait.poll_count += 1
    wait.last_polled_at = now
    wait.evidence = evidence

    if done:
        wait.status = MainAITaskWaitStatus.satisfied if passed else MainAITaskWaitStatus.failed
        wait.resolved_at = now
    elif now >= wait.deadline_at:
        wait.status = MainAITaskWaitStatus.timed_out
        wait.resolved_at = now
    else:
        # Bounded exponential backoff, full jitter -- same formula every other retry policy in
        # this codebase uses (app/jobs/retry.py), CI-appropriate base/cap (see module docstring).
        wait.backoff_seconds = int(compute_backoff_seconds(wait.poll_count, base=CI_POLL_BASE_SECONDS, cap=CI_POLL_CAP_SECONDS)) or 1
        wait.next_poll_at = min(now + timedelta(seconds=wait.backoff_seconds), wait.deadline_at)

    db.add(wait)
    db.flush()
    return wait


def cancel_ci_wait(db: Session, wait: MainAITaskWait, *, reason: str) -> MainAITaskWait:
    """Cooperative-cancellation integration point (app/mainai_execution/cancellation.py): a
    `waiting_ci` task's own cancel request must resolve its wait record too, not just flip the
    task's status, or a later poll tick would keep trying to advance a wait whose task no
    longer cares about the answer."""
    if wait.status == MainAITaskWaitStatus.pending:
        wait.status = MainAITaskWaitStatus.cancelled
        wait.resolved_at = datetime.utcnow()
        wait.evidence = {**wait.evidence, "cancel_reason": reason}
        db.add(wait)
        db.flush()
    return wait
