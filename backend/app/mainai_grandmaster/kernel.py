"""Authoritative board refresh, invariant validation, leases, and event ingestion."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Iterable, Protocol

from app.mainai_grandmaster.types import (
    Board,
    BoardStatus,
    EvidencePointer,
    Execution,
    PlannedMove,
    ProposedMove,
    ReportEvent,
    ResourceLease,
    ResultType,
    WorkspaceMode,
    utc_now,
)


_FORBIDDEN_AUTONOMOUS_ACTIONS = frozenset({"merge", "deploy", "activate_recall", "spend", "purchase", "override_security", "mutate_founder_policy"})
_CRITICAL_FIELDS = ("agent", "task", "execution", "workspace")


class AuthoritativeBoardSource(Protocol):
    """Adapter boundary for GitHub, CI, filesystem, DB, and execution ledgers."""

    def snapshot(self) -> Board: ...


class GrandmasterKernel:
    """Validates plans against a freshly refreshed board; never executes a move."""

    def __init__(self, board: Board | None = None) -> None:
        self.board = board or Board()
        self._lock = threading.RLock()

    def refresh(self, snapshots: Iterable[Board], *, now: datetime | None = None) -> Board:
        """Merge authoritative snapshots, newest observation wins per identity.

        Chat reports remain provenance only. They can help locate an entity but do not
        overwrite authoritative mutable state.
        """
        at = now or utc_now()
        merged = Board(refreshed_at=at)
        for snapshot in snapshots:
            self._merge_map(merged.agents, snapshot.agents, at)
            self._merge_map(merged.sessions, snapshot.sessions, at)
            self._merge_map(merged.tasks, snapshot.tasks, at, allow_missing_evidence=True)
            self._merge_map(merged.executions, snapshot.executions, at)
            self._merge_map(merged.workspaces, snapshot.workspaces, at)
            self._merge_map(merged.branches, snapshot.branches, at)
            self._merge_map(merged.candidates, snapshot.candidates, at)
            self._merge_map(merged.examinations, snapshot.examinations, at)
            merged.dependencies.extend(item for item in snapshot.dependencies if item.evidence.fresh(at))
            merged.blockers.extend(item for item in snapshot.blockers if item.evidence.fresh(at))
            merged.reports.extend(snapshot.reports)
        with self._lock:
            # Leases are kernel-owned state and survive a source refresh.
            merged.leases = dict(self.board.leases)
            self.board = merged
            self.expire_leases(now=at)
            return self.board.copy()

    def refresh_from_sources(
        self, sources: Iterable[AuthoritativeBoardSource], *, now: datetime | None = None
    ) -> Board:
        """Pull current machine state so the Founder is never required to relay reports."""
        return self.refresh((source.snapshot() for source in sources), now=now)

    @staticmethod
    def _merge_map(target: dict, source: dict, now: datetime, *, allow_missing_evidence: bool = False) -> None:
        for key, item in source.items():
            evidence = getattr(item, "evidence", None)
            if evidence is None and allow_missing_evidence:
                continue
            if evidence is None or not evidence.fresh(now):
                continue
            current = target.get(key)
            current_evidence = getattr(current, "evidence", None)
            if current is None or current_evidence.observed_at < evidence.observed_at:
                target[key] = item

    def validate_move(self, move: ProposedMove, *, now: datetime | None = None) -> PlannedMove:
        at = now or utc_now()
        reasons: list[str] = []
        if self.board.refreshed_at is None:
            return PlannedMove(move, False, "NEEDS_STATE_REFRESH", ("board_not_refreshed",))

        agent = self.board.agents.get(move.agent_key)
        task = self.board.tasks.get(move.task_key)
        workspace = self.board.workspaces.get(move.workspace_key)
        execution = self.board.executions.get(move.execution_id)
        missing = [name for name, value in zip(_CRITICAL_FIELDS, (agent, task, execution, workspace), strict=True) if value is None]
        if missing:
            return PlannedMove(move, False, "NEEDS_STATE_REFRESH", tuple(f"unknown_{item}" for item in missing))

        stale_facts = [
            name
            for name, item in zip(_CRITICAL_FIELDS, (agent, task, execution, workspace), strict=True)
            if getattr(item, "evidence", None) is None or not item.evidence.fresh(at)
        ]
        if stale_facts:
            return PlannedMove(move, False, "NEEDS_STATE_REFRESH", tuple(f"stale_{item}" for item in stale_facts))

        if move.action in _FORBIDDEN_AUTONOMOUS_ACTIONS:
            reasons.append("action_requires_external_governed_authority")
        if agent.status not in {BoardStatus.IDLE, BoardStatus.RESERVED}:
            reasons.append("agent_not_available")
        if any(e.agent_key == move.agent_key and e.status in {BoardStatus.RUNNING, BoardStatus.RESERVED} and e.execution_id != move.execution_id for e in self.board.executions.values()):
            reasons.append("agent_occupied")
        if any(e.task_key == move.task_key and e.status in {BoardStatus.RUNNING, BoardStatus.RESERVED, BoardStatus.UNDER_EXAMINATION} and e.execution_id != move.execution_id for e in self.board.executions.values()):
            reasons.append("duplicate_active_task")
        if execution.status == BoardStatus.RUNNING:
            reasons.append("execution_already_started")
        if task.status in {BoardStatus.COMPLETE_UNVERIFIED, BoardStatus.CERTIFIED}:
            reasons.append("existing_implementation_requires_state_check")
        if execution.task_key != move.task_key or execution.agent_key != move.agent_key:
            reasons.append("execution_binding_mismatch")
        if workspace.mode != WorkspaceMode.MUTABLE:
            reasons.append("workspace_not_mutable")
        if workspace.owner_agent not in {None, move.agent_key}:
            reasons.append("workspace_owned_by_other_agent")
        if workspace.execution_id not in {None, move.execution_id}:
            reasons.append("workspace_execution_mismatch")
        if workspace.branch not in {None, move.branch}:
            reasons.append("workspace_branch_mismatch")
        branch = self.board.branches.get(move.branch)
        if branch is not None and branch.owner_agent not in {None, move.agent_key}:
            reasons.append("branch_owned_by_other_agent")
        if any(
            e.execution_id != move.execution_id
            and e.branch == move.branch
            and e.status in {BoardStatus.RUNNING, BoardStatus.RESERVED, BoardStatus.UNDER_EXAMINATION}
            for e in self.board.executions.values()
        ):
            reasons.append("branch_execution_collision")
        lease = self.board.leases.get(move.workspace_key)
        if lease is not None and lease.active(at) and (lease.owner_agent != move.agent_key or lease.execution_id != move.execution_id):
            reasons.append("workspace_lease_collision")

        examined = [x for x in self.board.examinations.values() if x.status == BoardStatus.UNDER_EXAMINATION]
        if any(x.workspace_key == move.workspace_key for x in examined):
            reasons.append("examiner_workspace_is_read_only_for_builder")
        if any(self.board.candidates.get(x.candidate_sha) and self.board.candidates[x.candidate_sha].branch == move.branch for x in examined):
            reasons.append("examined_branch_is_immutable")
        frozen = self.board.candidates.get(move.base_sha)
        if frozen is not None and frozen.frozen and move.child_of_sha != move.base_sha:
            reasons.append("frozen_candidate_requires_new_child")
        if move.child_of_sha is not None and move.child_of_sha != move.base_sha:
            reasons.append("child_base_mismatch")

        if task.required_role not in agent.roles or not task.required_capabilities.issubset(agent.capabilities):
            reasons.append("agent_role_or_capability_mismatch")
        dependency_state = {d.depends_on: d.satisfied for d in self.board.dependencies if d.task_key == task.key}
        if any(dependency_state.get(key) is not True for key in task.dependencies):
            reasons.append("dependency_not_ready_or_unknown")
        if any(b.task_key == task.key and b.active for b in self.board.blockers):
            reasons.append("active_blocker")
        reasons.extend(self._migration_collisions(move, task.migration_namespaces))

        if reasons:
            return PlannedMove(move, False, "BLOCKED", tuple(dict.fromkeys(reasons)))
        return PlannedMove(move, True, "ASSIGNABLE", ())

    def _migration_collisions(self, move: ProposedMove, namespaces: frozenset[str]) -> list[str]:
        if not namespaces:
            return []
        for execution in self.board.executions.values():
            if execution.execution_id == move.execution_id or execution.status not in {BoardStatus.RUNNING, BoardStatus.RESERVED}:
                continue
            other = self.board.tasks.get(execution.task_key)
            if other is not None and namespaces.intersection(other.migration_namespaces):
                return ["migration_namespace_collision"]
        return []

    def acquire_lease(self, move: ProposedMove, *, ttl: timedelta, now: datetime | None = None) -> ResourceLease | None:
        at = now or utc_now()
        with self._lock:
            decision = self.validate_move(move, now=at)
            if not decision.legal:
                return None
            current = self.board.leases.get(move.workspace_key)
            if current is not None and current.active(at):
                return current if current.owner_agent == move.agent_key and current.execution_id == move.execution_id else None
            lease = ResourceLease(
                workspace_key=move.workspace_key, owner_agent=move.agent_key,
                execution_id=move.execution_id, branch=move.branch, mode=WorkspaceMode.MUTABLE,
                acquired_at=at, heartbeat_at=at, expires_at=at + ttl,
            )
            self.board.leases[move.workspace_key] = lease
            return lease

    def heartbeat_lease(self, workspace_key: str, *, agent_key: str, execution_id: str, ttl: timedelta, now: datetime | None = None) -> ResourceLease | None:
        at = now or utc_now()
        with self._lock:
            lease = self.board.leases.get(workspace_key)
            if lease is None or not lease.active(at) or lease.owner_agent != agent_key or lease.execution_id != execution_id:
                return None
            renewed = replace(lease, heartbeat_at=at, expires_at=at + ttl)
            self.board.leases[workspace_key] = renewed
            return renewed

    def release_lease(self, workspace_key: str, *, agent_key: str, execution_id: str, now: datetime | None = None) -> bool:
        at = now or utc_now()
        with self._lock:
            lease = self.board.leases.get(workspace_key)
            if lease is None or lease.owner_agent != agent_key or lease.execution_id != execution_id:
                return False
            self.board.leases[workspace_key] = replace(lease, released_at=at)
            return True

    def expire_leases(self, *, now: datetime | None = None) -> int:
        at = now or utc_now()
        count = 0
        with self._lock:
            for key, lease in tuple(self.board.leases.items()):
                if lease.released_at is None and lease.expires_at <= at:
                    self.board.leases[key] = replace(lease, released_at=at)
                    count += 1
        return count

    def ingest_reports(self, message_id: str, reports: Iterable[dict], *, received_at: datetime | None = None) -> tuple[ReportEvent, ...]:
        """Create one event per report. Report text is never authoritative state."""
        at = received_at or utc_now()
        events: list[ReportEvent] = []
        for index, report in enumerate(reports):
            required = {"agent_key", "task_key", "execution_id", "result_type", "status"}
            if not required.issubset(report):
                raise ValueError("each report requires agent, task, execution, result type, and status")
            event = ReportEvent(
                event_id=f"{message_id}:{index}", message_id=message_id,
                agent_key=report["agent_key"], task_key=report["task_key"],
                execution_id=report["execution_id"], branch=report.get("branch"), sha=report.get("sha"),
                result_type=ResultType(report["result_type"]), reported_status=BoardStatus(report["status"]),
                evidence=EvidencePointer("founder_message", message_id, at, authoritative=False, payload=dict(report)),
            )
            events.append(event)
        self.board.reports.extend(events)
        return tuple(events)

    def ingest_machine_execution(self, execution: Execution) -> None:
        if not execution.evidence.authoritative:
            raise ValueError("machine execution event must be authoritative")
        current = self.board.executions.get(execution.execution_id)
        if current is None or current.evidence.observed_at < execution.evidence.observed_at:
            self.board.executions[execution.execution_id] = execution

    def record_examiner_verdict(self, examination_id: str, verdict: str, evidence: EvidencePointer) -> None:
        if not evidence.fresh(utc_now()):
            raise ValueError("examiner verdict requires fresh authoritative evidence")
        examination = self.board.examinations[examination_id]
        candidate = self.board.candidates[examination.candidate_sha]
        builders = {
            e.agent_key
            for e in self.board.executions.values()
            if e.task_key == candidate.task_key and e.workspace_key != examination.workspace_key
        }
        if examination.examiner_agent in builders:
            raise ValueError("builder cannot certify its own candidate")
        if verdict == "FREEZE_SHA":
            self.board.examinations[examination_id] = replace(examination, status=BoardStatus.CERTIFIED, verdict=verdict, evidence=evidence)
            self.board.candidates[candidate.sha] = replace(candidate, status=BoardStatus.CERTIFIED, frozen=True, evidence=evidence)
        elif verdict == "RETURN_TO_BUILDER":
            self.board.examinations[examination_id] = replace(examination, status=BoardStatus.COMPLETE_UNVERIFIED, verdict=verdict, evidence=evidence)
        else:
            raise ValueError("unsupported examiner verdict")

    def remote_pushed(self, branch: str, sha: str) -> bool:
        row = self.board.branches.get(branch)
        return bool(row and row.evidence.source == "github" and row.evidence.fresh(utc_now()) and row.remote_sha == sha)

    def discover_sha(self, *, branch: str | None = None, task_key: str | None = None) -> str | None:
        """Return a machine-discoverable SHA or UNKNOWN (`None`), never request relay text."""
        if branch is not None:
            row = self.board.branches.get(branch)
            if row and row.evidence.source == "github" and row.evidence.fresh(utc_now()):
                return row.remote_sha
        if task_key is not None:
            matches = [row for row in self.board.candidates.values() if row.task_key == task_key]
            if matches:
                return max(matches, key=lambda row: row.evidence.observed_at).sha
        return None

    def certification_valid(self, candidate_sha: str, examiner_agent: str) -> bool:
        candidate = self.board.candidates.get(candidate_sha)
        if candidate is None:
            return False
        execution_builders = {e.agent_key for e in self.board.executions.values() if e.task_key == candidate.task_key}
        return examiner_agent not in execution_builders and any(
            exam.candidate_sha == candidate_sha and exam.examiner_agent == examiner_agent and exam.status == BoardStatus.CERTIFIED
            for exam in self.board.examinations.values()
        )
