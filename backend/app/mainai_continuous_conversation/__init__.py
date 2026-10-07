"""MainAI continuous conversation foundation.

The founder talks to MainAI. MainAI looks up machine-discoverable facts. Agent
assignment execution is not wired on this chat surface. Conversation state does
not grant security permissions. Subject binding is required for every SHA answer.
"""

from app.mainai_continuous_conversation.classify import classify_inbound
from app.mainai_continuous_conversation.discover import discover_software_truth
from app.mainai_continuous_conversation.entities import bind_subject, bind_subject_from_db
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
    "bind_subject",
    "bind_subject_from_db",
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
