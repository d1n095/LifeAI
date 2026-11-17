"""MainAI-invoked OpenAI consultation — the orchestration-facing seam.

This is how MainAI's existing orchestration (workforce assignments via
app/workforce/provider_worker.py's consultation_mode) consults OpenAI when AUTHORIZED,
without the founder manually forwarding messages. It is a thin authorization wrapper over
the SAME gated core the founder HTTP path uses (app/openai_consult/service.consult_openai)
— deliberately NOT a second invocation stack: identical egress gate, identical budget
store, identical safe error mapping, identical Responses API call.

What this wrapper adds on top of the founder path (every gate fail-closed, BEFORE any
network touch):

1. RECURSION GUARDS — a consultation may never trigger another consultation:
   * a ContextVar marks the whole pipeline; a nested consult_for_mainai inside a
     consultation's own processing is refused outright;
   * `chain_depth` must be 0 — no caller can request a chained consultation;
   * the sync wrapper (consult_for_mainai_sync, used by the sync workforce harness)
     refuses if an event loop is already running in this thread — a consultation kicked
     off from inside async consultation processing cannot even start.
2. INDEPENDENT AUTHORIZATION — openai_consult_mainai_enabled must be true (separate from
   the founder HTTP path so either capability can be disabled alone), AND the workforce
   activation gates must be verified (require_activation_allowed — UNKNOWN != VERIFIED,
   fail-closed). Both reuses and respects MainAI's existing provider-activation model;
   the staged-provider workstream in provider_worker is untouched.
3. NO TOOLS — MainAI consultations structurally never attach web_search or any other
   tool (the ConsultIn is built internally with allow_web_search=False; there is no
   parameter to change it).
4. NO RETRIEVAL AUTHORITY — the ONLY content that can ever be sent is the explicit
   goal_text/instructions strings handed in by the caller. This module imports no
   Document/Memory/knowledge/workforce-context models and performs no reads: retrieval
   authority is NOT egress authority (docs/LIFE_VAULT_EGRESS_CONTROL.md's invariant).
   Context handed in is also credential-scrubbed (assert_no_credentials_in_context).
5. AUDITABILITY — egress disclosure events carry purpose="mainai_consult" plus the
   assignment's goal/task/job ids when provided, so every consultation is traceable to
   its orchestration context.
"""

from __future__ import annotations

import asyncio
import contextvars
import uuid

from app.config import Settings, get_settings
from app.openai_consult.schemas import ConsultIn
from app.openai_consult.service import ConsultationConfigError, consult_openai

# True for the entire duration of one consult_for_mainai pipeline. Any consult_for_mainai
# entered while this is set is BY DEFINITION a consultation triggered from inside
# consultation processing — refused before any gate, any DB touch, any network.
_INSIDE_CONSULTATION = contextvars.ContextVar("openai_consult_inside", default=False)

# A consultation may never chain into another one. Callers pass chain_depth=0 (the only
# accepted value); the parameter exists so a future orchestration loop that WANTS bounded
# iteration must first widen this constant deliberately and argue it in review.
MAX_CONSULTATION_CHAIN_DEPTH = 0


def _assert_no_credentials_in_strings(*values: str | None) -> None:
    """Credential scrub on the explicit inputs (mirrors provider_worker's context guard).
    Reuses the workforce helper on synthetic single-key items so the SAME denylist applies."""
    from app.workforce.provider_worker import assert_no_credentials_in_context

    items = [{"value": v} for v in values if v]
    assert_no_credentials_in_context(items)


