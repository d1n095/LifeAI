"""Shared vocabulary for `app.mainai_cognitive_ops`. See
docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md for the full architecture decision this
package implements.

THIS PACKAGE OWNS: information lifecycle policy, context packaging, anti-duplication metadata,
communication-delta metadata, compatibility/impact analysis, repo-state observations, and
self-optimization measurement. It does NOT own a second copy of job/lease/authority truth --
those stay with `app.agent_coordination`/`app.resource_intelligence`/`app.mainai_research`/
`app.mainai_executive`, composed via read-only calls, never duplicated."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

__all__ = [
    "AgentState",
    "ProgramStatus",
    "WorkItem",
    "DuplicationVerdict",
    "InformationTier",
    "InformationTemperature",
    "CommunicationDeltaVerdict",
    "DebugStage",
    "RemoteSyncState",
    "CognitiveOpsError",
]


class CognitiveOpsError(ValueError):
    """Raised on a structural/ownership violation, never for "no data observed yet"."""


@dataclass(frozen=True)
class AgentState:
    """AGENT EXISTS != AGENT AVAILABLE. AGENT BUSY != ASSIGNABLE. Plain, caller-supplied
    snapshot -- this package never maintains its own agent registry (that stays with
    `app.agent_coordination`)."""

    agent_id: str
    exists: bool = True
    busy: bool = False
    blocked: bool = False
    idle: bool = False
    current_program: str | None = None
    current_task: str | None = None


class ProgramStatus(str, enum.Enum):
    """PROGRAM != SUBTASK. SUBTASK COMPLETE != PROGRAM COMPLETE. PARTIAL != COMPLETE."""

    ACTIVE = "active"
    SUBTASK_COMPLETE = "subtask_complete"
    PARTIAL = "partial"
    COMPLETE = "complete"


@dataclass(frozen=True)
class WorkItem:
    item_id: str
    program: str
    subtask: str | None
    owner_agent: str | None
    status: ProgramStatus
    keywords: tuple[str, ...] = field(default_factory=tuple)
    dependency_ids: tuple[str, ...] = field(default_factory=tuple)


class DuplicationVerdict(str, enum.Enum):
    """SAME GOAL != NECESSARILY SAME JOB. Independent-examiner duplication is intentional and
    must remain allowed -- it is a distinct verdict from accidental waste."""

    NOT_DUPLICATE = "not_duplicate"
    REUSE_CANDIDATE = "reuse_candidate"
    ACCIDENTAL_DUPLICATE = "accidental_duplicate"
    INTENTIONAL_EXAMINER_DUPLICATE = "intentional_examiner_duplicate"


class InformationTier(str, enum.Enum):
    """SUMMARY != SOURCE. RAW SOURCE != SUMMARY. ARCHIVED != FORGOTTEN."""

    RAW_SOURCE = "raw_source"
    DURABLE_STRUCTURED_MEMORY = "durable_structured_memory"
    ACTIVE_WORKING_CONTEXT = "active_working_context"


class InformationTemperature(str, enum.Enum):
    """COLD != IRRELEVANT. HOT != MORE TRUE. Temperature is a storage/retrieval concern only,
    never a truth or authority signal."""

    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class CommunicationDeltaVerdict(str, enum.Enum):
    """ALREADY REPORTED != REPORT AGAIN. NO MATERIAL STATUS CHANGE != FOUNDER NOTIFICATION."""

    SUPPRESS_ALREADY_REPORTED = "suppress_already_reported"
    SUPPRESS_NO_MATERIAL_CHANGE = "suppress_no_material_change"
    REPORT_MATERIAL_CHANGE = "report_material_change"
    REPORT_DECISION_REQUIRED = "report_decision_required"


class DebugStage(str, enum.Enum):
    """The full SYMPTOM -> ... -> LEARN systemic-debugging pipeline. Order matters --
    `systemic_debugging.assess_fix_readiness()` gates on this exact sequence."""

    SYMPTOM = "symptom"
    REPRODUCE = "reproduce"
    ROOT_CAUSE = "root_cause"
    CALLERS = "callers"
    CALLEES = "callees"
    DATA_FLOW = "data_flow"
    SCHEMA = "schema"
    DATABASE_READS_WRITES = "database_reads_writes"
    AUTHORITY = "authority"
    SECURITY = "security"
    PRIVACY = "privacy"
    CONCURRENCY = "concurrency"
    LEASES = "leases"
    RETRIES = "retries"
    RECOVERY = "recovery"
    CACHES = "caches"
    CURRENTNESS = "currentness"
    API_CONTRACTS = "api_contracts"
    COMPONENT_DEPENDENCIES = "component_dependencies"
    FAILURE_PATHS = "failure_paths"
    SMALLEST_SAFE_FIX = "smallest_safe_fix"
    LOCAL_TEST = "local_test"
    ADJACENT_INTEGRATION_TEST = "adjacent_integration_test"
    REAL_RUNTIME_PATH = "real_runtime_path"
    REGRESSION = "regression"
    CRASH_RESTART = "crash_restart"
    NEW_CONFLICT_CHECK = "new_conflict_check"
    LEARN = "learn"


@dataclass(frozen=True)
class RemoteSyncState:
    """LOCAL COMMIT != REMOTE COMMIT. COMMITTED != BACKED UP. VERIFIED LOCALLY != AVAILABLE
    REMOTELY. REMOTE EXISTS != REMOTE IS CURRENT."""

    branch: str
    local_sha: str
    remote_sha: str | None
    ahead: int | None
    behind: int | None
    dirty_files: tuple[str, ...] = field(default_factory=tuple)
    as_of: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)
