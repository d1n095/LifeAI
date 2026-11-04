"""Resource Intelligence -> Provider Economics bridge. See
docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md. Closes P1 #3 from the Research, Truth &
Advisory Intelligence round's own handoff doc.

Real, DB-composing derivation of `app.mainai_research.provider_economics.
ProviderEconomicsSignal` from `app.resource_intelligence`'s own real functions
(`agent_efficiency_profile`, `provider_quota_remaining`) -- a caller no longer has to
hand-reconstruct this mapping themselves. Never a second cost/quota ledger; every number here
is read from the same `resource_intelligence` source those functions already own.

UNKNOWN != ZERO: a `missing_data=True` envelope maps to `None` on the signal, never `0.0`.
MISSING != FREE: a missing quota fraction maps to `None`, never treated as "unlimited quota
remaining". ADVISORY != AUTHORITY: this module returns a `ProviderEconomicsSignal` for
`provider_economics.recommend_provider_action()` to reason over -- it never itself authorizes a
provider change."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.mainai_research.provider_economics import ProviderEconomicsSignal
from app.resource_intelligence.efficiency_profile import agent_efficiency_profile
from app.resource_intelligence.quota import provider_quota_remaining


def _envelope_value(envelope) -> float | None:
    if envelope is None or envelope.missing_data or envelope.value is None:
        return None
    return float(envelope.value)


def _min_known_quota_fraction(quota: dict) -> float | None:
    known = [float(e.value) for e in quota.values() if not e.missing_data and e.value is not None]
    return min(known) if known else None


def derive_provider_economics_signal(
    db: Session,
    *,
    owner_id: uuid.UUID,
    provider_id: str,
    agent_id: uuid.UUID,
    goal_id: uuid.UUID | None = None,
    task_type: str | None = None,
    unique_capability: bool = False,
    strategic_fallback_value: bool = False,
) -> ProviderEconomicsSignal:
    """`agent_id` is the caller-known agent instance that runs on `provider_id` -- this bridge
    does not itself maintain a provider-to-agent registry (that stays with
    `app.agent_coordination`); the caller supplies the mapping it already knows."""

    profile = agent_efficiency_profile(db, owner_id=owner_id, agent_id=agent_id, task_type=task_type)
    accepted = profile["accepted_commit_rate"]
    rework = profile["rework_rate"]
    cost = profile["cost_per_accepted_commit"]

    observation_count = accepted.sample_size if accepted.sample_size is not None else 0

    quota_remaining_fraction = None
    if goal_id is not None:
        quota = provider_quota_remaining(db, owner_id=owner_id, goal_id=goal_id)
        quota_remaining_fraction = _min_known_quota_fraction(quota)

    return ProviderEconomicsSignal(
        provider_id=provider_id,
        observation_count=observation_count,
        cost_per_accepted_commit_usd=_envelope_value(cost),
        rework_rate=_envelope_value(rework),
        accepted_commit_rate=_envelope_value(accepted),
        quota_remaining_fraction=quota_remaining_fraction,
        unique_capability=unique_capability,
        strategic_fallback_value=strategic_fallback_value,
    )
