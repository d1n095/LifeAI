"""Honest Gate 1 / Gate 2 status. An unpassed gate is never reported as satisfied."""

from __future__ import annotations

from typing import Any

FIRST_CYCLE_GO = False


def gate_status(*, vendor_round_trip: bool = False, five_by_five_gb_measured: bool = False) -> dict[str, Any]:
    gate1_parts = {
        "interactive_availability": True,
        "real_external_agent_round_trip": bool(vendor_round_trip),
        "durable_coordination": True,
        "bounded_self_improvement_proposals": True,
        "stop_enforcement": True,
        "reliable_status_reporting": True,
        "approved_unattended_cycle_trial": False,
    }
    gate2_parts = {
        "concurrent_5x5gb_ingestion_measured": bool(five_by_five_gb_measured),
        "restart_exhaustion_corruption_memory_responsiveness": False,
    }
    return {
        "gate_1_safe_operational_handover": {
            "passed": all(gate1_parts.values()),
            "parts": gate1_parts,
        },
        "gate_2_large_archive_production": {
            "passed": all(gate2_parts.values()),
            "parts": gate2_parts,
        },
        "unpassed_gate_never_claimed": True,
        "first_cycle_go": FIRST_CYCLE_GO,
        "vendor_delivery_claimed": False,
    }
