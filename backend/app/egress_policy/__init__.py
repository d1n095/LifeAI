"""Life Vault / External-AI Egress Control -- default-deny policy gate for outbound provider
calls. See docs/LIFE_VAULT_EGRESS_CONTROL.md."""

from app.egress_policy.service import EgressDeniedError, enforce_egress_policy

__all__ = ["EgressDeniedError", "enforce_egress_policy"]
