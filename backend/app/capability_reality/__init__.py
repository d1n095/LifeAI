"""Deterministic recording/query API for Life's own capability reality -- what she and the
surrounding system can actually do right now, distinct from what is merely configured,
planned, or unknown. See `service.py`'s own module docstring for the full doctrine."""

from app.capability_reality.agent_bridge import sync_agent_adapter_capability
from app.capability_reality.service import (
    CapabilityRealityError,
    get_capability_reality,
    list_capability_gaps,
    list_capability_records,
    record_capability_gap,
    record_capability_observation,
)

__all__ = [
    "CapabilityRealityError",
    "get_capability_reality",
    "list_capability_gaps",
    "list_capability_records",
    "record_capability_gap",
    "record_capability_observation",
    "sync_agent_adapter_capability",
]
