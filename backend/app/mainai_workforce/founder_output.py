"""Founder Output -- deltas, not noise. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md.

Reuses `app.mainai_cognitive_ops.founder_anti_repetition.assess_communication_necessity()` and
`app.mainai_cognitive_ops.founder_communication_ledger` verbatim -- this module does NOT
reimplement suppression logic; it only builds the workforce/coverage-domain status strings the
founder's own §18 examples describe, then asks the EXISTING filter whether they are worth
sending."""

from __future__ import annotations

from app.mainai_cognitive_ops.founder_anti_repetition import CommunicationNecessity, assess_communication_necessity


def build_coverage_delta_message(*, omitted_requirement_count: int, previous_completion_percent: float, new_completion_percent: float) -> str:
    return f"Historical audit found {omitted_requirement_count} valid omitted requirement(s); completion recalculated from {previous_completion_percent:.0f}% to {new_completion_percent:.0f}%."


def build_workforce_wait_message(*, waiting_for_agent: str, eta_minutes: float, held_agent: str) -> str:
    return f"{waiting_for_agent} is expected free in ~{eta_minutes:.0f} minute(s) and is materially better for the next critical task; {held_agent} remains available but is intentionally held."


def build_capability_stage_message(*, capability_key: str, new_stage_name: str, provider: str, new_provider_role: str) -> str:
    return f"Local MainAI reached {new_stage_name} for {capability_key}; {provider} can move from default builder to {new_provider_role} for this task class."


def evaluate_message_necessity(*, previous: dict | None, candidate_status: str, decision_now_required: bool = False, risk_changed: bool = False) -> CommunicationNecessity:
    return assess_communication_necessity(previous=previous, candidate_status=candidate_status, decision_now_required=decision_now_required, risk_changed=risk_changed)
