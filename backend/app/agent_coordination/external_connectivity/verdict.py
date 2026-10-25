"""Honest capability verdict for operational independence.

Local gateway function, green CI and a disconnected API prototype are not
completion. Unattended autonomy stays false while FA-9/CO-10, publication
controls or independent certification remain blocked.
"""

from __future__ import annotations

from typing import Any

from app.agent_coordination.durable import FIRST_CYCLE_GO
from app.agent_coordination.external_connectivity.catalog import SELECTED_FIRST_PATH
from app.agent_coordination.lifecycle import MISSING_MESSAGING, MISSING_PUBLICATION

MILESTONE = "verified_bounded_unattended_operating_cycle"
VERTICAL_SLICE_ID = "cursor_cloud_agents_assignment_result_v1"

CYCLE_REQUIREMENTS = (
    ("discover_and_prioritize_authorized_work", "Internal durable ledger and GitHub read-only intake exist. They do not dispatch to a connected external coding agent."),
    ("dispatch_to_genuinely_connected_external_agent", "Cursor Cloud Agents API v1 is the selected adapter, but live create/follow-up is hard-blocked. No external agent is connected."),
    ("verify_provider_side_receipt_and_execution_identity", "Identity classes distinguish local tokens from provider keys. No provider-authenticated receipt exists in this runtime."),
    ("collect_independently_verifiable_completion_evidence", "Official HMAC and GET contracts are implemented. No live FINISHED/ERROR evidence has been collected."),
    ("route_completed_work_to_independent_examination", "Examiner obligations can be prepared on the local ledger. Nothing completed by an external agent is available to route."),
    ("process_findings_and_dispatch_authorized_corrections", "Correction dispatch would reuse the same fail-closed adapter. Not live."),
    ("observe_exact_sha_ci_and_coordinate_certification", "Read-only GitHub intake can observe public exact-SHA CI. Certification remains examiner-owned and is not self-asserted."),
    ("recover_from_restart_duplicates_expired_credentials_temp_failures", "Durable ledger reconciliation exists locally. Provider-side retry/revocation is not authorized."),
    ("detect_stuck_agents_and_escalate_only_when_needed", "Stuck-agent detection against a live provider run is not implemented. Escalation must not invent progress."),
    ("report_progress_without_routine_supervision_requests", "Founder-ops snapshot can report local state. External-cycle exceptions cannot be reported because the cycle is not running."),
)


def vertical_slice() -> dict[str, Any]:
    """Smallest end-to-end path: one provider, one verified assignment/result cycle."""
    steps = [
        {
            "step": "founder_setup",
            "required": True,
            "live": False,
            "missing_capability": "founder_cursor_cloud_agents_authorization",
            "note": "Scoped Cursor API key, spend bound, expiry and revocation. Not a local bridge token.",
        },
        {
            "step": "discover_one_authorized_assignment",
            "required": True,
            "live": False,
            "reuse": "DurableCoordinationService + GitHub read-only intake",
        },
        {
            "step": "dispatch_create_cloud_agent",
            "required": True,
            "live": False,
            "interface": "POST /v1/agents",
            "missing_capability": "founder_cursor_cloud_live_mutate_authorization",
        },
        {
            "step": "verify_provider_receipt",
            "required": True,
            "live": False,
            "interface": "GET /v1/agents/{id} with the same provider key",
            "identity_class": "cursor_cloud_api_key_candidate",
        },
        {
            "step": "collect_completion_evidence",
            "required": True,
            "live": False,
            "interface": "GET run and/or v0 HMAC statusChange FINISHED/ERROR",
        },
        {
            "step": "bind_to_existing_ledger",
            "required": True,
            "live": False,
            "reuse": "persist_owner_ledger + delivery states; persisted=False until live evidence exists",
        },
        {
            "step": "route_to_independent_examiner",
            "required": True,
            "live": False,
            "note": "Examiner identity must be distinct from the builder. Builder cannot set independently_verified.",
        },
        {
            "step": "observe_exact_sha_ci",
            "required": True,
            "live": False,
            "reuse": "GitHubDevelopmentIntake public GET",
        },
    ]
    return {
        "slice_id": VERTICAL_SLICE_ID,
        "provider": "cursor",
        "path_id": SELECTED_FIRST_PATH,
        "additional_providers": [],
        "why_only_one_provider": (
            "Do not expand to Claude Code or Codex until this Cursor Cloud Agents "
            "assignment/result cycle is live and independently evidenced."
        ),
        "replaceable_adapter": True,
        "provider_cannot_redefine_authority": True,
        "steps": steps,
        "complete": False,
        "vendor_delivery_claimed": False,
        "first_cycle_go": FIRST_CYCLE_GO,
    }


