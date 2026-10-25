"""Gate-1 operational handover: absence policy, interactive isolation, bounded self-improve.

Reuses existing founder-ops, workforce, durable ledger, spend and STOP. Does not claim
Gate 1 or Gate 2. first_cycle_go stays false.
"""

from app.mainai_operational_handover.absence import (
    evaluate_absence_tick,
    founder_catchup_report,
)
from app.mainai_operational_handover.gates import gate_status
from app.mainai_operational_handover.interactive import InteractiveIsolation
from app.mainai_operational_handover.self_improve import run_bounded_self_improvement_cycle

__all__ = [
    "InteractiveIsolation",
    "evaluate_absence_tick",
    "founder_catchup_report",
    "gate_status",
    "run_bounded_self_improvement_cycle",
]
