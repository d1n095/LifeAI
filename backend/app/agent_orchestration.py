"""MainAI Core: agent orchestration — the first vertical slice of "samtal -> relevant minne ->
helhetsbild -> problem -> agentuppdrag -> kod -> granskning -> GitHub-PR -> checkpoint ->
nästa session" (CLAUDE.md's 2026-07-26 direction).

MainAI owns the task, the memory, the context, the rules, the result, and the review history
(every AgentTaskEvent row) — external LLM providers are used only as swappable *engines* for
the "code agent" and "review agent" roles, via the exact same provider adapters
(app/providers/registry.py) chat already uses. No new agent runtime, no new event
architecture: dispatch is one chat_with_fallback() call per role.

GitHub write access is real (app/integrations/github_client.py) but gated hard:
  - `settings.github_write_enabled=False` (the default): prepare_github_pr() only ever
    PROPOSES an exact branch/commit/PR — it never calls GitHub. This is the "read/analyze/
    propose" phase CLAUDE.md's section 7 asks to start with.
  - Even with write enabled, a task must have an approved review AND recorded test results
    before a PR can be prepared — see _require_approved_with_tests().
  - attempt_auto_merge() ALWAYS reports blocked in this slice: there is no merge method in
    github_client.py to call at all (see that module's docstring), independent of
    `github_auto_merge_enabled`. Never force-pushes, never deletes a branch, never deploys.

Local-first status (see docs/MAINAI_ARCHITECTURE.md §1's "MainAI är systemets intelligens,
inte en extern tjänst" grundprincip for the full principle this is measured against):
  - **Retrieval is fully local.** `retrieve_relevant_context()`/the system map
    (app/project_memory.py) never call an external provider — they read MainAI's own stored
    notes/checkpoints/sources and a textual scan of the repository.
  - **Orchestration is provider-decoupled, not yet local-first.** dispatch_task()/
    review_task() go through the same provider-agnostic `chat_with_fallback()` chat uses —
    no code here knows which external LLM answered, and any configured provider can be
    swapped without touching this module. But if EVERY configured provider fails or is
    unconfigured, there is currently no local reasoning fallback: dispatch/review simply
    cannot produce a code/review response (see dispatch_task_route's `ProviderError` handling
    in app/routers/agents.py, which turns that failure into a clean, non-leaking 503 instead
    of an unhandled exception — it does not add a local alternative).
  - **The Local MainAI Capability Layer remains planned work, not built here.** A local
    reasoning/model fallback for the code/review agent roles is target architecture (see
    docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §8's build order), not part of this slice.
"""

import re
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations.github_client import GitHubClientError, get_github_client
from app.models.agent_task import AgentRole, AgentTask, AgentTaskEvent, AgentTaskEventType, AgentTaskStatus
from app.project_memory import ingest_github_status
from app.providers.base import Message
from app.providers.registry import chat_with_fallback

CODE_AGENT_SYSTEM_PROMPT = (
    "Du är MainAI:s kodagent. Du får ett avgränsat uppdrag: berörda filer, vad som INTE får "
    "ändras, acceptanskriterier och krävda tester. Svara ENDAST med en unified diff (git "
    "diff-format) följt av en kort sammanfattning. Håll dig strikt inom scope — lägg inte till "
    "funktionalitet eller refaktorering som inte krävs av uppdraget."
)

REVIEW_AGENT_SYSTEM_PROMPT = (
    "Du är MainAI:s granskningsagent. Du får ett kodagentuppdrag, dess diff och testresultat. "
    "Granska: diffens scope (håller den sig inom uppdraget?), arkitekturöverensstämmelse, "
    "duplicering, felhantering, autentisering/behörighet, migrationer, regressioner, tester, "
    "dokumentation, och om resultatet uppfyller acceptanskriterierna. Svara med första raden "
    "exakt 'VERDICT: approved' eller 'VERDICT: needs_correction' eller 'VERDICT: rejected', "
    "följt av en motivering."
)

_REVIEW_VERDICT_RE = re.compile(r"^verdict:\s*(approved|rejected|needs_correction)\s*$")


def _parse_review_verdict(first_line: str) -> str:
    """Fail-closed parse of the review agent's mandated first line.

    Substring checks (`"approved" in line`) previously treated `VERDICT: not approved` and
    `unapproved` as approvals. Only an exact `VERDICT: <token>` line may grant a verdict;
    anything else is `needs_correction`.
    """
    match = _REVIEW_VERDICT_RE.match(first_line.strip().lower())
    if match is None:
        return "needs_correction"
    return match.group(1)