def unattended_trial_plan() -> dict[str, Any]:
    """Acceptance contract for a 72-hour unattended trial. The trial cannot start yet."""
    return {
        "name": "72h_unattended_operating_trial",
        "duration_hours": 72,
        "start_authorized": False,
        "zero_routine_founder_message_forwarding": True,
        "restricted_actions_remain_blocked": True,
        "first_cycle_go": FIRST_CYCLE_GO,
        "unattended_autonomy_ready": False,
        "cannot_start_while": [
            "no_live_provider_authenticated_external_agent",
            "vertical_slice_incomplete",
            "fa9_co10_privilege_and_publication_gates_unsatisfied",
            "independent_certification_blocked",
            "founder_live_mutate_unauthorized",
            "first_cycle_go_false",
        ],
        "required_coverage": [
            {
                "id": "multiple_assignments",
                "need": "At least two distinct authorized assignments dispatched to the same connected Cursor Cloud Agent path.",
            },
            {
                "id": "independent_review",
                "need": "Each completion routed to an examiner identity distinct from the builder.",
            },
            {
                "id": "authorized_corrections",
                "need": "At least one examiner finding becomes a follow-up run under the same provider key and ledger binding.",
            },
            {
                "id": "process_restart",
                "need": "Worker/process restart mid-cycle; in-flight assignment resumes from durable state without duplicate vendor create.",
            },
            {
                "id": "duplicate_events",
                "need": "Replayed webhook or GET is idempotent; no second assignment or second completion.",
            },
            {
                "id": "expired_credentials",
                "need": "Expired or revoked provider key fails closed, escalates once, and does not fabricate receipt.",
            },
            {
                "id": "temporary_failure",
                "need": "Bounded retry on 5xx/timeout; no paid replan storm; no invented success.",
            },
            {
                "id": "stuck_agent_escalation",
                "need": "A silent/stuck provider run escalates only after automated recovery and existing authority are insufficient.",
            },
            {
                "id": "founder_reporting",
                "need": "Progress, exceptions and decisions appear on Founder-ops without a routine 'please forward this' request.",
            },
        ],
        "success_metric": (
            "Completed, independently verifiable work without Founder babysitting — "
            "not the number of components implemented."
        ),
        "codex_owned_gates_remain_authoritative": [
            "FA-9",
            "CO-10",
            "STOP",
            "privilege",
            "publication",
        ],
    }


def capability_verdict() -> dict[str, Any]:
    requirements = [
        {
            "id": index + 1,
            "name": name,
            "satisfied": False,
            "evidence": evidence,
        }
        for index, (name, evidence) in enumerate(CYCLE_REQUIREMENTS)
    ]
    return {
        "milestone": MILESTONE,
        "ready": False,
        "unattended_operating_cycle_ready": False,
        "unattended_autonomy_ready": False,
        "local_gateway_sufficient": False,
        "green_ci_sufficient": False,
        "disconnected_prototype_sufficient": False,
        "vendor_delivery_claimed": False,
        "independently_verified": False,
        "first_cycle_go": FIRST_CYCLE_GO,
        "external_agent_connected": False,
        "provider_neutral_orchestration": True,
        "single_vendor_mandatory": False,
        "external_agents_cannot_redefine_authority": True,
        "missing_capability": MISSING_MESSAGING,
        "publication_missing_capability": MISSING_PUBLICATION,
        "blocking_gates": [
            "no_provider_authenticated_external_agent",
            "founder_cursor_cloud_live_mutate_authorization",
            "fa9_co10_privilege_and_publication",
            "independent_certification",
            "first_cycle_go_false",
        ],
        "requirements": requirements,
        "vertical_slice": vertical_slice(),
        "unattended_trial": unattended_trial_plan(),
        "inherited_release_blocker": {
            "name": "frontend_npm_audit_zod_ghsa_mrhx",
            "on_this_branch": True,
            "note": (
                "This #303-based branch does not include the #305 zod pin. "
                "Do not suppress the audit or mix that security patch into this PR."
            ),
        },
        "amends_303_through_306": False,
        "duplicates_claude_or_codex_work": False,
        "verdict": (
            "NOT READY. #307 proves official-interface feasibility and a fail-closed "
            "Cursor Cloud Agents prototype. It does not connect an external agent, "
            "does not complete a verified assignment/result cycle, and does not "
            "authorize a 72-hour unattended trial."
        ),
    }
