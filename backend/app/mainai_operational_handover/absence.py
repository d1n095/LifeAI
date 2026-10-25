"""Bounded Founder-absence operating policy.

Silence is never approval. Recoverable failures continue. Approval/restriction
pauses only the affected operation. No indefinite retries or uncontrolled spend.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

FIRST_CYCLE_GO = False
MAX_RECOVERABLE_RETRIES = 3
LOW_RISK = frozenset({"read_only_audit", "index", "reconcile", "status", "observe"})
HIGH_RISK = frozenset({"deploy", "publish", "spend", "grant", "live_mutate", "github_write"})


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def evaluate_absence_tick(
    *,
    items: Iterable[dict[str, Any]],
    stop_blocked: bool,
    standing_grants: Iterable[dict[str, Any]] | None = None,
    spend_exhausted: bool = False,
    absence_enabled: bool = False,
) -> dict[str, Any]:
    grants = list(standing_grants or [])
    grant_scopes = {str(row.get("scope") or "") for row in grants if row.get("active")}
    continued: list[dict[str, Any]] = []
    paused: list[dict[str, Any]] = []
    deferred_new: list[dict[str, Any]] = []
    recovered: list[dict[str, Any]] = []

    if stop_blocked:
        return {
            "absence_enabled": absence_enabled,
            "stop_blocked": True,
            "continued": [],
            "paused": [{"reason": "STOP", "scope": "all"}],
            "deferred_new": [],
            "recovered": [],
            "silence_is_not_approval": True,
            "uncontrolled_spend": False,
            "indefinite_retry": False,
            "first_cycle_go": FIRST_CYCLE_GO,
        }

    for item in items:
        kind = str(item.get("kind") or "task")
        risk = str(item.get("risk") or "unknown")
        approved = bool(item.get("approved"))
        needs_approval = bool(item.get("needs_approval"))
        restriction = item.get("restriction")
        retries = int(item.get("retries") or 0)
        is_new = bool(item.get("new_work"))
        recoverable = bool(item.get("recoverable_failure"))

        if needs_approval and not approved:
            paused.append({**item, "pause_reason": "approval_required", "silence_is_not_approval": True})
            continue
        if restriction and str(restriction.get("workaround_class") or "") in {"prohibited", "uncertain"}:
            paused.append({**item, "pause_reason": "restriction", "affected_only": True})
            continue
        if risk in HIGH_RISK:
            paused.append({**item, "pause_reason": "high_risk_requires_founder"})
            continue
        if spend_exhausted and item.get("costs_provider"):
            paused.append({**item, "pause_reason": "spend_exhausted"})
            continue
        if recoverable:
            if retries >= MAX_RECOVERABLE_RETRIES:
                paused.append({**item, "pause_reason": "retry_budget_exhausted"})
                continue
            recovered.append(item)
        if is_new:
            scope = str(item.get("scope") or kind)
            if scope not in grant_scopes and risk not in LOW_RISK:
                deferred_new.append({**item, "reason": "no_standing_grant"})
                continue
        continued.append(item)

    return {
        "absence_enabled": absence_enabled,
        "stop_blocked": False,
        "continued": continued,
        "paused": paused,
        "deferred_new": deferred_new,
        "recovered": recovered,
        "other_eligible_continues": True,
        "silence_is_not_approval": True,
        "uncontrolled_spend": False,
        "indefinite_retry": False,
        "max_recoverable_retries": MAX_RECOVERABLE_RETRIES,
        "evaluated_at": _iso(),
        "first_cycle_go": FIRST_CYCLE_GO,
        "vendor_delivery_claimed": False,
    }


def founder_catchup_report(evaluation: dict[str, Any]) -> dict[str, Any]:
    continued = evaluation.get("continued") or []
    paused = evaluation.get("paused") or []
    deferred = evaluation.get("deferred_new") or []
    return {
        "kind": "founder_return_catchup",
        "completed_or_continued": [{"id": row.get("id"), "title": row.get("title")} for row in continued],
        "paused_needing_founder": [
            {
                "id": row.get("id"),
                "title": row.get("title"),
                "reason": row.get("pause_reason") or row.get("reason"),
            }
            for row in paused + deferred
        ],
        "could_not_accomplish": [row.get("title") or row.get("id") for row in paused + deferred],
        "silence_was_not_treated_as_approval": True,
        "routine_alerts_suppressed": True,
        "first_cycle_go": FIRST_CYCLE_GO,
        "generated_at": _iso(),
    }


def items_from_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Project founder-ops rows into absence items. Does not invent work."""
    items: list[dict[str, Any]] = []
    for row in snapshot.get("independent_ready_work") or []:
        items.append(
            {
                "id": row.get("id") or row.get("task_id"),
                "title": row.get("title") or row.get("description") or "ready_work",
                "kind": "task",
                "risk": "read_only_audit",
                "approved": True,
                "needs_approval": False,
                "new_work": False,
            }
        )
    for row in snapshot.get("authorization_requests") or []:
        items.append(
            {
                "id": row.get("id"),
                "title": row.get("title") or "authorization",
                "kind": "approval",
                "risk": "grant",
                "approved": False,
                "needs_approval": True,
                "new_work": True,
            }
        )
    return items
