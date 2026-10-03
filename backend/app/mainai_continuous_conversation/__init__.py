"""MainAI continuous conversation foundation.

The founder talks to MainAI. MainAI manages the machines. Agent chat is not a
founder-relay bus. Conversation state does not grant security permissions.
"""

from app.mainai_continuous_conversation.classify import classify_inbound
from app.mainai_continuous_conversation.discover import discover_software_truth
from app.mainai_continuous_conversation.occupancy import occupancy_for_turn
from app.mainai_continuous_conversation.orchestrate import founder_alpha_continuous_turn, handle_founder_message
from app.mainai_continuous_conversation.outbound import filter_outbound
from app.mainai_continuous_conversation.service import (
    apply_outbound_filter,
    compose_founder_reply,
    get_or_create_canonical_conversation,
    persist_turn,
)

__all__ = [
    "apply_outbound_filter",
    "classify_inbound",
    "compose_founder_reply",
    "discover_software_truth",
    "filter_outbound",
    "founder_alpha_continuous_turn",
    "get_or_create_canonical_conversation",
    "handle_founder_message",
    "occupancy_for_turn",
    "persist_turn",
]
