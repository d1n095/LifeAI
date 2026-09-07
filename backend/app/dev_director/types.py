"""MainAI V2 Autonomous Development Director -- core types (Part 1 of 2).

Standalone, isolated, NOT imported by any production runtime path (no app.main import, no
app.guardian/app.privacy_boundary/app.sentinel/app.sovereign_identity/app.life_recovery/
app.operating_shell/app.attachment_chamber import from this package). DOES reference real
production types by name/shape in docstrings (SupervisorScope, TaskScopedAuthority,
ReadinessLevel, APPROVAL_POLICIES, VerificationPolicy) -- this is a NEW coordination tier
explicitly designed to compose with real infrastructure, not a sixth sibling requiring
mutual independence from production code. See
docs/mainai_v2/MAINAI_V2_AUTONOMOUS_DEVELOPMENT_DIRECTOR_RECONCILIATION.md for the full
decision this implements.

PROGRAM STATE != EXECUTION AUTHORITY. AUTONOMY LEVEL != AUTHORITY TOKEN. TASK ASSIGNED !=
TASK AUTHORIZED. AGENT CLAIM != REPO STATE. PROVIDER OUTPUT != MAINAI COMMAND. BUILDER !=
FINAL EXAMINER. PROVIDER FAILURE != AUTHORITY WIDENING.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DevDirectorError(ValueError):
    """Base error for this package."""


# --- Autonomy level. -------------------------------------------------------------------


class AutonomyLevel(str, Enum):
    """AUTONOMY LEVEL != AUTHORITY TOKEN: nothing here grants anything. A declared policy
    intent that a future, real integration point (not built this round) would check against
    real authority before anything executes. `required_readiness_level`/`approval_policy_key`
    below reference the REAL app.mainai_startup_readiness.ReadinessLevel values and the REAL
    app.mainai_execution.approval.APPROVAL_POLICIES keys by name -- this enum does not import
    either module (reading/evaluating them is a real production call, out of scope for an
    isolated, unwired package), it only carries the STRING references a future integration
    point would use to look them up for real."""

    LEVEL_0_MANUAL_ONLY = "LEVEL_0_MANUAL_ONLY"
    LEVEL_1_ISOLATED_RESEARCH_CODE_TEST = "LEVEL_1_ISOLATED_RESEARCH_CODE_TEST"
    LEVEL_2_BUILDER_EXAMINER_FIX_LOOP_PR_PROPOSAL = "LEVEL_2_BUILDER_EXAMINER_FIX_LOOP_PR_PROPOSAL"
    LEVEL_3_AUTO_MERGE_LOW_RISK = "LEVEL_3_AUTO_MERGE_LOW_RISK"
    LEVEL_4_DEPLOYMENT_RELEASE = "LEVEL_4_DEPLOYMENT_RELEASE"


# Maps each AutonomyLevel to the REAL app.mainai_startup_readiness.ReadinessLevel value
# (by its exact string) required before jobs at that level may run for real, and the REAL
# app.mainai_execution.approval.APPROVAL_POLICIES key that would govern its tasks. Data only
# -- no import, no evaluation call.
AUTONOMY_LEVEL_REQUIRED_READINESS: dict[AutonomyLevel, str] = {
    AutonomyLevel.LEVEL_0_MANUAL_ONLY: "BLOCKED",
    AutonomyLevel.LEVEL_1_ISOLATED_RESEARCH_CODE_TEST: "READY_FOR_SAFE_INTERNAL_RUN",
    AutonomyLevel.LEVEL_2_BUILDER_EXAMINER_FIX_LOOP_PR_PROPOSAL: "READY_FOR_LOW_RISK_PROVIDER_RUN",
    AutonomyLevel.LEVEL_3_AUTO_MERGE_LOW_RISK: "READY_FOR_SERIOUS_AUTONOMOUS_RUN",
    AutonomyLevel.LEVEL_4_DEPLOYMENT_RELEASE: "READY_FOR_SERIOUS_AUTONOMOUS_RUN",
}

AUTONOMY_LEVEL_APPROVAL_POLICY_KEY: dict[AutonomyLevel, str] = {
    AutonomyLevel.LEVEL_0_MANUAL_ONLY: "standard_repo_work",
    AutonomyLevel.LEVEL_1_ISOLATED_RESEARCH_CODE_TEST: "standard_repo_work",
    AutonomyLevel.LEVEL_2_BUILDER_EXAMINER_FIX_LOOP_PR_PROPOSAL: "autonomous_development_work",
    AutonomyLevel.LEVEL_3_AUTO_MERGE_LOW_RISK: "autonomous_development_work",
    AutonomyLevel.LEVEL_4_DEPLOYMENT_RELEASE: "autonomous_development_work",
}

# Levels requiring a non-empty founder_authorization_ref before a Program may adopt them --
# enforced structurally in program.py's set_autonomy_level(), never bypassable by construction.
LEVELS_REQUIRING_EXPLICIT_FOUNDER_AUTHORIZATION = frozenset(
    {AutonomyLevel.LEVEL_3_AUTO_MERGE_LOW_RISK, AutonomyLevel.LEVEL_4_DEPLOYMENT_RELEASE}
)

DEFAULT_AUTONOMY_LEVEL = AutonomyLevel.LEVEL_2_BUILDER_EXAMINER_FIX_LOOP_PR_PROPOSAL


class AutonomyLevelRequiresFounderAuthorizationError(DevDirectorError):
    pass


# --- Budget envelope. ------------------------------------------------------------------


@dataclass
class BudgetEnvelope:
    """Program-local bookkeeping ONLY. Explicitly a THIRD, HIGHER-LEVEL ceiling -- NOT a
    replacement for or unification with the REAL app.provider_spend (LLM API call spend) or
    app.workforce.cost/TaskScopedAuthority.spend_ceiling_usd (workforce assignment spend)
    systems, neither of which this module imports. Real cross-system enforcement against
    either is explicitly future integration work, not built here -- this is honest,
    self-contained bookkeeping a caller can use for Program-level visibility, nothing more."""

    program_id: uuid.UUID
    total_ceiling_usd: float
    consumed_usd: float = 0.0
    reserved_usd: float = 0.0
    per_provider_ceiling_usd: dict[str, float] = field(default_factory=dict)
    per_provider_consumed_usd: dict[str, float] = field(default_factory=dict)
    per_day_ceiling_usd: float | None = None


class BudgetExceededError(DevDirectorError):
    pass


# --- Program. ----------------------------------------------------------------------------


@dataclass
class Program:
    """A durable, cross-goal development program. PROGRAM STATE != EXECUTION AUTHORITY:
    every field here is descriptive/coordinating only -- no function in this package takes a
    Program and returns something that could be mistaken for a real execution grant. Actual
    execution always goes through the REAL app.execution_envelopes/SupervisorScope/
    TaskScopedAuthority machinery, unchanged, with its OWN separately-supplied authority
    object, never derived from a Program alone.

    `goal_ids` are REFERENCES to real app.models.mainai_execution.MainAIGoal.id values --
    this package never copies or duplicates goal data. `queued_job_ids`/`blocked_job_ids`/
    `completed_job_ids`/`failed_job_ids` are VIEWS/INDICES for convenience; the real source of
    truth for a Job's state is always the Job's own `state` field -- see
    job.py's recompute_program_job_index(), the only function that may set these tuples, and
    it always DERIVES them from real Job objects rather than letting them drift independently.
    """

    program_id: uuid.UUID
    owner_id: uuid.UUID
    repo_identity: str
    goal: str
    priority: str
    risk_class: str
    autonomy_level: AutonomyLevel
    budget_envelope: BudgetEnvelope
    created_at: datetime
    updated_at: datetime
    allowed_providers: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    allowed_branches: tuple[str, ...] = ()
    protected_artifacts: tuple["ProtectedArtifact", ...] = ()
    current_phase: str = "PLANNING"
    goal_ids: tuple[uuid.UUID, ...] = ()
    queued_job_ids: tuple[uuid.UUID, ...] = ()
    blocked_job_ids: tuple[uuid.UUID, ...] = ()
    completed_job_ids: tuple[uuid.UUID, ...] = ()
    failed_job_ids: tuple[uuid.UUID, ...] = ()
    examiner_requirements: dict[str, bool] | None = None
    merge_policy: str = "PR_PROPOSAL_ONLY"
    deployment_policy: str = "MANUAL_ONLY"
    last_progress_at: datetime | None = None
    recovery_state: str = "NONE"
    founder_authorization_ref: str | None = None
    offline_mode: bool = False


# --- Job (work queue). ------------------------------------------------------------------


class JobState(str, Enum):
    PLANNED = "PLANNED"
    READY = "READY"
    ASSIGNED = "ASSIGNED"
    RUNNING = "RUNNING"
    WAITING_FOR_RESULT = "WAITING_FOR_RESULT"
    VERIFYING = "VERIFYING"
    NEEDS_FIX = "NEEDS_FIX"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CERTIFIED = "CERTIFIED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


# Explicit transition table, same convention as app.life_intents.service's
# LIFE_INTENT_TRANSITIONS: {state: {allowed targets}}. CERTIFIED/CANCELLED/FAILED are
# terminal (empty sets). SUPERSEDED is reachable from any non-terminal state (mirrors
# IntentObject's own supersession discipline: a superseded Job remains historical, never
# resurrectable -- nothing transitions OUT of SUPERSEDED). NEEDS_FIX can only reach
# CERTIFIED by passing back through VERIFYING again -- a fresh, real re-examination of the
# NEW artifact, never grandfathering the old verdict.
JOB_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.PLANNED: {JobState.READY, JobState.BLOCKED, JobState.CANCELLED, JobState.SUPERSEDED},
    JobState.READY: {JobState.ASSIGNED, JobState.BLOCKED, JobState.CANCELLED, JobState.SUPERSEDED},
    JobState.ASSIGNED: {JobState.RUNNING, JobState.BLOCKED, JobState.CANCELLED, JobState.SUPERSEDED},
    JobState.RUNNING: {JobState.WAITING_FOR_RESULT, JobState.VERIFYING, JobState.FAILED, JobState.CANCELLED, JobState.SUPERSEDED},
    JobState.WAITING_FOR_RESULT: {JobState.VERIFYING, JobState.FAILED, JobState.CANCELLED, JobState.SUPERSEDED},
    JobState.VERIFYING: {JobState.CERTIFIED, JobState.NEEDS_FIX, JobState.FAILED, JobState.CANCELLED, JobState.SUPERSEDED},
    JobState.NEEDS_FIX: {JobState.ASSIGNED, JobState.RUNNING, JobState.CANCELLED, JobState.SUPERSEDED},
    JobState.BLOCKED: {JobState.READY, JobState.CANCELLED, JobState.SUPERSEDED},
    JobState.FAILED: set(),
    JobState.CERTIFIED: set(),
    JobState.CANCELLED: set(),
    JobState.SUPERSEDED: set(),
}

TERMINAL_JOB_STATES = frozenset({JobState.FAILED, JobState.CERTIFIED, JobState.CANCELLED, JobState.SUPERSEDED})


class JobTransitionError(DevDirectorError):
    pass


@dataclass
class Job:
    """A unit of Director-layer work coordination. Thin reference layer over
    app.models.mainai_execution.MainAIGoal/MainAITask -- `goal_ref`/`task_ref` point at REAL
    rows when they exist; this package never fabricates or duplicates their data. A Job not
    yet backed by a real goal uses `goal_description` instead, until/unless one is created."""

    job_id: uuid.UUID
    program_id: uuid.UUID
    state: JobState
    created_at: datetime
    goal_ref: uuid.UUID | None = None
    task_ref: uuid.UUID | None = None
    goal_description: str | None = None
    builder_role: str | None = None
    examiner_role: str | None = None
    dependencies: tuple[uuid.UUID, ...] = ()
    blocked_by: tuple[uuid.UUID, ...] = ()
    touches: tuple[str, ...] = ()  # declared file/subsystem globs this job is expected to touch -- see conflicts.py
    input_artifact_sha: str | None = None
    expected_output: str | None = None
    risk_class: str = "low"
    authority_snapshot_ref: str | None = None  # opaque reference only, never actual authority material
    budget_usd: float | None = None
    timeout_seconds: int | None = None
    retry_policy: str = "no_auto_retry"
    attempt_number: int = 1
    result_artifact_sha: str | None = None
    test_evidence: "CompletionEvidence | None" = None
    review_evidence: "ExaminerVerdictRecord | None" = None
    next_action: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    history: tuple["JobEvent", ...] = ()


@dataclass
class JobEvent:
    """Hash-chained, append-only -- same discipline as Guardian's ContainmentReceipt chain
    and Sentinel's EventReceipt chain."""

    event_id: uuid.UUID
    job_id: uuid.UUID
    from_state: JobState | None
    to_state: JobState
    note: str
    prev_hash: str
    this_hash: str = ""
    at: datetime = field(default_factory=_utcnow)


