"""Canonical orchestration state vocabulary.

LEDGER STATE != SECURITY PERMISSION. Recording that an agent is assigned, busy, or
"PASS"ed a review never grants merge, deploy, Recall, provider, or RLS authority.

AGENT TEXT != TASK STATE. A claim is evidence of what an agent said. GitHub, pytest
execution, and the independent verification registry remain the only software-truth
sources that may flip GitHub-backed or certification-backed fields.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


FOUNDER_ALPHA_FINAL_SHA = "2fbe20aacf1203fc0e16d216ef55b666b2181619"
FOUNDER_ALPHA_FINAL_BRANCH = "codex/founder-alpha-final-composed-candidate"

GITHUB_BACKED_FIELDS = frozenset(
    {
        "remote_pushed",
        "remote_sha",
        "branch_exists_remotely",
        "ci_state",
        "pr_merged",
        "default_branch_sha",
        "deployment_state",
        "local_matches_remote",
        "tree_sha",
    }
)

SOFTWARE_TRUTH_SOURCES = frozenset(
    {
        "github",
        "pytest_execution",
        "verification_registry",
        "local_git",
    }
)

AGENT_CLAIM_SOURCE = "agent_claim"


class AgentRole(str, enum.Enum):
    BUILDER = "builder"
    EXAMINER = "examiner"
    VALIDATOR = "validator"
    RESEARCHER = "researcher"
    OPERATOR = "operator"


class TaskStatus(str, enum.Enum):
    QUEUED = "queued"
    ASSIGNED = "assigned"
    RUNNING = "running"
    BLOCKED = "blocked"
    AWAITING_EXTERNAL = "awaiting_external"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OccupancyStatus(str, enum.Enum):
    IDLE = "idle"
    RUNNING = "running"
    BLOCKED = "blocked"


class SlotStatus(str, enum.Enum):
    FREE = "free"
    OCCUPIED = "occupied"


class AssignmentRefusal(str, enum.Enum):
    AGENT_RUNNING = "AGENT_RUNNING"
    NO_FREE_SLOT = "NO_FREE_SLOT"
    FROZEN_CANDIDATE = "FROZEN_CANDIDATE"
    DUPLICATE_WORK = "DUPLICATE_WORK"
    BRANCH_CONFLICT = "BRANCH_CONFLICT"
    MISSING_AGENT = "MISSING_AGENT"


class NextActionKind(str, enum.Enum):
    ASSIGN_INDEPENDENT_LANE = "assign_independent_lane"
    REQUEST_INDEPENDENT_EXAMINATION = "request_independent_examination"
    RETURN_DEFECT_TO_BUILDER = "return_defect_to_builder"
    CONTINUE_OR_REASSIGN = "continue_or_reassign"
    HOLD_FROZEN_CANDIDATE = "hold_frozen_candidate"
    DISCOVER_FROM_GITHUB = "discover_from_github"
    WAIT_FOR_RUNNING_AGENT = "wait_for_running_agent"
    NO_FOUNDER_RELAY = "no_founder_relay"


class AuthorityRequired(str, enum.Enum):
    NONE = "none"
    FOUNDER_ONLY = "founder_only"
    MATERIAL_PRODUCT_DECISION = "material_product_decision"
    SECURITY_POLICY = "security_policy"
    MONEY_BUDGET = "money_budget"
    DESTRUCTIVE_ACTION = "destructive_action"
    UNRESOLVED_ALTERNATIVES = "unresolved_alternatives"
    GENUINE_BLOCKER = "genuine_blocker"


class ClaimKind(str, enum.Enum):
    REMOTE_PUSHED = "remote_pushed"
    TESTS_PASSED = "tests_passed"
    EXAMINER_RESULT = "examiner_result"
    BRANCH_EXISTS = "branch_exists"
    TASK_COMPLETED = "task_completed"
    STOPPED = "stopped"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class PytestRunEvidence:
    sha: str
    passed: int
    failed: int
    skipped: int
    command: str
    source: str
    tree_sha: str | None = None
    executed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.source != "pytest_execution":
            raise ValueError("test-run evidence must bind to pytest_execution, not agent text")
        if len(self.sha) != 40 or any(ch not in "0123456789abcdef" for ch in self.sha):
            raise ValueError("test-run evidence must bind to an exact 40-character lowercase git SHA")


@dataclass(frozen=True)
class GitHubCheckRun:
    name: str
    status: str
    conclusion: str | None
    head_sha: str


@dataclass(frozen=True)
class GitHubPullTruth:
    number: int
    state: str
    merged: bool
    head_sha: str
    head_ref: str
    base_ref: str


@dataclass(frozen=True)
class GitHubDeploymentTruth:
    id: int
    sha: str
    environment: str
    state: str | None


@dataclass(frozen=True)
class GitHubTruthSnapshot:
    """Point-in-time GitHub observation. Never inferred from conversation text."""

    branch: str
    exists_remotely: bool
    commit_sha: str | None
    tree_sha: str | None
    local_sha: str | None
    local_matches_remote: bool | None
    check_runs: tuple[GitHubCheckRun, ...] = ()
    pull_requests: tuple[GitHubPullTruth, ...] = ()
    default_branch: str | None = None
    default_branch_sha: str | None = None
    deployments: tuple[GitHubDeploymentTruth, ...] = ()
    captured_at: datetime | None = None
    source: str = "github"

    def __post_init__(self) -> None:
        if self.source != "github":
            raise ValueError("GitHub truth snapshots must name source='github'")

    @property
    def any_open_pr(self) -> bool:
        return any(pr.state == "open" for pr in self.pull_requests)

    @property
    def any_merged_pr(self) -> bool:
        return any(pr.merged for pr in self.pull_requests)

    @property
    def ci_any_failure(self) -> bool:
        return any(run.conclusion in {"failure", "timed_out", "cancelled", "action_required"} for run in self.check_runs)

    @property
    def ci_any_pending(self) -> bool:
        return any(run.status in {"queued", "in_progress", "pending"} for run in self.check_runs)

    @property
    def ci_all_completed_success(self) -> bool:
        if not self.check_runs:
            return False
        return all(run.status == "completed" and run.conclusion == "success" for run in self.check_runs)

    def ci_binds_to_sha(self, sha: str) -> bool:
        return bool(self.check_runs) and all(run.head_sha == sha for run in self.check_runs)


@dataclass
class AgentOccupancy:
    agent_key: str
    occupancy: OccupancyStatus
    role: AgentRole | None = None
    supports_parallel_workers: bool = False
    max_slots: int = 1
    occupied_slots: int = 0
    current_task_id: UUID | str | None = None
    current_task_title: str | None = None

    @property
    def free_slots(self) -> int:
        return max(0, self.max_slots - self.occupied_slots)

    @property
    def is_running(self) -> bool:
        return self.occupancy is OccupancyStatus.RUNNING or self.occupied_slots > 0


@dataclass
class TaskRecord:
    task_id: UUID | str
    title: str
    owner_id: UUID | str | None
    role: AgentRole
    status: TaskStatus
    agent_key: str | None = None
    exact_input_sha: str | None = None
    working_branch: str | None = None
    output_sha: str | None = None
    remote_sha: str | None = None
    remote_pushed: bool = False
    frozen: bool = False
    protects_sha: str | None = None
    protects_branch: str | None = None
    artifacts: list[Any] = field(default_factory=list)
    test_runs: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[dict[str, Any]] = field(default_factory=list)
    next_action: dict[str, Any] = field(default_factory=dict)
    authority_required: AuthorityRequired = AuthorityRequired.NONE
    last_verified_source: str | None = None
    last_verified_at: datetime | None = None
    depends_on: tuple[str, ...] = ()
    assigned_slot_key: str | None = None

    def protects(self, sha: str | None = None, branch: str | None = None) -> bool:
        if self.frozen and sha and self.protects_sha == sha:
            return True
        if self.frozen and branch and self.protects_branch == branch:
            return True
        return False


@dataclass(frozen=True)
class AgentClaim:
    agent_key: str
    kind: ClaimKind
    raw_text: str
    claimed_value: dict[str, Any]
    task_id: UUID | str | None = None
    bound_sha: str | None = None
    recorded_at: datetime | None = None
    authoritative: bool = False


@dataclass(frozen=True)
class ProposedAssignment:
    agent_key: str
    title: str
    role: AgentRole
    working_branch: str
    exact_input_sha: str | None = None
    modifies_branch: str | None = None
    depends_on: tuple[str, ...] = ()
    slot_key: str | None = None
    create_new_slot: bool = False


@dataclass(frozen=True)
class AssignmentDecision:
    allowed: bool
    refusal: AssignmentRefusal | None = None
    reason: str = ""
    slot_key: str | None = None


@dataclass(frozen=True)
class PlannedAction:
    kind: NextActionKind
    agent_key: str | None
    reason: str
    interrupt_founder: bool = False
    assignment: ProposedAssignment | None = None


@dataclass
class OrchestrationWorld:
    agents: list[AgentOccupancy]
    tasks: list[TaskRecord]
    github: GitHubTruthSnapshot | None = None
    frozen_sha: str = FOUNDER_ALPHA_FINAL_SHA
    frozen_branch: str = FOUNDER_ALPHA_FINAL_BRANCH
    queued_assignments: list[ProposedAssignment] = field(default_factory=list)
    claims: list[AgentClaim] = field(default_factory=list)

    def agent(self, key: str) -> AgentOccupancy | None:
        return next((item for item in self.agents if item.agent_key == key), None)

    def running_tasks_for(self, agent_key: str) -> list[TaskRecord]:
        return [
            task
            for task in self.tasks
            if task.agent_key == agent_key and task.status in {TaskStatus.ASSIGNED, TaskStatus.RUNNING}
        ]


@dataclass(frozen=True)
class OrchestrationPlan:
    actions: tuple[PlannedAction, ...]
    refused: tuple[AssignmentDecision, ...]
    founder_messages: tuple[str, ...]
    notes: tuple[str, ...]
