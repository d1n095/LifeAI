"""Authoritative board refresh, invariant validation, leases, and event ingestion."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Iterable, Protocol

from app.mainai_grandmaster.coordination import (
    CoordinationStore,
    normalize_physical_path,
)
from app.mainai_grandmaster.types import (
    Board,
    BoardStatus,
    Clock,
    EvidencePointer,
    Execution,
    PlannedMove,
    ProposedMove,
    ReportEvent,
    ResourceLease,
    ResultType,
    SystemClock,
    WorkspaceMode,
)

_SAFE_AUTONOMOUS_ACTIONS = frozenset(
    {"build", "test", "examine", "inspect", "analyze", "document", "remediate"}
)
_TRUSTED_VERDICT_SOURCES = frozenset({"verification_registry"})
_CRITICAL_FIELDS = ("agent", "task", "execution", "workspace")


class AuthoritativeBoardSource(Protocol):
    def snapshot(self) -> Board: ...


class GrandmasterKernel:
    """Validate proposed work. It never executes a move or widens authority."""

    def __init__(
        self,
        board: Board | None = None,
        *,
        clock: Clock | None = None,
        coordination: CoordinationStore | None = None,
    ) -> None:
        self.board = board or Board()
        self.clock = clock or SystemClock()
        self.coordination = coordination
        self._lock = threading.RLock()

    def refresh(
        self, snapshots: Iterable[Board], *, now: datetime | None = None
    ) -> Board:
        at = now or self.clock.now()
        with self._lock:
            merged = Board(refreshed_at=at)
            for snapshot in snapshots:
                for target, source in (
                    (merged.agents, snapshot.agents),
                    (merged.sessions, snapshot.sessions),
                    (merged.tasks, snapshot.tasks),
                    (merged.executions, snapshot.executions),
                    (merged.workspaces, snapshot.workspaces),
                    (merged.branches, snapshot.branches),
                    (merged.candidates, snapshot.candidates),
                    (merged.examinations, snapshot.examinations),
                ):
                    self._merge_map(target, source, at)
                merged.dependencies.extend(
                    item for item in snapshot.dependencies if item.evidence.fresh(at)
                )
                merged.blockers.extend(
                    item for item in snapshot.blockers if item.evidence.fresh(at)
                )
                self._merge_reports(merged.reports, snapshot.reports)
            self.board = merged
            self.board.leases = self._active_leases(at)
            if self._dependency_cycle():
                raise ValueError("authoritative task graph contains a dependency cycle")
            return self.board.copy()

    def refresh_from_sources(
        self,
        sources: Iterable[AuthoritativeBoardSource],
        *,
        now: datetime | None = None,
    ) -> Board:
        return self.refresh((source.snapshot() for source in sources), now=now)

    @staticmethod
    def _merge_map(target: dict, source: dict, now: datetime) -> None:
        for key, item in source.items():
            evidence = getattr(item, "evidence", None)
            if evidence is None or not evidence.fresh(now):
                continue
            current = target.get(key)
            current_evidence = getattr(current, "evidence", None)
            if current is None or current_evidence.observed_at < evidence.observed_at:
                target[key] = item

    def validate_move(
        self, move: ProposedMove, *, now: datetime | None = None
    ) -> PlannedMove:
        at = now or self.clock.now()
        with self._lock:
            return self._validate_move_locked(move, at)

    def validate_moves(
        self, moves: Iterable[ProposedMove], *, now: datetime | None = None
    ) -> tuple[PlannedMove, ...]:
        """Evaluate a planning batch against one locked board generation."""
        at = now or self.clock.now()
        with self._lock:
            return tuple(self._validate_move_locked(move, at) for move in moves)

    def board_snapshot(self) -> Board:
        with self._lock:
            return self.board.copy()

    def validate_builder_child(
        self, examination_id: str, move: ProposedMove, *, now: datetime | None = None
    ) -> PlannedMove:
        at = now or self.clock.now()
        with self._lock:
            examination = self.board.examinations.get(examination_id)
            if examination is None or examination.verdict != "RETURN_TO_BUILDER":
                return PlannedMove(
                    move, False, "BLOCKED", ("return_to_builder_verdict_required",)
                )
            return self._validate_move_locked(move, at)

    def agent_is_idle(self, agent_key: str) -> bool:
        with self._lock:
            agent = self.board.agents.get(agent_key)
            return bool(agent and agent.status == BoardStatus.IDLE)

    def _validate_move_locked(self, move: ProposedMove, at: datetime) -> PlannedMove:
        reasons: list[str] = []
        if self.board.refreshed_at is None:
            return PlannedMove(
                move, False, "NEEDS_STATE_REFRESH", ("board_not_refreshed",)
            )
        agent = self.board.agents.get(move.agent_key)
        task = self.board.tasks.get(move.task_key)
        workspace = self.board.workspaces.get(move.workspace_key)
        execution = self.board.executions.get(move.execution_id)
        missing = [
            name
            for name, value in zip(
                _CRITICAL_FIELDS, (agent, task, execution, workspace), strict=True
            )
            if value is None
        ]
        if missing:
            return PlannedMove(
                move,
                False,
                "NEEDS_STATE_REFRESH",
                tuple(f"unknown_{x}" for x in missing),
            )
        stale = [
            name
            for name, item in zip(
                _CRITICAL_FIELDS, (agent, task, execution, workspace), strict=True
            )
            if getattr(item, "evidence", None) is None or not item.evidence.fresh(at)
        ]
        if stale:
            return PlannedMove(
                move, False, "NEEDS_STATE_REFRESH", tuple(f"stale_{x}" for x in stale)
            )

        if move.action not in _SAFE_AUTONOMOUS_ACTIONS:
            reasons.append("action_requires_external_governed_authority")
        if agent.status not in {BoardStatus.IDLE, BoardStatus.RESERVED}:
            reasons.append("agent_not_available")
        if any(
            row.agent_key == move.agent_key
            and row.status in {BoardStatus.RUNNING, BoardStatus.RESERVED}
            and row.execution_id != move.execution_id
            for row in self.board.executions.values()
        ):
            reasons.append("agent_occupied")
        if any(
            row.task_key == move.task_key
            and row.status
            in {
                BoardStatus.RUNNING,
                BoardStatus.RESERVED,
                BoardStatus.UNDER_EXAMINATION,
            }
            and row.execution_id != move.execution_id
            for row in self.board.executions.values()
        ):
            reasons.append("duplicate_active_task")
        if execution.status in {BoardStatus.RUNNING, BoardStatus.RESERVED}:
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
            row.execution_id != move.execution_id
            and row.branch == move.branch
            and row.status
            in {
                BoardStatus.RUNNING,
                BoardStatus.RESERVED,
                BoardStatus.UNDER_EXAMINATION,
            }
            for row in self.board.executions.values()
        ):
            reasons.append("branch_execution_collision")

        move_path = normalize_physical_path(workspace.path)
        for key, other in self.board.workspaces.items():
            if (
                key == move.workspace_key
                or normalize_physical_path(other.path) != move_path
            ):
                continue
            if (
                other.owner_agent not in {None, move.agent_key}
                or other.execution_id not in {None, move.execution_id}
                or other.branch not in {None, move.branch}
            ):
                reasons.append("physical_workspace_path_collision")
        for lease in self._active_leases(at).values():
            if (
                lease.owner_agent == move.agent_key
                and lease.execution_id != move.execution_id
            ):
                reasons.append("agent_active_lease_collision")
            if (
                lease.task_key == move.task_key
                and lease.execution_id != move.execution_id
            ):
                reasons.append("task_active_lease_collision")
            if (
                lease.workspace_path == move_path
                and lease.execution_id != move.execution_id
            ):
                reasons.append("workspace_lease_collision")

        protected = [
            x
            for x in self.board.examinations.values()
            if x.status in {BoardStatus.RESERVED, BoardStatus.UNDER_EXAMINATION}
        ]
        if any(
            x.workspace_key == move.workspace_key
            or (
                self.board.workspaces.get(x.workspace_key) is not None
                and normalize_physical_path(self.board.workspaces[x.workspace_key].path)
                == move_path
            )
            for x in protected
        ):
            reasons.append("examiner_workspace_is_read_only_for_builder")
        if any(
            self.board.candidates.get(x.candidate_sha)
            and self.board.candidates[x.candidate_sha].branch == move.branch
            for x in protected
        ):
            reasons.append("examined_branch_is_immutable")
        frozen = self.board.candidates.get(move.base_sha)
        if frozen is not None and frozen.frozen and move.child_of_sha != move.base_sha:
            reasons.append("frozen_candidate_requires_new_child")
        if any(
            x.frozen and x.branch == move.branch for x in self.board.candidates.values()
        ):
            reasons.append("frozen_candidate_branch_is_immutable")
        if move.child_of_sha is not None and move.child_of_sha != move.base_sha:
            reasons.append("child_base_mismatch")
        for examination in self.board.examinations.values():
            if (
                examination.verdict != "RETURN_TO_BUILDER"
                or examination.candidate_sha != move.base_sha
            ):
                continue
            candidate = self.board.candidates.get(examination.candidate_sha)
            if (
                move.child_of_sha != examination.candidate_sha
                or move.workspace_key == examination.workspace_key
                or (candidate is not None and move.branch == candidate.branch)
            ):
                reasons.append("return_to_builder_requires_new_child_branch_workspace")

        if (
            task.required_role not in agent.roles
            or not task.required_capabilities.issubset(agent.capabilities)
        ):
            reasons.append("agent_role_or_capability_mismatch")
        if self._dependency_cycle():
            reasons.append("dependency_cycle")
        if any(
            self.board.tasks.get(key) is None
            or self.board.tasks[key].status is not BoardStatus.CERTIFIED
            or self.board.tasks[key].evidence is None
            or not self.board.tasks[key].evidence.fresh(at)
            for key in task.dependencies
        ):
            reasons.append("dependency_not_ready_or_unknown")
        if any(x.task_key == task.key and x.active for x in self.board.blockers):
            reasons.append("active_blocker")
        reasons.extend(self._migration_collisions(move, task.migration_namespaces))
        return (
            PlannedMove(move, False, "BLOCKED", tuple(dict.fromkeys(reasons)))
            if reasons
            else PlannedMove(move, True, "ASSIGNABLE", ())
        )

    def _migration_collisions(
        self, move: ProposedMove, namespaces: frozenset[str]
    ) -> list[str]:
        if not namespaces:
            return []
        for execution in self.board.executions.values():
            if execution.execution_id == move.execution_id or execution.status not in {
                BoardStatus.RUNNING,
                BoardStatus.RESERVED,
            }:
                continue
            other = self.board.tasks.get(execution.task_key)
            if other is not None and namespaces.intersection(
                other.migration_namespaces
            ):
                return ["migration_namespace_collision"]
        return []

    def acquire_lease(
        self, move: ProposedMove, *, ttl: timedelta, now: datetime | None = None
    ) -> ResourceLease | None:
        at = now or self.clock.now()
        with self._lock:
            if self.coordination is None or ttl <= timedelta(0):
                return None
            if not self._validate_move_locked(move, at).legal:
                return None
            lease = self.coordination.acquire(
                move, self.board.workspaces[move.workspace_key].path, ttl, at
            )
            if lease is not None:
                self.board.leases[move.workspace_key] = lease
            return lease

    def heartbeat_lease(
        self,
        workspace_key: str,
        *,
        agent_key: str,
        execution_id: str,
        fencing_token: int,
        ttl: timedelta,
        now: datetime | None = None,
    ) -> ResourceLease | None:
        at = now or self.clock.now()
        with self._lock:
            if self.coordination is None:
                return None
            lease = self.coordination.heartbeat(
                workspace_key, agent_key, execution_id, fencing_token, ttl, at
            )
            if lease is not None:
                self.board.leases[workspace_key] = lease
            return lease

    def release_lease(
        self,
        workspace_key: str,
        *,
        agent_key: str,
        execution_id: str,
        fencing_token: int,
        now: datetime | None = None,
    ) -> bool:
        at = now or self.clock.now()
        with self._lock:
            if self.coordination is None:
                return False
            released = self.coordination.release(
                workspace_key, agent_key, execution_id, fencing_token, at
            )
            if released and workspace_key in self.board.leases:
                self.board.leases[workspace_key] = replace(
                    self.board.leases[workspace_key], released_at=at
                )
            return released

    def expire_leases(self, *, now: datetime | None = None) -> int:
        at = now or self.clock.now()
        with self._lock:
            if self.coordination is None:
                return 0
            count = self.coordination.expire(at)
            self.board.leases = self._active_leases(at)
            return count

    def ingest_reports(
        self,
        message_id: str,
        reports: Iterable[dict],
        *,
        received_at: datetime | None = None,
    ) -> tuple[ReportEvent, ...]:
        at = received_at or self.clock.now()
        events: list[ReportEvent] = []
        with self._lock:
            existing = {event.event_id: event for event in self.board.reports}
            for index, report in enumerate(reports):
                required = {
                    "agent_key",
                    "task_key",
                    "execution_id",
                    "result_type",
                    "status",
                }
                if not required.issubset(report):
                    raise ValueError(
                        "each report requires agent, task, execution, result type, and status"
                    )
                event = ReportEvent(
                    event_id=f"{message_id}:{index}",
                    message_id=message_id,
                    agent_key=report["agent_key"],
                    task_key=report["task_key"],
                    execution_id=report["execution_id"],
                    branch=report.get("branch"),
                    sha=report.get("sha"),
                    result_type=ResultType(report["result_type"]),
                    reported_status=BoardStatus(report["status"]),
                    evidence=EvidencePointer(
                        "founder_message", message_id, at, False, payload=dict(report)
                    ),
                )
                prior = existing.get(event.event_id)
                if prior is not None:
                    if prior.evidence.payload != event.evidence.payload:
                        raise ValueError(
                            "report event id replayed with conflicting payload"
                        )
                    events.append(prior)
                    continue
                self.board.reports.append(event)
                existing[event.event_id] = event
                events.append(event)
        return tuple(events)

    def ingest_machine_execution(self, execution: Execution) -> None:
        if not execution.evidence.authoritative:
            raise ValueError("machine execution event must be authoritative")
        with self._lock:
            current = self.board.executions.get(execution.execution_id)
            if (
                current is None
                or current.evidence.observed_at < execution.evidence.observed_at
            ):
                self.board.executions[execution.execution_id] = execution

    def record_examiner_verdict(
        self, examination_id: str, verdict: str, evidence: EvidencePointer
    ) -> None:
        with self._lock:
            if evidence.source not in _TRUSTED_VERDICT_SOURCES or not evidence.fresh(
                self.clock.now()
            ):
                raise ValueError(
                    "examiner verdict requires fresh trusted verification-registry evidence"
                )
            examination = self.board.examinations[examination_id]
            candidate = self.board.candidates[examination.candidate_sha]
            required = {
                "examination_id": examination_id,
                "candidate_sha": examination.candidate_sha,
                "examiner_agent": examination.examiner_agent,
                "verdict": verdict,
            }
            if any(
                evidence.payload.get(key) != value for key, value in required.items()
            ):
                raise ValueError(
                    "examiner verdict evidence is not bound to registry identity"
                )
            builders = {
                row.agent_key
                for row in self.board.executions.values()
                if row.task_key == candidate.task_key
                and row.workspace_key != examination.workspace_key
            }
            if examination.examiner_agent in builders:
                raise ValueError("builder cannot certify its own candidate")
            if verdict == "FREEZE_SHA":
                self.board.examinations[examination_id] = replace(
                    examination,
                    status=BoardStatus.CERTIFIED,
                    verdict=verdict,
                    evidence=evidence,
                )
                self.board.candidates[candidate.sha] = replace(
                    candidate,
                    status=BoardStatus.CERTIFIED,
                    frozen=True,
                    evidence=evidence,
                )
            elif verdict == "RETURN_TO_BUILDER":
                self.board.examinations[examination_id] = replace(
                    examination,
                    status=BoardStatus.COMPLETE_UNVERIFIED,
                    verdict=verdict,
                    evidence=evidence,
                )
            else:
                raise ValueError("unsupported examiner verdict")

    def remote_pushed(self, branch: str, sha: str) -> bool:
        with self._lock:
            row = self.board.branches.get(branch)
            return bool(
                row
                and row.evidence.source == "github"
                and row.evidence.fresh(self.clock.now())
                and row.remote_sha == sha
            )

    def discover_sha(
        self, *, branch: str | None = None, task_key: str | None = None
    ) -> str | None:
        with self._lock:
            if branch is not None:
                row = self.board.branches.get(branch)
                if (
                    row
                    and row.evidence.source == "github"
                    and row.evidence.fresh(self.clock.now())
                ):
                    return row.remote_sha
            if task_key is not None:
                matches = [
                    row
                    for row in self.board.candidates.values()
                    if row.task_key == task_key
                ]
                if matches:
                    return max(matches, key=lambda row: row.evidence.observed_at).sha
            return None

    def certification_valid(self, candidate_sha: str, examiner_agent: str) -> bool:
        with self._lock:
            candidate = self.board.candidates.get(candidate_sha)
            if candidate is None:
                return False
            builders = {
                row.agent_key
                for row in self.board.executions.values()
                if row.task_key == candidate.task_key
            }
            return examiner_agent not in builders and any(
                exam.candidate_sha == candidate_sha
                and exam.examiner_agent == examiner_agent
                and exam.status == BoardStatus.CERTIFIED
                and exam.evidence.source in _TRUSTED_VERDICT_SOURCES
                for exam in self.board.examinations.values()
            )

    def _active_leases(self, at: datetime) -> dict[str, ResourceLease]:
        if self.coordination is None:
            return {}
        return {item.workspace_key: item for item in self.coordination.active(at)}

    @staticmethod
    def _merge_reports(target: list[ReportEvent], source: list[ReportEvent]) -> None:
        known = {event.event_id: event for event in target}
        for event in source:
            prior = known.get(event.event_id)
            if prior is not None and prior.evidence.payload != event.evidence.payload:
                raise ValueError("conflicting report event in authoritative refresh")
            if prior is None:
                target.append(event)
                known[event.event_id] = event

    def _dependency_cycle(self) -> bool:
        graph = {key: set(task.dependencies) for key, task in self.board.tasks.items()}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> bool:
            if key in visiting:
                return True
            if key in visited:
                return False
            visiting.add(key)
            if any(dep in graph and visit(dep) for dep in graph.get(key, ())):
                return True
            visiting.remove(key)
            visited.add(key)
            return False

        return any(visit(key) for key in graph)