class NoReadyJob:
    """Sentinel: no job is currently ready to run, OR a genuine priority tie exists that must
    not be silently broken by an arbitrary pick. `ambiguous_candidates` is non-empty only in
    the tie case -- DO NOT silently guess if multiple equal-priority candidates exist."""

    def __init__(self, *, ambiguous_candidates: tuple[uuid.UUID, ...] = ()) -> None:
        self.ambiguous_candidates = ambiguous_candidates


@dataclass(frozen=True)
class ConflictReport:
    job_a: uuid.UUID
    job_b: uuid.UUID
    overlapping: tuple[str, ...]
    reason: str


# --- Protected artifacts. ---------------------------------------------------------------


@dataclass(frozen=True)
class ProtectedArtifact:
    ref: str
    reason: str
    declared_by: str
    do_not_modify: bool = True
    do_not_merge: bool = True
    do_not_rebase: bool = True
    examine_only: bool = True


class ProtectedArtifactViolationError(DevDirectorError):
    pass


# The real, seeded first example -- matching this whole session's actual lived practice.
PR_245_PROTECTED_ARTIFACT = ProtectedArtifact(
    ref="818dfb732da47901eb5ae06ffdd9c829fe00c4c5",
    reason="PR #245's builder-not-final-examiner candidate: builder!=examiner doctrine, frozen pending independent certification.",
    declared_by="founder",
    do_not_modify=True,
    do_not_merge=True,
    do_not_rebase=True,
    examine_only=True,
)


