"""Temporary team formation patterns (T8). No automatic cross-agent context sharing."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.workforce import WorkforceTeam
from app.workforce.broker import DelegationBrokerError, form_team
from app.workforce.context import create_context_package
from app.models.workforce import WorkforceContextPackage


KNOWN_TEAM_PATTERNS: frozenset[str] = frozenset(
    {
        "BUILDER_VERIFIER",
        "RESEARCHER_FACT_CHECKER",
        "PLANNER_RED_TEAM",
        "CODER_TESTER_SECURITY",
        "MULTIPLE_SOLUTIONS_COMPARATOR",
    }
)


@dataclass(frozen=True)
class TeamMemberPackage:
    profile_id: uuid.UUID
    package: WorkforceContextPackage


def form_pattern_team(
    db: Session,
    *,
    owner_id: uuid.UUID,
    pattern: str,
    member_profile_ids: list[uuid.UUID],
    name: str | None = None,
) -> WorkforceTeam:
    if pattern not in KNOWN_TEAM_PATTERNS:
        # Extensible: unknown patterns allowed but must be explicit strings.
        if not pattern or len(pattern) > 64:
            raise DelegationBrokerError(f"invalid team pattern: {pattern}")
    expected = {
        "BUILDER_VERIFIER": 2,
        "RESEARCHER_FACT_CHECKER": 2,
        "PLANNER_RED_TEAM": 2,
        "CODER_TESTER_SECURITY": 3,
        "MULTIPLE_SOLUTIONS_COMPARATOR": None,  # 2+ builders + comparator
    }.get(pattern)
    if expected is not None and len(member_profile_ids) != expected:
        raise DelegationBrokerError(f"pattern {pattern} expects {expected} members, got {len(member_profile_ids)}")
    if pattern == "MULTIPLE_SOLUTIONS_COMPARATOR" and len(member_profile_ids) < 3:
        raise DelegationBrokerError("MULTIPLE_SOLUTIONS_COMPARATOR needs >=2 solvers + comparator")
    return form_team(
        db,
        owner_id=owner_id,
        name=name or pattern.lower().replace("_", "+"),
        pattern=pattern,
        member_profile_ids=member_profile_ids,
    )


def package_context_per_member(
    db: Session,
    *,
    owner_id: uuid.UUID,
    team: WorkforceTeam,
    member_items: dict[uuid.UUID, list[dict]],
) -> list[TeamMemberPackage]:
    """Each member gets its own minimized package. No automatic sharing."""
    out: list[TeamMemberPackage] = []
    member_ids = [uuid.UUID(str(x)) for x in (team.member_profile_ids or [])]
    for pid in member_ids:
        items = list(member_items.get(pid, []))
        # Refuse stuffing another member's private refs without explicit per-member list.
        pkg = create_context_package(
            db,
            owner_id=owner_id,
            trust_zone="CONTROLLED_INTERNAL",
            requested_items=items,
            provenance={
                "team_id": str(team.id),
                "profile_id": str(pid),
                "shared_automatically": False,
            },
        )
        out.append(TeamMemberPackage(profile_id=pid, package=pkg))
    # Invariant: distinct fingerprints when contents differ.
    return out


def assert_no_automatic_cross_context(packages: list[TeamMemberPackage]) -> None:
    fingerprints = [p.package.content_fingerprint for p in packages]
    # Identical empty packages may share fingerprint — that's ok; leakage is about items.
    traces = []
    for p in packages:
        traces.append({item.get("trace_id") for item in (p.package.items or []) if item.get("trace_id")})
    for i, a in enumerate(traces):
        for j, b in enumerate(traces):
            if i >= j:
                continue
            overlap = a & b
            if overlap:
                raise DelegationBrokerError(
                    f"cross-agent context leak: shared trace_ids {overlap} between members"
                )
