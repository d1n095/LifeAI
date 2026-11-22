"""Round 12 Phase 7: LIVING CORE STATE INTERFACE — the cognitive-state event contract
that Freebuff's Founder Console / Living Core renderer may consume.

SCOPE (deliberately minimal):
  - ONE versioned event schema (schema_version '1.0'), ONE derivation function from REAL
    system signals, ZERO frontend code. Freebuff owns rendering; this file owns the
    CONTRACT and the derivation.
  - The state names are OPERATIONAL METAPHORS, not emotion claims: 'reasoning' means
    "a bounded cognitive pipeline is running", 'waiting_for_approval' means "decisions
    sit undecided", etc. The contract never asserts MainAI has feelings — the states
    describe machine workload and authorization posture.
  - PRIVACY: an event carries state + confidence (optional) + coarse provenance keys
    (counts, ids of the SIGNAL entities, action names). It NEVER carries message text,
    prompt content, reasoning traces, model instructions, or security internals
    (redacted categories, not payloads).

Legitimate states (closed set — anything else is a contract violation):
  idle | listening | reasoning | researching | creating | responding |
  waiting_for_approval | recovering | security_attention

Derivation (deterministic, from the existing audit/tables — no new storage):
  security_attention  <- recent security audit events (rate_limit_hit, security_*), owner-scoped
  recovering          <- recent provider verification failures (provider_verification_checks)
  waiting_for_approval<- pending mainai_improvement_proposals for the owner
  researching         <- recent cognitive-gate run / evidence-chain activity
  creating            <- recent improvement_proposal_implemented / source-upload audit events
  responding          <- recent chat activity (chat_message audit family)
  reasoning           <- recent problem-learning decision activity
  listening           <- recent auth/session activity (the founder is present, no pipeline running)
  idle                <- nothing recent (the honest default — never a fabricated state)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

COGNITIVE_STATE_SCHEMA_VERSION = "1.0"

LEGITIMATE_STATES = frozenset({
    "idle", "listening", "reasoning", "researching", "creating", "responding",
    "waiting_for_approval", "recovering", "security_attention",
})

# The lookback windows are deliberately coarse; the renderer animates nothing here.
_RECENT = timedelta(minutes=15)

# audit actions that signal each state (owner-scoped where the table is owner-scoped)
_AUDIT_STATE_SIGNALS = [
    ("security_attention", ("rate_limit_hit", "security_boundary", "authz_denied", "prompt_injection_flagged")),
    ("creating", ("improvement_proposal_implemented", "source_created", "document_created")),
    ("researching", ("cognitive_gate_run_started", "evidence_chain_computed", "research_started")),
    ("responding", ("chat_message_sent", "chat_reply_generated")),
    ("reasoning", ("decision_recorded", "decision_superseded", "claim_relationship_declared")),
    ("listening", ("login_success", "session_started")),
]

_ALLOWED_PROVENANCE_KEYS = frozenset({"signal", "count", "entity_ids", "window_minutes", "schema_version"})


@dataclass
class CognitiveStateEvent:
    state: str
    confidence: float | None = None  # 0..1 where derivable; None where not
    provenance: dict = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: str = COGNITIVE_STATE_SCHEMA_VERSION

    def to_dict(self) -> dict:
        assert self.state in LEGITIMATE_STATES, f"illegitimate cognitive state: {self.state}"
        clean = {k: v for k, v in self.provenance.items() if k in _ALLOWED_PROVENANCE_KEYS}
        return {
            "schema_version": self.schema_version,
            "state": self.state,
            "confidence": self.confidence,
            "provenance": clean,
            "ts": self.ts.isoformat(),
        }


def derive_cognitive_state(db: Session, owner_id: uuid.UUID, now: datetime | None = None) -> CognitiveStateEvent:
    """Derive the CURRENT state from real signals only. Priority order: security first,
    then recovery, then authorization waits, then activity states, then idle. Every
    signal is coarse (counts/ids/action names) — no content ever leaves the system."""
    now = now or datetime.now(timezone.utc)
    since = now - _RECENT

    pending = db.execute(
        sa_text("SELECT count(*) FROM mainai_improvement_proposals WHERE owner_id = :o AND status = 'proposed'"),
        {"o": str(owner_id)},
    ).scalar()

    security_hits = db.execute(
        sa_text("SELECT count(*) FROM audit_log WHERE created_at > :since AND action = ANY(:acts)"),
        {"since": since, "acts": list(_AUDIT_STATE_SIGNALS[0][1])},
    ).scalar()

    provider_failures = db.execute(
        sa_text("SELECT count(*) FROM provider_verification_checks WHERE checked_at > :since AND result IN ('unreachable','invalid_key','rate_limited')"),
        {"since": since},
    ).scalar()

    activity_state, activity_prov = None, {"signal": None, "count": 0}
    for state, actions in _AUDIT_STATE_SIGNALS[1:]:
        hits = db.execute(
            sa_text("SELECT count(*) FROM audit_log WHERE created_at > :since AND action = ANY(:acts)"),
            {"since": since, "acts": list(actions)},
        ).scalar()
        if hits:
            activity_state, activity_prov = state, {"signal": actions[0], "count": int(hits), "window_minutes": 15}
            break

    if security_hits:
        return CognitiveStateEvent(
            state="security_attention", confidence=0.9,
            provenance={"signal": "security_audit_event", "count": int(security_hits), "window_minutes": 15},
        )
    if provider_failures:
        return CognitiveStateEvent(
            state="recovering", confidence=0.7,
            provenance={"signal": "provider_verification_failure", "count": int(provider_failures), "window_minutes": 15},
        )
    if pending:
        return CognitiveStateEvent(
            state="waiting_for_approval", confidence=0.95,
            provenance={"signal": "pending_proposals", "count": int(pending)},
        )
    if activity_state:
        return CognitiveStateEvent(
            state=activity_state, confidence=0.6, provenance=activity_prov,
        )
    return CognitiveStateEvent(state="idle", confidence=None, provenance={"signal": "no_recent_activity"})
