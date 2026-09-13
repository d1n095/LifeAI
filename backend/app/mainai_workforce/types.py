"""Shared vocabulary for `app.mainai_workforce`. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md.

CAPABILITY != AUTHORITY. MASTERY != AUTHORITY. BEST AGENT != AUTHORIZED AGENT. SCHEDULER
RECOMMENDATION != EXECUTION AUTHORIZATION. LOCAL MODEL SUCCESS != NEW PERMISSION."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class WorkforceError(ValueError):
    """Raised on a structural/ownership violation, never for "no data observed yet"."""


class AutonomyStage(enum.IntEnum):
    """STAGE 0 (external dependency) through STAGE 6 (external no longer required for normal
    cases), per the founder's own §10 -- ordinal, IntEnum so comparisons/promotion-by-one-step
    are trivial and structurally bounded to 0..6 (matches migration 0075's own CHECK
    constraint)."""

    EXTERNAL_DEPENDENCY = 0
    OBSERVE_EXTERNAL_EXPERT = 1
    PROPOSE_EXTERNAL_CORRECTS = 2
    EXECUTE_EXTERNAL_EXAMINES = 3
    EXECUTE_LOCAL_EXAMINER_EXTERNAL_SPOT_CHECKS = 4
    LOCAL_DEFAULT_EXTERNAL_FALLBACK = 5
    EXTERNAL_NOT_REQUIRED = 6


class WaitOrAssignDecision(str, enum.Enum):
    WORK_NOW = "work_now"
    WAIT = "wait"
    CONTINUE_CURRENT_WORK = "continue_current_work"
    HAND_OFF = "hand_off"
    RESERVE_FOR_UPCOMING = "reserve_for_upcoming"
    ACT_AS_EXAMINER = "act_as_examiner"
    KEEP_FREE = "keep_free"
    DEFER = "defer"


class ProviderDependenceRecommendation(str, enum.Enum):
    KEEP_AS_DEFAULT = "keep_as_default"
    KEEP_AS_EXAMINER = "keep_as_examiner"
    MOVE_TO_FALLBACK = "move_to_fallback"
    REDUCE_USAGE = "reduce_usage"
    DO_NOT_REDUCE_YET = "do_not_reduce_yet"


@dataclass(frozen=True)
class TeacherObservation:
    """Observable METHOD/PROCESS evidence from one external-agent job -- NEVER hidden
    chain-of-thought, per the founder's own explicit instruction. Only what was actually
    observable from the outside: what was inspected, what was tried, what was corrected."""

    task_class: str
    provider: str
    problem_decomposition_steps: tuple[str, ...] = field(default_factory=tuple)
    files_inspected: tuple[str, ...] = field(default_factory=tuple)
    tools_used: tuple[str, ...] = field(default_factory=tuple)
    debug_method: str | None = None
    tests_selected: tuple[str, ...] = field(default_factory=tuple)
    root_cause_method: str | None = None
    dependency_tracing_steps: tuple[str, ...] = field(default_factory=tuple)
    errors_encountered: tuple[str, ...] = field(default_factory=tuple)
    examiner_corrections: tuple[str, ...] = field(default_factory=tuple)
    cost_usd: float | None = None
    time_seconds: float | None = None
    rework_required: bool = False
