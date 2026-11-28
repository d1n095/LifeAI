"""P0b / A1 -- shadow observation of a REAL external agent.

The bootstrap strategy (docs/MAINAI_BOOTSTRAP_STRATEGY.md, A1) asks for the shortest honest
step before any dispatch: READ the real state of an external agent, distinguish fresh from
unknown values, and decide WAIT / FOLLOW_UP / INVESTIGATE without ever acting. This module is
that step. It reuses, and adds nothing beside, the pieces that already exist:

- `app.agent_coordination.runtime_view` -- who is registered, what each agent is assigned and
  what that assignment's canonical status is right now (read-only, recomputed per call);
- `app.agent_coordination.routing.next_feasible_assignment_for_agent` -- FIFO selection of an
  agent's OWN ready work, used here only to PREPARE a proposal, never to dispatch;
- `agent_work_assignment_events` -- the existing append-only log, used as the durable
  acknowledgement record for an ingested report.

Three deliberate properties, because agent reports are untrusted input:

1. A report is DATA, never an instruction and never an authority. Nothing here transitions an
   assignment's status, acquires a lease, calls an adapter, or spends provider budget. An agent
   that reports "KLAR" does not make its work complete -- completion still comes from the
   existing verification chain.
2. Unknown is said out loud. A missing report, a stale report, an unparseable file or an
   oversized file all produce `UNKNOWN`/`STALE` and a decision to investigate; none of them are
   silently read as "idle" (which would invite a double dispatch) or as "done".
3. The report channel is a plain directory of small JSON files. It is the shortest channel that
   needs no credential, no session token and no network, which is exactly what the strategy's
   "no UI automation to bypass session or subscription limits" rule requires. A future adapter
   may replace the transport; the schema and the decisions below do not change.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_coordination.routing import (
    OUTCOME_ASSIGNMENT_FOUND,
    next_feasible_assignment_for_agent,
)
from app.agent_coordination.runtime_view import all_agents_runtime_snapshot
from app.models.agent_coordination import (
    AgentWorkAssignment,
    AgentWorkAssignmentEvent,
    AgentWorkAssignmentEventType,
    CoordinationAgent,
)

# The four words the strategy itself uses for agent states (docs/MAINAI_AGENT_HANDOFF_2026-11-28.md):
# ARBETAR = do not interrupt, BLOCKERAD = name the concrete obstacle, KLAR = verify before a new
# task, UNKNOWN/STALE = investigate, never double-dispatch.
STATUS_WORKING = "ARBETAR"
STATUS_BLOCKED = "BLOCKERAD"
STATUS_DONE = "KLAR"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_STALE = "STALE"

REPORTED_STATUSES = frozenset({STATUS_WORKING, STATUS_BLOCKED, STATUS_DONE})

# What Life should DO about the observation -- one deterministic decision per agent.
DECISION_WAIT = "WAIT"  # keep out of the way; nothing to acknowledge yet
DECISION_ACKNOWLEDGE_AND_PREPARE_NEXT = "ACKNOWLEDGE_AND_PREPARE_NEXT"
DECISION_INVESTIGATE = "INVESTIGATE"  # unknown/stale: look before assigning anything

REPORT_KEYS = frozenset(
    {
        "agent_key",
        "report_id",
        "status",
        "assignment_ref",
        "summary",
        "output_refs",
        "reported_at",
    }
)

MAX_REPORT_BYTES = 64_000
MAX_REPORTS_PER_READ = 50
MAX_SUMMARY_CHARS = 2_000
DEFAULT_FRESH_SECONDS = 1_800  # 30 minutes: an older report cannot vouch for "working now"
MAX_FUTURE_SKEW_SECONDS = 300


class AgentReportInvalidError(RuntimeError):
    """A report that does not meet the schema exactly. Never partially trusted, never repaired:
    a malformed report is rejected and surfaced as evidence of an invalid channel, so a broken
    agent cannot quietly look healthy."""


@dataclass(frozen=True)
class ExternalAgentReport:
    """One validated report from an external agent. Pointers only where work is concerned --
    `output_refs` names branches/commits/PRs rather than carrying diffs this layer would have to
    trust blindly, matching `adapters.AgentResult`'s own contract."""

    agent_key: str
    report_id: str
    reported_status: str
    summary: str
    reported_at: datetime
    source_path: str
    sha256: str
    assignment_ref: str | None = None
    output_refs: tuple[dict, ...] = ()


