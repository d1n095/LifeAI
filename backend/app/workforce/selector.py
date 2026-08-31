"""Bounded agent selection / routing (T7). Scores from recorded evidence, not self-claims."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.workforce import WorkforceAgentProfile, WorkforcePerformanceRollup
from app.workforce.performance import verified_success_rate
from app.workforce.registry import assert_agent_selectable

EXTERNAL_ZONES = frozenset({"EXTERNAL_PROVIDER", "UNTRUSTED_REMOTE"})


@dataclass(frozen=True)
class CandidateScore:
    profile_id: uuid.UUID
    agent_key: str
    score: float
    explanation: dict


class SelectorError(Exception):
    pass


def _cost_penalty(cost_class: str) -> float:
    return {"low": 0.0, "medium": 0.15, "high": 0.35, "unknown": 0.2}.get(cost_class, 0.2)


def score_candidates(
    db: Session,
    *,
    owner_id: uuid.UUID,
    required_capability: str,
    risk: str = "low",
    data_sensitivity: str = "internal",
    prefer_local_only: bool = False,
    cost_ceiling_usd: float | None = None,
) -> list[CandidateScore]:
    profiles = list(
        db.execute(
            select(WorkforceAgentProfile).where(
                WorkforceAgentProfile.owner_id == owner_id,
                WorkforceAgentProfile.status.in_(("active", "probation", "sandbox")),
            )
        ).scalars()
    )
    scored: list[CandidateScore] = []
    for profile in profiles:
        try:
            assert_agent_selectable(profile)
        except Exception:
            continue
        caps = list(profile.capability_tags or [])
        if required_capability not in caps and caps:
            # Allow empty-tag profiles only as weak fallbacks.
            continue
        if prefer_local_only and profile.trust_zone in EXTERNAL_ZONES:
            continue
        if data_sensitivity in ("vault", "secret", "high") and profile.trust_zone in EXTERNAL_ZONES:
            continue

        rollup = db.execute(
            select(WorkforcePerformanceRollup).where(
                WorkforcePerformanceRollup.owner_id == owner_id,
                WorkforcePerformanceRollup.profile_id == profile.id,
                WorkforcePerformanceRollup.capability_tag == required_capability,
            )
        ).scalar_one_or_none()

        success_rate = verified_success_rate(rollup) if rollup else None
        # No evidence → low prior, never "trusted because new".
        evidence_score = 0.25 if success_rate is None else (0.4 + 0.6 * success_rate)
        capability_match = 1.0 if required_capability in caps else 0.2
        # Read-only self-model signal — CAPABILITY_REALITY != AUTHORITY; never invents status.
        reality_factor = 1.0
        reality_status = None
        try:
            from app.capability_reality import get_capability_reality

            reality = get_capability_reality(
                db, owner_id=owner_id, capability_key=required_capability
            )
            if reality is not None:
                reality_status = reality.status
                if reality.status == "verified_available":
                    reality_factor = 1.0
                elif reality.status in ("configured_unavailable", "configured_disabled"):
                    reality_factor = 0.35
                else:
                    # unknown / planned — downrank, do not refuse alone
                    reality_factor = 0.6
        except Exception:
            reality_factor = 1.0
        risk_fit = 1.0
        if risk == "high" and profile.risk_tier == "low":
            risk_fit = 0.5
        local_bonus = 0.1 if profile.trust_zone in ("LOCAL_INTERNAL", "CONTROLLED_INTERNAL") else 0.0
        penalty = _cost_penalty(profile.cost_class)
        if cost_ceiling_usd is not None and cost_ceiling_usd <= 0 and profile.cost_class == "high":
            continue
        if rollup and int(rollup.security_violations) + int(rollup.authority_violations) > 0:
            penalty += 0.5

        score = capability_match * evidence_score * risk_fit * reality_factor + local_bonus - penalty
        explanation = {
            "capability_match": capability_match,
            "evidence_score": evidence_score,
            "verified_success_rate": success_rate,
            "jobs_with_evidence": (
                int(rollup.verified_success) + int(rollup.verified_failure) if rollup else 0
            ),
            "risk_fit": risk_fit,
            "local_bonus": local_bonus,
            "cost_penalty": penalty,
            "trust_zone": profile.trust_zone,
            "status": profile.status,
            "capability_reality_status": reality_status,
            "capability_reality_factor": reality_factor,
            # Explicit: no self-reported confidence enters this score.
            "used_agent_self_confidence": False,
            "capability_reality_is_not_authority": True,
        }
        scored.append(
            CandidateScore(
                profile_id=profile.id,
                agent_key=profile.agent_key,
                score=score,
                explanation=explanation,
            )
        )
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored


def select_best_candidate(
    db: Session,
    *,
    owner_id: uuid.UUID,
    required_capability: str,
    **kwargs,
) -> CandidateScore:
    ranked = score_candidates(db, owner_id=owner_id, required_capability=required_capability, **kwargs)
    if not ranked:
        raise SelectorError(f"no selectable workforce agent for capability={required_capability}")
    return ranked[0]
