"""Evidence-only completeness projection for project-work recommendations.

This reuses the executive priority engine but never fills missing factors with its dataclass
defaults. A score is emitted only when every factor is explicitly sourced and current.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone
from math import isfinite
from typing import Any

from app.mainai_executive.priority import PriorityFactors, score_priority

PRIORITY_DIMENSIONS = tuple(
    field.name for field in fields(PriorityFactors) if field.name != "founder_override"
)
MANDATORY_CONSTRAINTS = ("security", "authorization", "dependencies", "founder_approval")


def _has_current_source(record: dict[str, Any], now: datetime) -> bool:
    if not record.get("source_ref") or not record.get("observed_at"):
        return False
    if str(record.get("freshness_status") or "").upper() != "CURRENT":
        return False
    try:
        observed_at = datetime.fromisoformat(str(record["observed_at"]).replace("Z", "+00:00"))
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        if observed_at.astimezone(timezone.utc) > now:
            return False
        valid_until = record.get("valid_until")
        if not valid_until:
            return True
        expiry = datetime.fromisoformat(str(valid_until).replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry.astimezone(timezone.utc) >= now
    except (TypeError, ValueError):
        return False


def assess_decision_completeness(
    *,
    provenance: dict[str, Any] | None,
    category: str = "project_work_priority",
) -> dict[str, Any]:
    provenance = provenance if isinstance(provenance, dict) else {}
    recorded_factors = provenance.get("priority_factors")
    recorded_factors = recorded_factors if isinstance(recorded_factors, dict) else {}
    evaluated: list[dict[str, Any]] = []
    missing: list[str] = []
    stale: list[str] = []
    freshness_unknown: list[str] = []
    not_applicable: list[str] = []
    assumptions: list[str] = []
    unknown_impact: list[str] = []
    values: dict[str, float] = {}
    now = datetime.now(timezone.utc)

    for name in PRIORITY_DIMENSIONS:
        entry = recorded_factors.get(name)
        if not isinstance(entry, dict):
            missing.append(name)
            unknown_impact.append(name)
            continue
        value = entry.get("value")
        source_ref = entry.get("source_ref")
        observed_at = entry.get("observed_at")
        if entry.get("assumption") is True:
            assumptions.append(name)
        freshness_status = str(entry.get("freshness_status") or "").upper()
        if entry.get("stale") is True or freshness_status == "STALE":
            stale.append(name)
            unknown_impact.append(name)
            continue
        if freshness_status == "NOT_APPLICABLE":
            not_applicable.append(name)
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            missing.append(name)
            unknown_impact.append(name)
            continue
        try:
            numeric_value = float(value)
        except (OverflowError, TypeError, ValueError):
            assumptions.append(name)
            unknown_impact.append(name)
            continue
        if not isfinite(numeric_value) or not 0 <= numeric_value <= 1:
            assumptions.append(name)
            unknown_impact.append(name)
            continue
        if not source_ref or not observed_at:
            assumptions.append(name)
            unknown_impact.append(name)
            continue
        try:
            timestamp = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            if timestamp.astimezone(timezone.utc) > now:
                raise ValueError("Evidence timestamp is in the future.")
            valid_until = entry.get("valid_until")
            expiry = (
                datetime.fromisoformat(str(valid_until).replace("Z", "+00:00"))
                if valid_until
                else None
            )
            if expiry is not None and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            expired = bool(expiry and expiry.astimezone(timezone.utc) < now)
        except (TypeError, ValueError):
            assumptions.append(name)
            unknown_impact.append(name)
            continue
        if expired:
            stale.append(name)
            unknown_impact.append(name)
            continue
        if freshness_status != "CURRENT":
            freshness_unknown.append(name)
            unknown_impact.append(name)
            continue
        if entry.get("assumption") is True:
            unknown_impact.append(name)
            continue
        values[name] = numeric_value
        evaluated.append(
            {
                "name": name,
                "value": numeric_value,
                "source_ref": str(source_ref),
                "observed_at": timestamp.isoformat(),
            }
        )

    constraints = provenance.get("constraints")
    constraint_states = {}
    for name in MANDATORY_CONSTRAINTS:
        entry = constraints.get(name) if isinstance(constraints, dict) else None
        if name == "founder_approval" and category == "read_only_observation":
            constraint_states[name] = {
                "status": "NOT_APPLICABLE",
                "reason": "This endpoint only observes project state; it does not execute or approve work.",
            }
        elif not isinstance(entry, dict):
            constraint_states[name] = {"status": "NOT_CHECKED"}
        elif entry.get("status") == "NOT_APPLICABLE" and entry.get("reason"):
            constraint_states[name] = {
                "status": "NOT_APPLICABLE",
                "reason": str(entry["reason"]),
            }
        elif (
            entry.get("status") == "CHECKED"
            and _has_current_source(entry, now)
        ):
            constraint_states[name] = {
                "status": "CHECKED",
                "source_ref": str(entry["source_ref"]),
                "observed_at": str(entry["observed_at"]),
            }
        else:
            constraint_states[name] = {"status": "NOT_CHECKED"}
    alternatives = provenance.get("alternatives")
    alternatives_evaluated = (
        isinstance(alternatives, list)
        and bool(alternatives)
        and all(
            isinstance(item, dict)
            and _has_current_source(item, now)
            for item in alternatives
        )
    )
    alternatives_status = "EVALUATED" if alternatives_evaluated else "NOT_CHECKED"
    override_entry = provenance.get("founder_override")
    founder_override = None
    if isinstance(override_entry, dict):
        override_value = override_entry.get("value")
        allowed_overrides = {"NOW", "NEAR", "MID", "LONG", "LATER", "OPTIONAL", "BLOCKED"}
        if (
            isinstance(override_value, str)
            and override_value.upper() in allowed_overrides
            and _has_current_source(override_entry, now)
            and override_entry.get("assumption") is not True
        ):
            founder_override = override_value.upper()
        else:
            assumptions.append("founder_override")
            unknown_impact.append("founder_override")
    elif override_entry:
        assumptions.append("founder_override")
        unknown_impact.append("founder_override")
    score = None
    priority = None
    explanation = None
    complete = (
        len(values) == len(PRIORITY_DIMENSIONS)
        and not stale
        and not freshness_unknown
        and not assumptions
        and all(item["status"] in {"CHECKED", "NOT_APPLICABLE"} for item in constraint_states.values())
        and alternatives_status == "EVALUATED"
    )
    if len(values) == len(PRIORITY_DIMENSIONS) and not stale and not freshness_unknown and not assumptions:
        factors = PriorityFactors(**values, founder_override=founder_override)
        priority, score, explanation = score_priority(factors)
    return {
        "category": category,
        "status": "COMPLETE" if complete else ("INCOMPLETE" if recorded_factors else "NOT_CHECKED"),
        "factors_evaluated": evaluated,
        "factors_missing": sorted(set(missing)),
        "factors_stale": sorted(set(stale)),
        "factors_freshness_unknown": sorted(set(freshness_unknown)),
        "factors_not_applicable": sorted(set(not_applicable)),
        "factors_assumed_or_unsourced": sorted(set(assumptions)),
        "impact_unknown_for": sorted(set(unknown_impact)),
        "mandatory_constraints": constraint_states,
        "alternatives": {
            "status": alternatives_status,
            "items": alternatives if isinstance(alternatives, list) else [],
        },
        "priority": {
            "status": "SCORED" if score is not None else "NOT_SCORED_INCOMPLETE_EVIDENCE",
            "horizon": priority,
            "score": score,
            "explanation": explanation,
            "engine": "app.mainai_executive.priority.score_priority",
        },
        "reevaluation": {
            "status": "NOT_VERSIONED",
            "previous_decision_ref": None,
            "evidence_delta": [],
            "reason": "This read-only snapshot does not persist recommendation versions.",
        },
        "research": {
            "status": "NOT_ENABLED",
            "reason": "No external research is initiated by the operations-center snapshot.",
        },
        "authority_granted": False,
    }