# --- External provider lease. -----------------------------------------------------------


@dataclass(frozen=True)
class ExternalProviderLease:
    """Same SHAPE as app.development_supervisor.service.SupervisorScope (local execution) and
    app.workforce.authority.TaskScopedAuthority (workforce assignments), but for agents
    OUTSIDE this process (Codex/Cursor/Claude/local_agent:<id>). NEVER carries raw GitHub
    credentials or Vault access -- structurally enforced by simply never having a field that
    could hold either; the absence of the field IS the defense (see
    test_external_provider_lease_field_set_is_closed, which pins this dataclass's own field
    set so a future accidental addition is caught)."""

    lease_id: uuid.UUID
    provider_identity: str
    task_ref: uuid.UUID | None
    workspace_ref: str
    branch: str
    allowed_tools: tuple[str, ...]
    allowed_files: tuple[str, ...]
    expires_at: datetime
    budget_usd: float | None
    network_scope: tuple[str, ...]
    disclosure_scope: str
    authority_scope: tuple[str, ...]


class ProviderUsageState(str, Enum):
    AVAILABLE = "AVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    USAGE_EXHAUSTED = "USAGE_EXHAUSTED"
    OFFLINE = "OFFLINE"
    FAILED = "FAILED"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True)
class ProviderCapabilityProfile:
    provider_identity: str
    capabilities: tuple[str, ...]
    usage_state: ProviderUsageState
    cost_class: str
    privacy_class: str
    recent_failure_rate: float
    is_examiner_eligible: bool = True