async def consult_for_mainai(
    db,
    *,
    owner_id: uuid.UUID,
    goal_text: str,
    instructions: str | None = None,
    goal_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    chain_depth: int = 0,
    agent_key: str | None = None,
    settings: Settings | None = None,
    budget_store=None,
) -> tuple:
    """One authorized MainAI consultation. Returns (ConsultOut, stats) exactly like
    consult_openai. Raises (all BEFORE any network I/O):
        ConsultationConfigError   — disabled, unauthorized, nested/recursive, or unconfigured
        ProviderActivationBlocked — workforce activation gates not verified
        BudgetExceededError       — owner's daily token/cost ceiling would be crossed
        EgressDeniedError         — content refused by the egress kernel
    """
    # Guard 1a: nested consultation (this call was triggered from inside another
    # consultation's pipeline). Checked FIRST — before anything else can run.
    if _INSIDE_CONSULTATION.get():
        raise ConsultationConfigError(
            "Rekursiv konsultation vägras: en pågående konsultation kan aldrig utlösa en ny "
            "(fail-closed)."
        )
    # Guard 1b: explicit chain depth — no caller can request a chained consultation.
    if chain_depth != MAX_CONSULTATION_CHAIN_DEPTH:
        raise ConsultationConfigError(
            f"chain_depth={chain_depth} vägras: konsultationer kan inte kedjas (max "
            f"{MAX_CONSULTATION_CHAIN_DEPTH})."
        )

    settings = settings or get_settings()

    # Gate 2: MainAI-specific authorization — independent of the founder HTTP path.
    if not settings.openai_consult_mainai_enabled:
        raise ConsultationConfigError(
            "MainAI-konsultation är avstängd (OPENAI_CONSULT_MAINAI_ENABLED=false) — "
            "lokal in-process worker förblir primär exekveringsväg."
        )

    # Gate 2b: emergency shutdown. The workforce kill switch blocks ANY new consultation
    # immediately — defense in depth: the workforce harness checks it before reaching this
    # path, but consult_for_mainai is also callable directly by orchestration.
    from app.workforce.kill_switch import assert_not_killed

    assert_not_killed()

    # Budget store: same per-owner daily ceilings as the founder path (built from the same
    # settings when the caller doesn't supply one).
    if budget_store is None:
        from app.openai_consult.budgets import build_budget_store_from_settings

        budget_store = build_budget_store_from_settings(settings)

    # Gate 3: reuse MainAI's existing provider-activation authorization primitive.
    # UNKNOWN != VERIFIED → fail-closed; never auto-promoted.
    from app.workforce.activation_gates import require_activation_allowed

    require_activation_allowed()

    # Gate 3b: bounded consultation-selection policy — per-owner daily admission cap
    # (app/openai_consult/policy.py, Redis-backed, fail-closed in production without it).
    # Consultation is a bounded exception to the local-AI-primary rule, not a reflex.
    from app.openai_consult.policy import build_policy_from_settings

    build_policy_from_settings(settings).check_and_increment(str(owner_id))

    # Gate 4: the explicit inputs are the ONLY egress surface — credential-scrubbed.
    _assert_no_credentials_in_strings(goal_text, instructions)

    token = _INSIDE_CONSULTATION.set(True)
    try:
        # Gate 5-7 (egress, budget, HTTP, settle): the SAME core the founder path uses.
        # allow_web_search/response_schema are structurally absent from this call — the
        # ConsultIn is built here, not accepted from the caller.
        result, stats = await consult_openai(
            db,
            owner_id=owner_id,
            payload_in=ConsultIn(
                prompt=goal_text,
                instructions=instructions,
            ),
            settings=settings,
            budget_store=budget_store,
            requested_by=f"app.openai_consult.mainai.consult_for_mainai:{agent_key or 'unknown'}",
            purpose="mainai_consult",
            task_id=task_id,
            goal_id=goal_id,
            job_id=job_id,
        )
    finally:
        _INSIDE_CONSULTATION.reset(token)

    # Output contract validation (fail-closed): an empty/whitespace answer is a malformed
    # provider response — never returned to orchestration as a valid consultation result.
    if not (result.output_text or "").strip():
        from app.openai_consult.service import ConsultationServiceError

        raise ConsultationServiceError(
            "Konsultatsvaret var tomt — vägrar returnera det till orkestreringen "
            "(contract-validering, fail-closed)."
        )
    return result, stats


def consult_for_mainai_sync(
    db,
    *,
    owner_id: uuid.UUID,
    goal_text: str,
    **kwargs,
):
    """Sync entry point for MainAI's sync orchestration harness (provider_worker). Refuses
    when an event loop is already running in this thread — fail-closed protection against
    consultations kicked off from inside async consultation processing."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # no running loop — the expected case for the sync harness
    else:
        raise ConsultationConfigError(
            "Konsultation vägras från en aktiv event loop — anropa consult_for_mainai "
            "(async) direkt istället (fail-closed mot nästling)."
        )
    return asyncio.run(
        consult_for_mainai(db, owner_id=owner_id, goal_text=goal_text, **kwargs)
    )
