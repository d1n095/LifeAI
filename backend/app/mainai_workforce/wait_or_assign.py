"""Wait vs Assign Decision. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md.

AGENT AVAILABLE != BEST AGENT. BEST AGENT != BEST TO WAIT FOR. WAIT TIME != WASTED TIME. MORE
PARALLELISM != MORE PROGRESS. CRITICAL PATH > RAW UTILIZATION.

Pure: no `db`, no I/O. Does not decide WHO is busy/idle (that is
`situational_snapshot.py`'s real, composed job) -- this module only decides what to do given
the caller-supplied facts."""

from __future__ import annotations

from dataclasses import dataclass

from app.mainai_workforce.types import WaitOrAssignDecision

# Hand-picked, documented starting point -- no historical data exists yet to fit this against
# (same convention as every other threshold in this program). Below this ETA, waiting for the
# best agent is worth it PROVIDED it stays meaningfully ahead on competency; above it, a safe
# candidate should proceed now rather than block the critical path on an unbounded wait.
WAIT_ETA_THRESHOLD_SECONDS = 1800.0  # 30 minutes
MIN_COMPETENCY_GAP_TO_JUSTIFY_WAIT = 0.05


@dataclass(frozen=True)
class WaitOrAssignResult:
    decision: WaitOrAssignDecision
    reason: str


def decide_wait_or_assign(
    *,
    best_agent_available: bool,
    best_agent_eta_seconds: float | None,
    best_agent_competency: float,
    candidate_agent_available: bool,
    candidate_agent_competency: float,
    critical_path: bool = False,
    duplication_risk: bool = False,
    branch_conflict_risk: bool = False,
) -> WaitOrAssignResult:
    """First check wins.

    1. Duplication/branch-conflict risk -> DEFER regardless of everyone's availability (MORE
       PARALLELISM != MORE PROGRESS if it means two agents fighting over the same files).
    2. The best agent is already available -> WORK_NOW with it (trivial case).
    3. The best agent is busy with a SHORT, known ETA and remains meaningfully more competent
       than the candidate -> WAIT (matches the founder's own worked example: Codex 8 minutes
       from finishing and ideal, Claude idle but an expensive context reload -> WAIT FOR
       CODEX). WAIT TIME != WASTED TIME here.
    4. Otherwise, if a safe candidate is available -> WORK_NOW with the candidate (matches the
       founder's own second worked example: Codex 3 hours remaining, Claude can safely complete
       now -> ASSIGN CLAUDE). CRITICAL PATH > RAW UTILIZATION: this branch fires even when
       waiting would eventually yield a marginally better agent, because the ETA is too long to
       justify blocking the critical path.
    5. Neither the best agent nor a safe candidate is available now -> WAIT by default (nothing
       else that is safe to do).
    """

    if duplication_risk or branch_conflict_risk:
        return WaitOrAssignResult(WaitOrAssignDecision.DEFER, "duplication or branch-conflict risk detected -- avoid assigning until resolved, regardless of who is available")

    if best_agent_available:
        return WaitOrAssignResult(WaitOrAssignDecision.WORK_NOW, "best agent is available now")

    competency_gap = best_agent_competency - candidate_agent_competency

    if (
        best_agent_eta_seconds is not None
        and best_agent_eta_seconds <= WAIT_ETA_THRESHOLD_SECONDS
        and competency_gap >= MIN_COMPETENCY_GAP_TO_JUSTIFY_WAIT
    ):
        return WaitOrAssignResult(
            WaitOrAssignDecision.WAIT,
            f"best agent is {best_agent_eta_seconds:.0f}s from available (<= {WAIT_ETA_THRESHOLD_SECONDS:.0f}s) and "
            f"remains {competency_gap:.2f} more competent than the candidate -- WAIT TIME != WASTED TIME",
        )

    if candidate_agent_available:
        eta_desc = f"{best_agent_eta_seconds:.0f}s" if best_agent_eta_seconds is not None else "unknown/long"
        return WaitOrAssignResult(
            WaitOrAssignDecision.WORK_NOW,
            f"best agent ETA is {eta_desc} (> {WAIT_ETA_THRESHOLD_SECONDS:.0f}s or the competency gap does not justify waiting) -- "
            f"a safe candidate is available now; CRITICAL PATH > RAW UTILIZATION" if critical_path else
            f"best agent ETA is {eta_desc}; a safe candidate is available now",
        )

    return WaitOrAssignResult(WaitOrAssignDecision.WAIT, "neither the best agent nor a safe candidate is available right now")
