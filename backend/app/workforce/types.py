"""Workforce Foundation vocabulary and invariants.

AGENT ROLE != AUTHORITY.
MODEL CAPABILITY != ACCESS.
PROVIDER != TRUSTED INTERNAL STATE.
PAST AGENT SUCCESS != FUTURE AUTHORITY.
DELEGATION != AUTHORIZATION.
EXTERNAL MODEL OUTPUT != TRUSTED FACT.
CONFIDENCE != PERFORMANCE EVIDENCE.
SAID != IMPLEMENTED.
"""

from __future__ import annotations

# Extensible string vocabularies — never hard-code product behavior on these names alone.
# Callers may use other agent_type / trust_zone values; known constants are documentation
# and selector hints, not a closed schema enum at the SQL layer (except status CHECKs).

KNOWN_AGENT_TYPES: frozenset[str] = frozenset(
    {
        "INTERNAL_SPECIALIST",
        "LOCAL_MODEL",
        "EXTERNAL_PROVIDER",
        "TEMPORARY_SUBAGENT",
        "VERIFIER",
        "RED_TEAM",
        "RESEARCH",
        "PLANNER",
        "MEMORY",
        "CODING",
        "SECURITY",
        "FINANCE",
        "DOMAIN_SPECIALIST",
    }
)

KNOWN_TRUST_ZONES: frozenset[str] = frozenset(
    {
        "LOCAL_INTERNAL",
        "CONTROLLED_INTERNAL",
        "EXTERNAL_PROVIDER",
        "UNTRUSTED_REMOTE",
    }
)

AGENT_LIFECYCLE_STATUSES: frozenset[str] = frozenset(
    {
        "need_detected",
        "candidate",
        "sandbox",
        "probation",
        "active",
        "disabled",
        "retired",
    }
)

VERIFICATION_STATUSES: frozenset[str] = frozenset(
    {
        "UNVERIFIED",
        "CHECKED",
        "VERIFIED",
        "REJECTED",
        "SUPERSEDED",
    }
)

# Objects that must never auto-enter an external context package.
FORBIDDEN_EXTERNAL_DISCLOSURE_KINDS: frozenset[str] = frozenset(
    {
        "vault",
        "secret",
        "api_key",
        "provider_credential",
        "full_founder_memory",
        "full_conversation_history",
        "raw_sensitive_identity",
        "system_secret",
        "other_agent_private_package",
    }
)

INVARIANTS: tuple[str, ...] = (
    "AGENT_ROLE_NE_AUTHORITY",
    "MODEL_CAPABILITY_NE_ACCESS",
    "PROVIDER_NE_TRUSTED_INTERNAL_STATE",
    "PAST_AGENT_SUCCESS_NE_FUTURE_AUTHORITY",
    "DELEGATION_NE_AUTHORIZATION",
    "EXTERNAL_MODEL_OUTPUT_NE_TRUSTED_FACT",
    "CONFIDENCE_NE_PERFORMANCE_EVIDENCE",
    "BUILDER_CANNOT_SELF_VERIFY",
)
