"""Read-only GitHub evidence synthesis for Section B of the Continuous Build Directive
("MainAI engineering coordinator" — read verified GitHub evidence, identify open tasks,
propose a prioritized work queue).

Deliberately produces a STATELESS report, never a persisted, founder-reviewable candidate.
This is an explicit architecture decision, not an oversight — explained in full because the
obvious-looking alternative (reuse `app.work_candidates`) was investigated and rejected:

  `app.work_candidates.service` already implements exactly the "signal producer != truth
  writer" staging pattern Section B needs (`WorkCandidate.status` starts "unreviewed";
  `authorize_work_candidate()` is the ONLY path to a real `MainAIGoal`, and it always
  requires an explicit `authorized_by`). But `WorkCandidate.source_entity_id` is a composite
  FK into `project_entities`, which is itself only ever reached via
  `promote_interpretation_proposal()` from an `InterpretationProposal`, which is itself only
  ever reached from a `KnowledgeClaim` extracted from the founder's own knowledge library
  (see migrations 0054-0056 and each model's own docstring). That whole lattice is deliberately
  scoped to FOUNDER KNOWLEDGE, with owner-anchored composite FKs at every stage reviewed
  specifically for that purpose.

  A GitHub pull request, issue, or commit is live EXTERNAL SYSTEM STATE, not founder
  knowledge. Fabricating a `KnowledgeClaim`/`ProjectEntity` whose sole purpose is to satisfy
  `work_candidates`' FK so a GitHub PR can pose as "structured project understanding" would
  be exactly the kind of structurally dishonest shortcut this repo's own isolation/extension
  principle (CLAUDE.md) warns against — reusing a table's SHAPE while breaking the invariant
  that shape was built to enforce is not "extending existing infrastructure", it's quietly
  defeating it.

  Whether GitHub-evidence-derived suggestions deserve their own founder-reviewable staging
  table — a sibling to `work_candidates`, same promotion-gate pattern, different source model
  — is a real architecture decision. It is not this module's to make unilaterally: it changes
  how work enters the goal-creation pipeline, which is exactly the class of "unresolved
  authority/security decision" the Continuous Build Directive says to pause on rather than
  decide alone. Until a human (founder, or Codex for the Founder-Alpha-security-adjacent parts
  of that call) makes it, this module only turns real GitHub evidence into something a person
  can read and act on — it writes nothing, creates no goal, and claims no authority of its
  own. The eventual persisted version, if and when that decision is made, should call this
  module's `build_github_work_queue_report()` for its evidence and then run it through
  whatever new staging model is designed — not reinvent the evidence-gathering half.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.integrations.github_client import GitHubClient

DEFAULT_STALE_DAYS = 7
DEFAULT_MAX_PULL_REQUESTS = 25


@dataclass(frozen=True)
class WorkQueueItem:
    """One piece of real, checkable GitHub evidence, plus the deterministic signals computed
    from it — never a judgment call about priority or next action. Ordering/prioritization is
    left to the reader (human or, later, an explicitly-reviewed staging layer); this dataclass
    only reports facts."""

    kind: str  # "open_pull_request" | "open_issue"
    number: int
    title: str
    html_url: str
    updated_at: str
    draft: bool = False
    labels: tuple[str, ...] = ()
    signals: tuple[str, ...] = field(default_factory=tuple)


def _is_stale(updated_at: str, *, stale_days: int, now: datetime) -> bool:
    updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    return (now - updated) > timedelta(days=stale_days)


async def _pr_signals(client: GitHubClient, pr: dict, *, stale_days: int, now: datetime) -> tuple[str, ...]:
    signals: list[str] = []
    if pr.get("draft"):
        signals.append("draft")
    if _is_stale(pr["updated_at"], stale_days=stale_days, now=now):
        signals.append(f"stale_{stale_days}d")
    head_sha = pr.get("head", {}).get("sha")
    if head_sha:
        check_runs = await client.list_check_runs(head_sha)
        if any(run.get("conclusion") == "failure" for run in check_runs):
            signals.append("ci_red")
        elif check_runs and all(run.get("status") == "completed" for run in check_runs):
            signals.append("ci_green")
    if pr.get("mergeable_state") == "dirty":
        signals.append("merge_conflict")
    return tuple(signals)


async def build_github_work_queue_report(
    client: GitHubClient,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
    max_pull_requests: int = DEFAULT_MAX_PULL_REQUESTS,
    now: datetime | None = None,
) -> list[WorkQueueItem]:
    """Repo-wide, read-only evidence report: every open PR (bounded by `max_pull_requests`,
    since this calls list_check_runs per PR — see module docstring for why this is evidence
    gathering, not prioritization) plus every open issue, each annotated with deterministic
    signals (draft/stale/ci_red/ci_green/merge_conflict) computed from real GitHub state, never
    guessed. Open issues get no signals beyond their own fields — GitHub's REST API does not
    expose check-run/mergeable state for issues, so this never fabricates one.

    Raises whatever `client`'s own methods raise (e.g. `GitHubClientError` if unconfigured) —
    this function adds no new failure mode, it only composes existing read calls."""
    now = now or datetime.now(UTC)
    items: list[WorkQueueItem] = []

    prs = await client.list_pull_requests(state="open", per_page=max_pull_requests)
    for pr in prs:
        signals = await _pr_signals(client, pr, stale_days=stale_days, now=now)
        items.append(
            WorkQueueItem(
                kind="open_pull_request",
                number=pr["number"],
                title=pr["title"],
                html_url=pr["html_url"],
                updated_at=pr["updated_at"],
                draft=bool(pr.get("draft")),
                labels=tuple(label["name"] for label in pr.get("labels", [])),
                signals=signals,
            )
        )

    issues = await client.list_issues(state="open")
    for issue in issues:
        issue_signals = ()
        if _is_stale(issue["updated_at"], stale_days=stale_days, now=now):
            issue_signals = (f"stale_{stale_days}d",)
        items.append(
            WorkQueueItem(
                kind="open_issue",
                number=issue["number"],
                title=issue["title"],
                html_url=issue["html_url"],
                updated_at=issue["updated_at"],
                labels=tuple(label["name"] for label in issue.get("labels", [])),
                signals=issue_signals,
            )
        )

    return items