class NoAvailableProvider:
    def __init__(self, *, reason: str) -> None:
        self.reason = reason


# --- Builder / examiner separation. -----------------------------------------------------


@dataclass(frozen=True)
class BuilderAssignment:
    """References EITHER a real WorkforceAssignment.id (local) OR an
    ExternalProviderLease.lease_id (external) -- never both, never neither. See
    builder_examiner.py's new_builder_assignment(), the only real constructor, which enforces
    this XOR structurally."""

    assignment_id: uuid.UUID
    job_id: uuid.UUID
    builder_identity: str
    workforce_assignment_ref: uuid.UUID | None
    external_lease_ref: uuid.UUID | None
    created_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class BuilderResult:
    """`claimed_completion` is the builder's OWN claim -- explicitly separate from and never
    conflated with verified truth (see completion_evidence.py)."""

    assignment_id: uuid.UUID
    result_sha: str
    branch: str
    builder_identity: str
    claimed_completion: bool
    submitted_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class ExaminerAssignment:
    """`target_sha` is the EXACT frozen SHA being examined -- never "whatever the branch
    currently points to". See builder_examiner.py's new_examiner_assignment(), which requires
    target_sha to be pinned explicitly by the caller, never re-resolved from a mutable branch
    ref."""

    examiner_assignment_id: uuid.UUID
    job_id: uuid.UUID
    examiner_identity: str
    target_sha: str
    scope: str
    known_risks: tuple[str, ...]
    test_expectations: tuple[str, ...]
    created_at: datetime = field(default_factory=_utcnow)


class ExaminerVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ExaminerVerdictRecord:
    examiner_assignment_id: uuid.UUID
    examiner_identity: str
    target_sha: str
    verdict: ExaminerVerdict
    evidence: tuple[str, ...]
    reason: str
    recorded_at: datetime = field(default_factory=_utcnow)


class BuilderExaminerCollusionError(DevDirectorError):
    pass


class ExaminerVerdictError(DevDirectorError):
    pass


# --- Completion evidence. ----------------------------------------------------------------


@dataclass(frozen=True)
class TestResultRecord:
    command: str
    passed: bool
    summary: str


@dataclass(frozen=True)
class CompletionEvidence:
    """AGENT CLAIM != REPO STATE, PROVIDER OUTPUT != MAINAI COMMAND: always treated as
    untrusted structured DATA, same discipline as app.attachment_chamber.untrusted_content's
    handling of extracted file content. No function in this package takes raw provider TEXT
    (a bare string) and produces an authority-relevant state change -- every state transition
    goes through this typed, validated record or an ExaminerVerdictRecord, never a free-text
    "the agent said it's done" shortcut."""

    branch: str
    base_sha: str
    new_sha: str
    working_tree_state: str  # "clean" | "dirty" | "unknown"
    changed_files: tuple[str, ...]
    test_commands: tuple[str, ...]
    test_results: tuple[TestResultRecord, ...]
    open_blockers: tuple[str, ...]
    p0_count: int
    p1_count: int
    production_wiring_state: str  # "none" | "partial" | "full"
    merge_state: str  # "unmerged" | "merged" | "superseded"
    submitted_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    sha_mismatch: bool
    reasons: tuple[str, ...]
