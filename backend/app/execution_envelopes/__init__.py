"""Deterministic recording/query API for Life's execution authorization envelope staging
layer -- the boundary between a proposed execution scope (MainAI's own suggestion) and real,
founder-granted execution authority for a MainAIGoal. See `service.py`'s own module docstring
for the full doctrine."""

from app.execution_envelopes.service import (
    ExecutionEnvelopeError,
    authorize_execution_scope,
    get_current_execution_envelope,
    get_execution_authorization_envelope,
    get_execution_scope_proposal,
    list_execution_authorization_envelopes,
    list_execution_scope_proposals,
    list_unreviewed_execution_scope_proposals,
    propose_execution_scope,
    reject_execution_scope,
)

__all__ = [
    "ExecutionEnvelopeError",
    "authorize_execution_scope",
    "get_current_execution_envelope",
    "get_execution_authorization_envelope",
    "get_execution_scope_proposal",
    "list_execution_authorization_envelopes",
    "list_execution_scope_proposals",
    "list_unreviewed_execution_scope_proposals",
    "propose_execution_scope",
    "reject_execution_scope",
]