@dataclass(frozen=True)
class ShadowObservation:
    """One agent's observed state and the single decision that follows from it."""

    agent_key: str
    status: str
    decision: str
    reason: str
    runtime_status: str
    open_assignment_ids: tuple[str, ...] = ()
    report_age_seconds: int | None = None
    report_sha256: str | None = None
    assignment_ref: str | None = None


@dataclass(frozen=True)
class ShadowCycle:
    """The full read of the channel: every observation plus the reports that were rejected."""

    observations: tuple[ShadowObservation, ...]
    rejected_reports: tuple[dict, ...] = field(default_factory=tuple)


def _bounded_text(value, label: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise AgentReportInvalidError(f"{label} must be a string")
    if not value.strip() or len(value) > maximum:
        raise AgentReportInvalidError(f"{label} must be 1..{maximum} characters")
    if any(not ch.isprintable() and ch not in "\n\t" for ch in value):
        raise AgentReportInvalidError(f"{label} contains non-printable characters")
    return value


def parse_agent_report(
    payload, *, source_path: str, sha256: str
) -> ExternalAgentReport:
    """Strict, total validation of one report. Exact keys, bounded strings, no guessing."""

    if not isinstance(payload, dict):
        raise AgentReportInvalidError("report must be a JSON object")
    keys = set(payload)
    if keys != REPORT_KEYS:
        missing = sorted(REPORT_KEYS - keys)
        extra = sorted(keys - REPORT_KEYS)
        raise AgentReportInvalidError(
            f"report keys must match exactly; missing={missing} unexpected={extra}"
        )
    agent_key = _bounded_text(payload["agent_key"], "agent_key", maximum=64)
    report_id = _bounded_text(payload["report_id"], "report_id", maximum=128)
    status = payload["status"]
    if status not in REPORTED_STATUSES:
        raise AgentReportInvalidError(
            f"status must be one of {sorted(REPORTED_STATUSES)}, got {status!r}"
        )
    assignment_ref = payload["assignment_ref"]
    if assignment_ref is not None:
        assignment_ref = _bounded_text(assignment_ref, "assignment_ref", maximum=128)
    summary = _bounded_text(payload["summary"], "summary", maximum=MAX_SUMMARY_CHARS)
    raw_refs = payload["output_refs"]
    if not isinstance(raw_refs, list) or len(raw_refs) > 20:
        raise AgentReportInvalidError("output_refs must be a list of at most 20 entries")
    output_refs = []
    for index, item in enumerate(raw_refs):
        if not isinstance(item, dict) or not item:
            raise AgentReportInvalidError(f"output_refs[{index}] must be a non-empty object")
        for key, value in item.items():
            _bounded_text(key, f"output_refs[{index}] key", maximum=64)
            _bounded_text(value, f"output_refs[{index}].{key}", maximum=512)
        output_refs.append(dict(item))
    reported_at = payload["reported_at"]
    if not isinstance(reported_at, str) or len(reported_at) > 40:
        raise AgentReportInvalidError("reported_at must be an ISO 8601 string")
    try:
        parsed_at = datetime.fromisoformat(reported_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AgentReportInvalidError("reported_at must be an ISO 8601 datetime") from exc
    if parsed_at.tzinfo is None:
        parsed_at = parsed_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if parsed_at - now > timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
        raise AgentReportInvalidError("reported_at is in the future beyond clock skew")
    return ExternalAgentReport(
        agent_key=agent_key,
        report_id=report_id,
        reported_status=status,
        summary=summary,
        reported_at=parsed_at,
        source_path=source_path,
        sha256=sha256,
        assignment_ref=assignment_ref,
        output_refs=tuple(output_refs),
    )


def read_report_files(
    directory: str | Path,
) -> tuple[tuple[ExternalAgentReport, ...], tuple[dict, ...]]:
    """Read every `*.json` report in one directory. Deterministic, bounded, never partial.

    A missing directory is an empty channel (not an error -- an agent simply has not written
    anything); a file that is too large, unreadable or malformed is reported in the reject list
    with its reason, because a broken report is itself information.
    """

    root = Path(directory)
    reports: list[ExternalAgentReport] = []
    rejected: list[dict] = []
    if not root.is_dir():
        return (), ()
    candidates = sorted(path for path in root.glob("*.json") if path.is_file())
    for path in candidates[:MAX_REPORTS_PER_READ]:
        try:
            size = path.stat().st_size
            if size > MAX_REPORT_BYTES:
                rejected.append({"path": str(path), "reason": "report exceeds byte bound"})
                continue
            raw = path.read_bytes()
        except OSError as exc:
            rejected.append({"path": str(path), "reason": f"unreadable: {exc}"})
            continue
        digest = hashlib.sha256(raw).hexdigest()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            rejected.append({"path": str(path), "reason": "report is not valid JSON"})
            continue
        try:
            reports.append(parse_agent_report(payload, source_path=str(path), sha256=digest))
        except AgentReportInvalidError as exc:
            rejected.append({"path": str(path), "reason": str(exc)})
    if len(candidates) > MAX_REPORTS_PER_READ:
        rejected.append(
            {
                "path": str(root),
                "reason": f"only the first {MAX_REPORTS_PER_READ} reports were read",
            }
        )
    return tuple(reports), tuple(rejected)


def _latest_report_per_agent(
    reports: tuple[ExternalAgentReport, ...],
) -> dict[str, ExternalAgentReport]:
    latest: dict[str, ExternalAgentReport] = {}
    for report in reports:
        current = latest.get(report.agent_key)
        if current is None or report.reported_at > current.reported_at:
            latest[report.agent_key] = report
    return latest


def observe_agents(
    db: Session,
    *,
    owner_id: uuid.UUID,
    report_dir: str | Path | None = None,
    reports: tuple[ExternalAgentReport, ...] | None = None,
    now: datetime | None = None,
    fresh_for_seconds: int = DEFAULT_FRESH_SECONDS,
) -> ShadowCycle:
    """Combine the durable runtime view with the report channel into one decision per agent.

    Read-only by construction: this function issues SELECTs only. Freshness is the ONLY thing
    that lets a report speak for the present; a report older than `fresh_for_seconds` is STALE
    even if it says ARBETAR, because "was working half an hour ago" is not "is working now".
    """

    if reports is None:
        reports, rejected = read_report_files(report_dir) if report_dir else ((), ())
    else:
        rejected = ()
    reference_now = now or datetime.now(timezone.utc)
    latest = _latest_report_per_agent(reports)
    views = {view.agent_key: view for view in all_agents_runtime_snapshot(db, owner_id=owner_id)}
    agent_keys = sorted(set(views) | set(latest))

    observations: list[ShadowObservation] = []
    for agent_key in agent_keys:
        view = views.get(agent_key)
        open_ids = (
            tuple(str(item.assignment_id) for item in view.current_assignments) if view else ()
        )
        runtime_status = view.runtime_status.value if view else STATUS_UNKNOWN
        report = latest.get(agent_key)
        if report is None:
            if open_ids:
                observations.append(
                    ShadowObservation(
                        agent_key=agent_key,
                        status=STATUS_UNKNOWN,
                        decision=DECISION_WAIT,
                        reason=(
                            "no report, but work is open for this agent -- assume nothing and "
                            "dispatch nothing: an open assignment must not receive a second one"
                        ),
                        runtime_status=runtime_status,
                        open_assignment_ids=open_ids,
                    )
                )
            else:
                observations.append(
                    ShadowObservation(
                        agent_key=agent_key,
                        status=STATUS_UNKNOWN,
                        decision=DECISION_INVESTIGATE,
                        reason="no report and no open work: status is simply unknown",
                        runtime_status=runtime_status,
                        open_assignment_ids=open_ids,
                    )
                )
            continue

        age_seconds = max(0, int((reference_now - report.reported_at).total_seconds()))
        if age_seconds > fresh_for_seconds:
            observations.append(
                ShadowObservation(
                    agent_key=agent_key,
                    status=STATUS_STALE,
                    decision=DECISION_INVESTIGATE,
                    reason=(
                        f"latest report is {age_seconds}s old (freshness bound "
                        f"{fresh_for_seconds}s); it cannot vouch for the current state"
                    ),
                    runtime_status=runtime_status,
                    open_assignment_ids=open_ids,
                    report_age_seconds=age_seconds,
                    report_sha256=report.sha256,
                    assignment_ref=report.assignment_ref,
                )
            )
            continue

        if report.reported_status == STATUS_WORKING:
            decision, reason = (
                DECISION_WAIT,
                "agent reports ARBETAR (fresh): stay out of the way, no dispatch",
            )
        elif report.reported_status == STATUS_BLOCKED:
            decision, reason = (
                DECISION_ACKNOWLEDGE_AND_PREPARE_NEXT,
                "agent reports BLOCKERAD (fresh): acknowledge, name the obstacle, prepare the "
                "next approved task without dispatching it",
            )
        else:
            decision, reason = (
                DECISION_ACKNOWLEDGE_AND_PREPARE_NEXT,
                "agent reports KLAR (fresh): acknowledge, then verify the delivery before any "
                "new task",
            )
        observations.append(
            ShadowObservation(
                agent_key=agent_key,
                status=report.reported_status,
                decision=decision,
                reason=reason,
                runtime_status=runtime_status,
                open_assignment_ids=open_ids,
                report_age_seconds=age_seconds,
                report_sha256=report.sha256,
                assignment_ref=report.assignment_ref,
            )
        )
    return ShadowCycle(observations=tuple(observations), rejected_reports=rejected)


def _assignment_for_report(
    db: Session, *, owner_id: uuid.UUID, report: ExternalAgentReport
) -> AgentWorkAssignment:
    """Resolve the assignment a report names, inside the owner's own scope, or reject it."""

    if not report.assignment_ref:
        raise AgentReportInvalidError("report names no assignment_ref")
    try:
        assignment_id = uuid.UUID(report.assignment_ref)
    except ValueError as exc:
        raise AgentReportInvalidError("assignment_ref is not a UUID") from exc
    assignment = db.execute(
        select(AgentWorkAssignment).where(
            AgentWorkAssignment.id == assignment_id,
            AgentWorkAssignment.owner_id == owner_id,
        )
    ).scalar_one_or_none()
    if assignment is None:
        raise AgentReportInvalidError("assignment_ref does not resolve in this owner's scope")
    agent = db.execute(
        select(CoordinationAgent).where(CoordinationAgent.id == assignment.agent_id)
    ).scalar_one()
    if agent.agent_key != report.agent_key:
        raise AgentReportInvalidError(
            "report agent_key does not own the referenced assignment"
        )
    return assignment


def acknowledge_report(
    db: Session, *, owner_id: uuid.UUID, report: ExternalAgentReport
) -> tuple[uuid.UUID, bool]:
    """Record that a report was received, exactly once, in the existing event log.

    Idempotent on (assignment, report_id, sha256): replaying the same report returns the
    original acknowledgement instead of appending a second one, so a retrying agent cannot
    inflate the audit trail. Returns ``(event_id, created)``.

    This is the whole of "kvittera resultat" at A1: the report becomes durable, attributable
    evidence. It does NOT complete the assignment, record a pass, or release a lease -- those
    remain the existing verification chain's decisions.
    """

    assignment = _assignment_for_report(db, owner_id=owner_id, report=report)
    existing_events = (
        db.execute(
            select(AgentWorkAssignmentEvent).where(
                AgentWorkAssignmentEvent.assignment_id == assignment.id,
                AgentWorkAssignmentEvent.owner_id == owner_id,
                AgentWorkAssignmentEvent.event_type
                == AgentWorkAssignmentEventType.execution_observed,
            )
        )
        .scalars()
        .all()
    )
    for event in existing_events:
        detail = event.detail or {}
        if detail.get("report_id") == report.report_id and detail.get(
            "report_sha256"
        ) == report.sha256:
            return event.id, False
    event = AgentWorkAssignmentEvent(
        assignment_id=assignment.id,
        owner_id=owner_id,
        event_type=AgentWorkAssignmentEventType.execution_observed,
        detail={
            "report_id": report.report_id,
            "report_sha256": report.sha256,
            "source": "external_agent_report",
            "source_path": report.source_path,
            "reported_status": report.reported_status,
            "summary": report.summary,
            "output_refs": [dict(ref) for ref in report.output_refs],
            "reported_at": report.reported_at.isoformat(),
            "trigger": "agent_coordination.shadow_observation.acknowledge_report",
        },
    )
    db.add(event)
    db.flush()
    return event.id, True


@dataclass(frozen=True)
class NextAssignmentProposal:
    """The next approved task, PREPARED but never dispatched."""

    agent_key: str
    outcome: str
    assignment_id: str | None
    reason: str


def propose_next_assignment(
    db: Session, *, owner_id: uuid.UUID, agent_key: str
) -> NextAssignmentProposal:
    """Ask the existing FIFO routing which of this agent's OWN ready assignments is next.

    No dispatch, no lease, no status change: the caller still has to pass
    `dispatch.evaluate_dispatch_readiness()` immediately before invoking anything, which is the
    unchanged fail-closed gate. A proposal is a proposal.
    """

    agent = db.execute(
        select(CoordinationAgent).where(CoordinationAgent.agent_key == agent_key)
    ).scalar_one_or_none()
    if agent is None:
        return NextAssignmentProposal(
            agent_key=agent_key,
            outcome="AGENT_NOT_REGISTERED",
            assignment_id=None,
            reason="no registered agent with this key; nothing to prepare",
        )
    decision = next_feasible_assignment_for_agent(db, agent_id=agent.id, owner_id=owner_id)
    chosen = getattr(decision, "assignment_id", None)
    return NextAssignmentProposal(
        agent_key=agent_key,
        outcome=decision.outcome,
        assignment_id=str(chosen) if chosen else None,
        reason=decision.reason,
    )


def shadow_cycle(
    db: Session,
    *,
    owner_id: uuid.UUID,
    report_dir: str | Path | None = None,
    acknowledge: bool = False,
    now: datetime | None = None,
    fresh_for_seconds: int = DEFAULT_FRESH_SECONDS,
) -> tuple[ShadowCycle, tuple[dict, ...]]:
    """One A1 pass: observe every agent, and (only when explicitly asked) acknowledge reports
    and prepare the next approved task. Dispatch remains impossible from here.

    ``acknowledge=False`` is the pure shadow mode the strategy asks to start with: SELECT only,
    no durable write at all. ``acknowledge=True`` adds the idempotent event append and the
    read-only proposal; it still cannot start, steer or cancel an agent.
    """

    cycle = observe_agents(
        db, owner_id=owner_id, report_dir=report_dir, now=now, fresh_for_seconds=fresh_for_seconds
    )
    results: list[dict] = [
        {"path": item["path"], "rejected": item["reason"]}
        for item in cycle.rejected_reports
    ]
    if not acknowledge:
        return cycle, tuple(results)
    reports, _rejected = (
        read_report_files(report_dir) if report_dir else ((), ())
    )
    latest = _latest_report_per_agent(reports)
    for observation in cycle.observations:
        if observation.decision != DECISION_ACKNOWLEDGE_AND_PREPARE_NEXT:
            continue
        report = latest.get(observation.agent_key)
        if report is None:
            continue
        try:
            event_id, created = acknowledge_report(db, owner_id=owner_id, report=report)
        except AgentReportInvalidError as exc:
            results.append(
                {"agent_key": observation.agent_key, "acknowledged": False, "reason": str(exc)}
            )
            continue
        proposal = propose_next_assignment(
            db, owner_id=owner_id, agent_key=observation.agent_key
        )
        results.append(
            {
                "agent_key": observation.agent_key,
                "acknowledged": True,
                "created": created,
                "event_id": str(event_id),
                "next_outcome": proposal.outcome,
                "next_assignment_id": proposal.assignment_id,
                "dispatch": "NOT_PERFORMED",
                "matches_routing": proposal.outcome == OUTCOME_ASSIGNMENT_FOUND,
            }
        )
    return cycle, tuple(results)
