"""Machine-readable board types used by the grandmaster kernel."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class SystemClock:
    def now(self) -> datetime:
        return utc_now()


@dataclass(frozen=True)
class FixedClock:
    current: datetime

    def now(self) -> datetime:
        return self.current


class BoardStatus(StrEnum):
    IDLE = "IDLE"
    RESERVED = "RESERVED"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    STALE = "STALE"
    COMPLETE_UNVERIFIED = "COMPLETE_UNVERIFIED"
    UNDER_EXAMINATION = "UNDER_EXAMINATION"
    CERTIFIED = "CERTIFIED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class WorkspaceMode(StrEnum):
    MUTABLE = "MUTABLE"
    READ_ONLY = "READ_ONLY"


class ResultType(StrEnum):
    BUILDER_REPORT = "BUILDER_REPORT"
    EXAMINER_REPORT = "EXAMINER_REPORT"
    CI_RESULT = "CI_RESULT"
    EXECUTION_EVENT = "EXECUTION_EVENT"


@dataclass(frozen=True)
class EvidencePointer:
    source: str
    source_ref: str
    observed_at: datetime
    authoritative: bool
    mutable: bool = True
    expires_at: datetime | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def fresh(self, now: datetime) -> bool:
        return self.authoritative and (
            not self.mutable or (self.expires_at is not None and self.expires_at > now)
        )


@dataclass(frozen=True)
class Agent:
    key: str
    roles: frozenset[str]
    capabilities: frozenset[str]
    status: BoardStatus
    evidence: EvidencePointer


@dataclass(frozen=True)
class AgentSession:
    session_id: str
    agent_key: str
    status: BoardStatus
    evidence: EvidencePointer


@dataclass(frozen=True)
class Task:
    key: str
    title: str
    required_role: str
    required_capabilities: frozenset[str]
    status: BoardStatus
    dependencies: frozenset[str] = frozenset()
    migration_namespaces: frozenset[str] = frozenset()
    requested_artifact: str | None = None
    evidence: EvidencePointer | None = None


@dataclass(frozen=True)
class Execution:
    execution_id: str
    task_key: str
    agent_key: str
    status: BoardStatus
    workspace_key: str | None
    branch: str | None
    sha: str | None
    evidence: EvidencePointer


@dataclass(frozen=True)
class Workspace:
    key: str
    path: str
    mode: WorkspaceMode
    owner_agent: str | None
    execution_id: str | None
    branch: str | None
    base_sha: str | None
    evidence: EvidencePointer


@dataclass(frozen=True)
class Branch:
    name: str
    local_sha: str | None
    remote_sha: str | None
    owner_agent: str | None
    evidence: EvidencePointer


@dataclass(frozen=True)
class CandidateSHA:
    sha: str
    task_key: str
    branch: str
    status: BoardStatus
    frozen: bool
    evidence: EvidencePointer


@dataclass(frozen=True)
class Examination:
    examination_id: str
    candidate_sha: str
    examiner_agent: str
    workspace_key: str
    status: BoardStatus
    verdict: str | None
    evidence: EvidencePointer


@dataclass(frozen=True)
class Dependency:
    task_key: str
    depends_on: str
    satisfied: bool | None
    evidence: EvidencePointer


@dataclass(frozen=True)
class Blocker:
    blocker_id: str
    task_key: str
    reason: str
    active: bool
    evidence: EvidencePointer


@dataclass(frozen=True)
class ResourceLease:
    workspace_key: str
    workspace_path: str
    task_key: str
    owner_agent: str
    execution_id: str
    branch: str
    mode: WorkspaceMode
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    fencing_token: int
    released_at: datetime | None = None

    def active(self, now: datetime) -> bool:
        return self.released_at is None and self.expires_at > now


@dataclass(frozen=True)
class ProposedMove:
    move_id: str
    task_key: str
    agent_key: str
    execution_id: str
    workspace_key: str
    branch: str
    base_sha: str
    action: str = "build"
    child_of_sha: str | None = None
    expected_value: int = 0
    critical_path_impact: int = 0
    reversibility: int = 0
    unblock_potential: int = 0


@dataclass(frozen=True)
class Contingency:
    task_key: str
    on_pass: ProposedMove | None = None
    on_return_to_builder: ProposedMove | None = None
    on_block: ProposedMove | None = None
    on_agent_idle: ProposedMove | None = None


@dataclass(frozen=True)
class PlannedMove:
    move: ProposedMove
    legal: bool
    outcome: str
    reasons: tuple[str, ...]
    rank: tuple[int, ...] | None = None


@dataclass(frozen=True)
class ReportEvent:
    event_id: str
    message_id: str
    agent_key: str
    task_key: str
    execution_id: str
    branch: str | None
    sha: str | None
    result_type: ResultType
    reported_status: BoardStatus
    evidence: EvidencePointer


@dataclass
class Board:
    refreshed_at: datetime | None = None
    agents: dict[str, Agent] = field(default_factory=dict)
    sessions: dict[str, AgentSession] = field(default_factory=dict)
    tasks: dict[str, Task] = field(default_factory=dict)
    executions: dict[str, Execution] = field(default_factory=dict)
    workspaces: dict[str, Workspace] = field(default_factory=dict)
    branches: dict[str, Branch] = field(default_factory=dict)
    candidates: dict[str, CandidateSHA] = field(default_factory=dict)
    examinations: dict[str, Examination] = field(default_factory=dict)
    dependencies: list[Dependency] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)
    leases: dict[str, ResourceLease] = field(default_factory=dict)
    reports: list[ReportEvent] = field(default_factory=list)

    def copy(self) -> "Board":
        return replace(
            self,
            agents=dict(self.agents),
            sessions=dict(self.sessions),
            tasks=dict(self.tasks),
            executions=dict(self.executions),
            workspaces=dict(self.workspaces),
            branches=dict(self.branches),
            candidates=dict(self.candidates),
            examinations=dict(self.examinations),
            dependencies=list(self.dependencies),
            blockers=list(self.blockers),
            leases=dict(self.leases),
            reports=list(self.reports),
        )