def create_agent_task(
    db: Session,
    *,
    title: str,
    description: str,
    acceptance_criteria: str,
    created_by: str,
    target_files: list[str] | None = None,
    constraints: str | None = None,
    required_tests: str | None = None,
    source_note_id: uuid.UUID | None = None,
) -> AgentTask:
    """Creates one scoped work order. Always meant to originate from a real ProjectNote
    (blocker/next_step) — `source_note_id` is optional only so ad-hoc founder-initiated tasks
    remain possible, not because citation stops mattering (CLAUDE.md's grundprincip)."""
    task = AgentTask(
        title=title,
        description=description,
        target_files=target_files or [],
        constraints=constraints,
        acceptance_criteria=acceptance_criteria,
        required_tests=required_tests,
        source_note_id=source_note_id,
        created_by=created_by,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def get_task(db: Session, task_id: uuid.UUID) -> AgentTask:
    task = db.get(AgentTask, task_id)
    if task is None:
        raise ValueError(f"Ingen AgentTask med id={task_id}")
    return task


def list_tasks(db: Session, *, status: AgentTaskStatus | None = None) -> list[AgentTask]:
    stmt = select(AgentTask).order_by(AgentTask.created_at.desc())
    if status is not None:
        stmt = stmt.where(AgentTask.status == status)
    return list(db.execute(stmt).scalars())


def list_task_events(db: Session, task_id: uuid.UUID) -> list[AgentTaskEvent]:
    stmt = select(AgentTaskEvent).where(AgentTaskEvent.task_id == task_id).order_by(AgentTaskEvent.created_at.asc())
    return list(db.execute(stmt).scalars())


def _record_event(
    db: Session,
    task: AgentTask,
    *,
    event_type: AgentTaskEventType,
    created_by: str,
    role: AgentRole | None = None,
    provider: str | None = None,
    model: str | None = None,
    payload: dict | None = None,
    new_status: AgentTaskStatus | None = None,
) -> AgentTaskEvent:
    event = AgentTaskEvent(
        task_id=task.id,
        event_type=event_type,
        role=role,
        provider=provider,
        model=model,
        payload=payload or {},
        created_by=created_by,
    )
    db.add(event)
    if new_status is not None:
        task.status = new_status
        task.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(event)
    return event


def _build_task_prompt(task: AgentTask) -> str:
    lines = [
        f"# Uppdrag: {task.title}",
        "",
        task.description,
        "",
        f"## Berörda filer\n{', '.join(task.target_files) if task.target_files else '(inga specificerade)'}",
        "",
        f"## Får INTE ändras\n{task.constraints or '(inget specificerat)'}",
        "",
        f"## Acceptanskriterier\n{task.acceptance_criteria}",
        "",
        f"## Krävda tester\n{task.required_tests or '(inga specificerade)'}",
    ]
    return "\n".join(lines)


async def dispatch_task(db: Session, task_id: uuid.UUID, *, dispatched_by: str) -> AgentTaskEvent:
    """Sends the task to the code agent (a chat_with_fallback() call, same provider chain as
    ordinary chat) and records the raw response as an event. Does not parse/apply the diff —
    that is a deliberately separate, later increment (see module docstring's scope note); this
    slice proves the dispatch -> record -> review -> propose chain end to end."""
    task = get_task(db, task_id)
    messages = [Message(role="system", content=CODE_AGENT_SYSTEM_PROMPT), Message(role="user", content=_build_task_prompt(task))]
    result, attempted = await chat_with_fallback(db, messages)
    return _record_event(
        db,
        task,
        event_type=AgentTaskEventType.dispatched,
        role=AgentRole.code,
        provider=result.provider,
        model=result.model,
        payload={"response": result.content, "providers_attempted": attempted},
        created_by=dispatched_by,
        new_status=AgentTaskStatus.result_recorded,
    )


def record_test_results(db: Session, task_id: uuid.UUID, *, passed: bool, output: str, recorded_by: str) -> AgentTaskEvent:
    """MainAI may never approve its own or an agent's work without verifiable test results
    (CLAUDE.md section 6) — this is the event review_task() requires exist before it will
    record an `approved` verdict."""
    task = get_task(db, task_id)
    return _record_event(
        db,
        task,
        event_type=AgentTaskEventType.test_results_recorded,
        payload={"passed": passed, "output": output[:20000]},
        created_by=recorded_by,
    )


def _latest_dispatch_response(db: Session, task_id: uuid.UUID) -> str | None:
    for event in reversed(list_task_events(db, task_id)):
        if event.event_type == AgentTaskEventType.dispatched:
            return event.payload.get("response")
    return None


def _has_recorded_tests(db: Session, task_id: uuid.UUID) -> bool:
    return any(e.event_type == AgentTaskEventType.test_results_recorded for e in list_task_events(db, task_id))


def _latest_test_result_passed(db: Session, task_id: uuid.UUID) -> bool | None:
    for event in reversed(list_task_events(db, task_id)):
        if event.event_type == AgentTaskEventType.test_results_recorded:
            return bool(event.payload.get("passed"))
    return None


async def review_task(db: Session, task_id: uuid.UUID, *, reviewed_by: str) -> AgentTaskEvent:
    """The review agent's verdict is BLOCKED (raises) if no test results have been recorded
    yet — MainAI may not fabricate or skip verification (CLAUDE.md section 6's explicit
    prohibition). The verdict itself always comes from the provider's own answer, parsed from
    a fixed 'VERDICT: <x>' first line — never inferred or defaulted to approved."""
    task = get_task(db, task_id)
    if not _has_recorded_tests(db, task_id):
        raise ValueError("Kan inte granska ett uppdrag utan registrerade testresultat (record_test_results() krävs först).")

    diff_response = _latest_dispatch_response(db, task_id) or "(inget kodagentsvar registrerat)"
    tests_passed = _latest_test_result_passed(db, task_id)

    review_prompt = (
        f"{_build_task_prompt(task)}\n\n## Kodagentens diff/svar\n{diff_response}\n\n"
        f"## Testresultat\n{'GRÖNA' if tests_passed else 'RÖDA/EJ GRÖNA'}"
    )
    messages = [Message(role="system", content=REVIEW_AGENT_SYSTEM_PROMPT), Message(role="user", content=review_prompt)]
    result, attempted = await chat_with_fallback(db, messages)

    first_line = result.content.strip().splitlines()[0].strip() if result.content.strip() else ""
    verdict = _parse_review_verdict(first_line)
    if verdict == "approved":
        new_status = AgentTaskStatus.reviewed_approved
    elif verdict == "rejected":
        new_status = AgentTaskStatus.reviewed_rejected
    else:
        new_status = AgentTaskStatus.reviewed_needs_correction

    # Never approve without green tests, regardless of what the review agent's text says —
    # a hard backstop, not just a prompt instruction (CLAUDE.md: "MainAI får inte godkänna...
    # utan verifierbara testresultat").
    if verdict == "approved" and not tests_passed:
        verdict, new_status = "needs_correction", AgentTaskStatus.reviewed_needs_correction

    return _record_event(
        db,
        task,
        event_type=AgentTaskEventType.reviewed,
        role=AgentRole.review,
        provider=result.provider,
        model=result.model,
        payload={"verdict": verdict, "findings": result.content, "providers_attempted": attempted, "tests_passed": tests_passed},
        created_by=reviewed_by,
        new_status=new_status,
    )


def _require_approved_with_tests(db: Session, task: AgentTask) -> None:
    if task.status != AgentTaskStatus.reviewed_approved:
        raise ValueError(f"Uppdraget är inte godkänt (status={task.status.value}) — kan inte förbereda en PR.")
    if _latest_test_result_passed(db, task.id) is not True:
        raise ValueError("Inga gröna testresultat registrerade — kan inte förbereda en PR.")


async def prepare_github_pr(db: Session, task_id: uuid.UUID, *, branch_name: str, base_branch: str, prepared_by: str) -> dict:
    """Builds the exact PR content (branch name, commit message, diff, PR title/body).

    If `settings.github_write_enabled` is False (the default): returns the proposal WITHOUT
    calling GitHub at all, and records a `github_pr_proposed` event — this is CLAUDE.md
    section 7's "read/analyze/propose" starting phase.

    If enabled: actually creates the branch (from `base_branch`'s current HEAD), commits the
    diff as a single file-content-API call (this slice's proof-of-capability scope — real
    multi-file/tree commits are a later increment), opens the PR, and records
    `github_branch_created`/`github_commit_pushed`/`github_pr_opened` events plus a
    ProjectBranchPRStatus row (reusing Fas 2's ingestion — CLAUDE.md's "lagra hela
    arbetskedjan i projektminnet"). Never merges — see module docstring."""
    task = get_task(db, task_id)
    _require_approved_with_tests(db, task)

    diff_response = _latest_dispatch_response(db, task_id) or ""
    pr_title = f"MainAI-uppdrag: {task.title}"
    pr_body = (
        f"Genererat av MainAI:s agentorkestrering (uppdrag {task.id}).\n\n"
        f"## Beskrivning\n{task.description}\n\n## Acceptanskriterier\n{task.acceptance_criteria}\n\n"
        f"## Granskning\nGodkänd — se AgentTaskEvent-historiken för uppdrag {task.id}."
    )

    settings = get_settings()
    if not settings.github_write_enabled:
        proposal = {"branch_name": branch_name, "base_branch": base_branch, "pr_title": pr_title, "pr_body": pr_body, "diff": diff_response}
        _record_event(
            db,
            task,
            event_type=AgentTaskEventType.github_pr_proposed,
            payload=proposal,
            created_by=prepared_by,
            new_status=AgentTaskStatus.pr_prepared,
        )
        return {"mode": "proposed", **proposal}

    client = get_github_client()
    try:
        base_sha = await client.get_ref(base_branch)
        await client.create_branch(new_branch=branch_name, from_sha=base_sha)
        _record_event(db, task, event_type=AgentTaskEventType.github_branch_created, payload={"branch": branch_name, "from_sha": base_sha}, created_by=prepared_by)

        commit_result = await client.create_or_update_file(
            path=f"docs/mainai-agent-tasks/{task.id}.diff",
            content=diff_response,
            message=f"MainAI-uppdrag {task.id}: {task.title}",
            branch=branch_name,
        )
        _record_event(db, task, event_type=AgentTaskEventType.github_commit_pushed, payload={"branch": branch_name, "commit": commit_result.get("commit", {}).get("sha")}, created_by=prepared_by)

        pr = await client.create_pull_request(title=pr_title, body=pr_body, head=branch_name, base=base_branch)
        _record_event(
            db,
            task,
            event_type=AgentTaskEventType.github_pr_opened,
            payload={"pr_number": pr.get("number"), "pr_url": pr.get("html_url")},
            created_by=prepared_by,
            new_status=AgentTaskStatus.pr_opened,
        )
        ingest_github_status(
            db,
            kind="pr",
            ref=f"#{pr.get('number')}",
            status="open",
            title=pr_title,
            base_ref=base_branch,
            head_ref=branch_name,
            ingested_by=prepared_by,
        )
        return {"mode": "created", "pr_number": pr.get("number"), "pr_url": pr.get("html_url")}
    except GitHubClientError as exc:
        _record_event(db, task, event_type=AgentTaskEventType.merge_blocked, payload={"reason": str(exc)}, created_by=prepared_by)
        raise


_AUTO_MERGE_GATE_CONDITIONS = [
    "grundaren har uttryckligen aktiverat auto-merge för aktuell riskklass",
    "ändringen ligger inom ett förhandsgodkänt scope",
    "full CI är grön",
    "migrationskontroller är gröna",
    "MainAI:s separata granskningssteg är godkänt",
    "inga olösta säkerhets-, arkitektur- eller produktkonflikter finns",
    "branch protection och GitHub-regler respekteras",
    "rollbackväg är dokumenterad",
]


def attempt_auto_merge(db: Session, task_id: uuid.UUID, *, requested_by: str) -> dict:
    """ALWAYS blocked in this slice — not a check that might someday pass by accident, but a
    genuine absence: app/integrations/github_client.py has no merge method to call, regardless
    of `settings.github_auto_merge_enabled`. Records a `merge_blocked` event every time,
    listing every one of CLAUDE.md section 7's required conditions as unmet, so the founder
    (or a future implementation) has an explicit, auditable list of what would need to be true
    before a real merge capability could even be considered."""
    task = get_task(db, task_id)
    reason = (
        "Auto-merge är inte implementerat i denna version (app/integrations/github_client.py "
        "har medvetet ingen merge-metod). Samtliga villkor nedan måste vara uppfyllda OCH en "
        "faktisk merge-implementation läggas till i en separat, granskad ändring innan detta "
        "kan bli något annat än blockerat: " + "; ".join(_AUTO_MERGE_GATE_CONDITIONS) + "."
    )
    _record_event(db, task, event_type=AgentTaskEventType.merge_blocked, payload={"reason": reason, "conditions": _AUTO_MERGE_GATE_CONDITIONS}, created_by=requested_by)
    return {"merged": False, "reason": reason, "conditions": _AUTO_MERGE_GATE_CONDITIONS}
