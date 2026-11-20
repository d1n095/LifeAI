"""MainAI chat — the user's message and the assistant's reply are two separate, independently
durable steps (see docs/BRANCH_REGISTRY.md's 2026-07-26 LLM Coupling & Failure-Boundary
Audit finding #1): the user's `MessageModel` row is committed BEFORE any provider is ever
called, so a provider failure can never lose or roll back what the founder actually typed.
`_attempt_assistant_reply()` is the one place that generates (or regenerates, on retry) the
assistant's response for an already-persisted user message — it creates or updates exactly
one assistant `MessageModel` row (enforced by migration 0016's partial unique index on
`in_reply_to_id`), never a second, duplicate reply."""

from base64 import urlsafe_b64encode

import logging
import uuid
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.context.resolver import (
    INTENT_CORRECTION,
    INTENT_EXPLICIT_MEMORY,
    INTENT_IDEA_WORTH_SAVING,
    ContextResolution,
    ConversationMessage,
    resolve_context,
)
from app.db import get_db
from app.deps import require_founder
from app.egress_policy import EgressDeniedError
from app.founder_memory_signals import record_candidate_signal
from app.limiter import limiter
from app.mainai_runtime_contract import build_answer_response, sanitize_unverified_execution_claims
from app.models.conversation import (
    ChatRequestClaim,
    Conversation,
    Message as MessageModel,
    MessageRole,
    MessageStatus,
)
from app.billing import chat_attempts, record_usage
from app.billing.chat_attempts import ReconciliationDenied, ReconciliationUnavailable
from app.billing.enforcement import BillingBudgetExceededError, BillingEnforcementUnavailableError
from app.billing.limits import effective_limits
from app.billing.reservation import ChatAttemptBudget
from app.models.provider_config import ProviderConfig
from app.models.user import User
from app.providers.base import Message, ProviderError
from app.providers.catalog import ModelSelectionError, ModelSelectionPolicy
from app.providers.registry import chat_with_fallback
from app.providers.verification import classify_provider_exception
from app.rag.context_status import build_context_status
from app.rag.message_source import MessageSourceLocator, get_or_create_message_source_unit
from app.rag.retrieve import GroundingUnavailableError, retrieve_context
from app.mainai_execution.lesson_selection import format_lesson_feedback_instructions
from app.project_status.service import format_project_status_instructions
from app.rag.graph_instructions import format_graph_context_instructions
from app.rag.untrusted_content import UNTRUSTED_CONTENT_POLICY, build_untrusted_context_block
from app.rag.epistemics import epistemic_status_counts_sql, format_epistemic_instructions
from app.rag.evidence_metadata import summarize_owner_evidence_landscape
from app.rag.evidence_quality import format_landscape_instructions
from app.rag.trust import assess_confidence, build_trust_instructions, detect_claim_conflicts, detect_conflicts
from app.schemas import (
    ChatAttemptReconcileIn,
    ChatMessageIn,
    ChatMessageOut,
    ChatUnresolvedAttemptOut,
    ContextStatusOut,
    SourceRef,
)

router = APIRouter(prefix="/api/chat", tags=["chat"], dependencies=[Depends(require_founder)])

logger = logging.getLogger(__name__)

# Resolver intents worth capturing as a candidate learning signal -- SIGNAL PRODUCER != TRUTH
# WRITER (see app/founder_memory_signals/service.py's own module docstring): this maps a
# resolver intent to a signal_kind, nothing more. It never touches founder_memory_notes.
_SIGNAL_WORTHY_INTENTS = {
    INTENT_EXPLICIT_MEMORY: "explicit_memory_candidate",
    INTENT_CORRECTION: "correction_candidate",
    INTENT_IDEA_WORTH_SAVING: "idea_candidate",
}

SYSTEM_PROMPT = (
    "Du är MainAI, grundarens Founder AI — inte en delad eller allmän assistent. Svara "
    "utifrån den kontext som ges nedan från kunskapsbiblioteket. Svara på samma språk som "
    "grundaren skriver på.\n\n"
    "VIKTIGT om vad du faktiskt kan göra: det här svaret är ditt HELA arbete med frågan — det "
    "finns inget bakgrundsjobb, ingen pågående körning och ingen separat process som fortsätter "
    "efter att du svarat. Du får ALDRIG påstå att du 'arbetar med det i bakgrunden', att 'jobbet "
    "har startat', att du 'kommer återkomma' eller liknande — det skulle vara osant. Om något du "
    "beskriver kräver en riktig, varaktig körning (t.ex. en granskning av flera dokument) ska du "
    "säga att grundaren själv kan starta ett sådant jobb via Jobb & Aktivitet — aldrig låtsas att "
    "du redan gör det."
)

settings = get_settings()

# Which of app/providers/base.py's ProviderError.category values a founder can reasonably
# expect to work by simply trying again (transient), vs. ones that need a real config change
# first (a bad/missing key, or a provider that structurally doesn't support this role).
_RETRYABLE_ERROR_CATEGORIES = {"unreachable", "rate_limited"}

# A retry must never turn an unresolved external effect into a second billable call. These
# categories are terminal until an evidence-backed reconciliation closes the attempt.
_UNRESOLVED_EFFECT_CATEGORIES = {
    "external_effect_unknown",
    "billing_outcome_unknown",
    "external_effect_unresolved",
}


def _verified_chat_policy() -> ModelSelectionPolicy:
    """Live chat must use an exact, verified external model.

    Local models keep working during rollout even if their exact tag predates the
    verification table; custom local tags are allowed, while unregistered external models
    still fail closed (their price and capabilities are not independently verified).
    """

    return ModelSelectionPolicy(
        role="chat",
        allow_external=True,
        prefer_local=True,
        require_verified=True,
        require_verified_local=False,
        allow_custom_local=True,
    )


def _chat_selection_policy(db: Session) -> ModelSelectionPolicy | None:
    """Enforce exact-model verification for admin-selected chat models.

    A deployment that has not yet written a ProviderConfig row still uses the settings
    default, exactly as before; this preserves an existing working local/legacy setup. Once
    a founder activates a model through Admin -> Providers, that live choice must satisfy the
    verified exact-model policy. External custom models remain fail-closed.
    """

    active = db.query(ProviderConfig).filter_by(role="chat", is_active=True).first()
    return _verified_chat_policy() if active is not None else None


def _get_or_create_reply_slot(db: Session, user_message_id: uuid.UUID) -> MessageModel | None:
    return db.query(MessageModel).filter_by(in_reply_to_id=user_message_id).populate_existing().first()


def _get_chat_request_claim(
    db: Session, *, owner_id: uuid.UUID, client_message_id: str
) -> ChatRequestClaim | None:
    return (
        db.query(ChatRequestClaim)
        .filter_by(owner_id=owner_id, client_message_id=client_message_id)
        .first()
    )


def _replay_chat_request(
    db: Session, *, claim: ChatRequestClaim, payload: ChatMessageIn, user: User
) -> ChatMessageOut:
    """Replay an already-claimed client send without invoking any provider.

    A retry with the same client_message_id either returns the original reply, returns the
    original failed contract, or gets 409 while the first attempt is unresolved. This is
    deliberately fail-closed: a crash between the durable claim and the provider call must
    not turn a retry into a second billable call.
    """
    conversation = db.get(Conversation, claim.conversation_id)
    user_message = db.get(MessageModel, claim.user_message_id)
    if conversation is None or user_message is None or conversation.user_id != user.id:
        raise HTTPException(status_code=404, detail="Meddelandet hittades inte.")
    if payload.conversation_id is not None and payload.conversation_id != conversation.id:
        raise HTTPException(
            status_code=409,
            detail="Idempotensnyckeln användes redan för en annan konversation.",
        )
    if user_message.content != payload.message:
        raise HTTPException(
            status_code=409,
            detail="Idempotensnyckeln användes redan för ett annat meddelande.",
        )

    reply = _get_or_create_reply_slot(db, user_message.id)
    if reply is None:
        raise HTTPException(
            status_code=409,
            detail="Förfrågan pågår fortfarande; försök igen senare.",
        )
    if str(reply.error_category or "").startswith(("provider_inflight", "inflight:")) or (
        reply.error_category in _UNRESOLVED_EFFECT_CATEGORIES
    ):
        raise HTTPException(
            status_code=409,
            detail="Provider outcome is unresolved; retry is blocked.",
        )
    if reply.status == MessageStatus.succeeded:
        return ChatMessageOut(
            conversation_id=conversation.id,
            user_message_id=user_message.id,
            assistant_status="succeeded",
            assistant_message_id=reply.id,
            reply=reply.content,
            provider=reply.provider,
            model=reply.model,
        )
    return ChatMessageOut(
        conversation_id=conversation.id,
        user_message_id=user_message.id,
        assistant_status="failed",
        assistant_message_id=reply.id,
        provider=reply.provider,
        model=reply.model,
        error_category=reply.error_category,
        retryable=reply.error_category in _RETRYABLE_ERROR_CATEGORIES,
    )



def _claim_reply_attempt(db, *, conversation, user_message, user):
    # Row-lock the immutable user-message identity, including the no-reply-row race.
    locked = db.query(MessageModel).filter_by(id=user_message.id, conversation_id=conversation.id).with_for_update().one()
    if conversation.user_id != user.id or locked.role != MessageRole.user:
        raise HTTPException(status_code=404, detail="Meddelandet hittades inte.")
    reply = _get_or_create_reply_slot(db, locked.id)
    if reply is not None and reply.status == MessageStatus.succeeded:
        db.commit()
        return reply
    if reply is not None and (str(reply.error_category).startswith(("provider_inflight", "inflight:"))
                              or reply.error_category in _UNRESOLVED_EFFECT_CATEGORIES):
        db.rollback()
        raise HTTPException(status_code=409, detail="Provider outcome is unresolved; retry is blocked.")
    if reply is None:
        reply = MessageModel(conversation_id=conversation.id, role=MessageRole.assistant,
                             in_reply_to_id=locked.id, content="", status=MessageStatus.failed)
        db.add(reply)
    reply.error_category = f"inflight:{urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip('=')}"
    db.commit()  # Durable before any outbound invocation; rollback cannot erase the claim.
    return reply

def _record_candidate_signal_if_worth_noticing(
    db: Session, *, owner_id: uuid.UUID, user_message: MessageModel, context_resolution: ContextResolution
) -> None:
    """Purely observational, same doctrine as `resolve_context()` itself (see the comment
    above its call site): never changes the chat response, never raises into the caller. Writes
    ONLY to `candidate_learning_signals` -- a staging table nothing treats as founder truth --
    never to `founder_memory_notes`. See docs/LIFE_FOUNDER_MEMORY.md's "Candidate learning
    signals" section: SIGNAL PRODUCER != TRUTH WRITER."""

    signal_kind = _SIGNAL_WORTHY_INTENTS.get(context_resolution.intent)
    if signal_kind is None:
        return
    try:
        record_candidate_signal(
            db, owner_id=owner_id, signal_kind=signal_kind, source_message_id=user_message.id,
            classifier_strategy="context_resolver_v1", classifier_confidence=context_resolution.confidence,
            classifier_reasoning=context_resolution.reasoning,
            idempotency_key=f"chat-resolver:{user_message.id}",
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("failed to record candidate learning signal for message %s (non-fatal)", user_message.id, exc_info=True)


async def _attempt_assistant_reply(db: Session, *, conversation: Conversation, user_message: MessageModel, user: User) -> ChatMessageOut:
    """Generates the assistant's reply for `user_message` (already committed). Used by both
    the initial send and the retry endpoint — a retry runs this exact same path, not a
    parallel one, so its behavior can never drift from the first attempt's."""
    # Incident 2026-07-26: retrieve_context() calls the EMBEDDING provider, a separate call
    # from chat_with_fallback()'s CHAT provider below — an unconfigured/failing embedding
    # provider previously crashed the whole request with an unhandled 500 before the chat
    # call (and its own, already-correct ProviderError handling) was ever reached. Widened
    # 2026-07-27 to also catch a raw httpx.HTTPError: a configured-but-invalid key raises an
    # UNWRAPPED httpx.HTTPStatusError from provider.embed() (only a missing key raises the
    # provider's own ProviderError) — that distinct case previously reached this function as
    # an unhandled exception. classify_provider_exception() turns either into a safe, sanitized
    # message (never a raw URL/key — see that function's own guarantee), fed into
    # build_context_status() below instead of collapsing straight to a fixed string.
    # Life Vault / External-AI Egress Control (docs/LIFE_VAULT_EGRESS_CONTROL.md, V2 query-
    # embedding half): retrieve_context() now also gates the query text itself through
    # enforce_egress_policy() before it reaches the embedding provider. A NEVER_EGRESS-marked
    # query is refused BEFORE any provider call -- never treated as a provider outage, never
    # silently retried against a different provider (embedding has no fallback chain in
    # MainAI 0.1 in the first place, see resolve_active()'s own docstring), and it degrades
    # exactly the same way a provider failure already does here: zero grounding, an accurate
    # degraded reason, the chat turn still completes ungrounded rather than crashing.
    assistant_row = _claim_reply_attempt(db, conversation=conversation, user_message=user_message, user=user)
    if assistant_row.status == MessageStatus.succeeded:
        return ChatMessageOut(conversation_id=conversation.id, user_message_id=user_message.id,
                              assistant_status="succeeded", assistant_message_id=assistant_row.id,
                              reply=assistant_row.content, provider=assistant_row.provider, model=assistant_row.model)
    retrieval_degraded_reason: str | None = None
    try:
        hits = await retrieve_context(db, user.id, user_message.content, top_k=5, effect_guard=True)
    except GroundingUnavailableError as exc:
        # The effect guard refused the active embedding provider before any call. This is
        # a configuration fact, not a provider outage, and it must never be silent: the
        # reply is explicitly marked ungrounded below and in the response contract.
        hits = []
        retrieval_degraded_reason = str(exc)
    except EgressDeniedError as exc:
        hits = []
        retrieval_degraded_reason = str(exc)
    except (ProviderError, httpx.HTTPError) as exc:
        hits = []
        retrieval_degraded_reason = classify_provider_exception(exc).message
    grounding_status = (
        "grounded" if hits else ("degraded" if retrieval_degraded_reason else "empty")
    )
    if retrieval_degraded_reason:
        logger.warning(
            "chat retrieval degraded for message %s: %s",
            user_message.id,
            retrieval_degraded_reason,
        )
    # Deleted sources can never appear here at all — app/rag/vector_store.py's search()
    # excludes Document.deleted_at IS NOT NULL at the query level, not just in the UI.
    conflicting_pairs = detect_conflicts(db, user.id, [h["document_id"] for h in hits])
    trust = assess_confidence(hits, conflicting_pairs)
    # Claim-level conflicts (STEG 10) can surface a disagreement source-level detection
    # alone would miss — see app/rag/trust.py's detect_claim_conflicts() docstring. ORed in,
    # never replaces the source-level check.
    if not trust.conflicts_detected and detect_claim_conflicts(db, user.id, [h["chunk_id"] for h in hits if h.get("chunk_id")]):
        trust.conflicts_detected = True

    def _label(h: dict) -> str:
        status = h.get("active_truth_status")
        return f"[{h['title']}] (status: {status})" if status and status != "active" else f"[{h['title']}]"

    # 2026-07-27 incident (see docs/BRANCH_REGISTRY.md's file-ingestion audit): a zero-hit
    # retrieval used to always inject the identical fixed string here, whether the real cause
    # was a stalled worker, a file mid-pipeline, a missing embedding provider, an indexing
    # failure, this specific search call failing, or genuinely no matching/no uploaded content.
    # The model then had no real reason to give and improvised one. build_context_status()
    # classifies the ACTUAL cause from existing signals (IndexStatus, worker heartbeat,
    # classify_provider_exception) — reused as both the injected context (so the model's own
    # reply reflects the real reason) and structured API metadata (context_status below) the
    # frontend can render without depending on the model having said it correctly.
    context_status = build_context_status(db, user.id, hit_count=len(hits), retrieval_degraded_reason=retrieval_degraded_reason)
    # Round 9 M4: retrieved document content is UNTRUSTED INPUT — every chunk is framed
    # as DATA (never instructions) so content-level injections cannot pose as platform
    # directives. The framing is deterministic and applied to every hit, not filtered by
    # suspicion heuristics.
    context_block = build_untrusted_context_block(
        [{"title": _label(h), "text": h["text"]} for h in hits],
        fallback=(
            context_status.message if context_status is not None else "Ingen relevant kunskap hittades."
        ),
    )
    context_status_out = (
        ContextStatusOut(
            reason=context_status.reason.value,
            message=context_status.message,
            pending_count=context_status.pending_count,
            awaiting_provider_count=context_status.awaiting_provider_count,
            failed_count=context_status.failed_count,
            indexed_count=context_status.indexed_count,
            total_document_count=context_status.total_document_count,
            worker_reachable=context_status.worker_reachable,
            out_of_space_chunk_count=context_status.out_of_space_chunk_count,
        )
        if context_status is not None
        else None
    )

    history = (
        db.query(MessageModel)
        .filter(
            MessageModel.conversation_id == conversation.id,
            MessageModel.id != user_message.id,
            MessageModel.status == MessageStatus.succeeded,
        )
        # S1B (migration 0030, §4.9): `id` as a tiebreaker makes this a total order. It matters
        # more here than anywhere else — this window is both the provider prompt's conversation
        # history AND app/context/resolver.py's input, so a same-timestamp pair rendering in a
        # different order between two calls could change the model's answer and the resolver's
        # classification for reasons nothing could reproduce. Same tiebreaker the S1B backfill
        # numbers by; still not `ORDER BY sequence_number` (EXPAND phase — see conversations.py).
        .order_by(MessageModel.created_at.asc(), MessageModel.id.asc())
        .limit(20)
        .all()
    )

    # Conversation Context Resolver v1 (see app/context/resolver.py, docs/CONTEXT_RESOLVER_V1.md)
    # — a rule-based classification of what kind of turn this message is (continuation, new
    # topic, correction, ...). Purely observational: it doesn't yet change retrieval or the
    # system prompt — surfaced on the response so the founder and a future UI can see it and
    # build on it without this endpoint's core behavior depending on it.
    context_resolution = resolve_context(
        user_message.content, [ConversationMessage(role=m.role.value, content=m.content, created_at=m.created_at) for m in history]
    )
    _record_candidate_signal_if_worth_noticing(db, owner_id=user.id, user_message=user_message, context_resolution=context_resolution)

    system_content = (
        f"{SYSTEM_PROMPT}\n\n{UNTRUSTED_CONTENT_POLICY}\n\nKONTEXT:\n{context_block}\n\n"
        f"TILLFÖRLITLIGHETSINSTRUKTION:\n"
        f"{build_trust_instructions(trust.level, trust.top_source_status, trust.conflicts_detected)}"
    )
    # Evidence-ladder rollup + body-of-evidence landscape (Round 8: these lines were
    # imported but never appended — the seam existed without a driver). Fail-open: any
    # failure degrades to the trust text alone, exactly the R7 contract.
    _evidence_lines: list[str] = []
    try:
        _evidence_lines.append(format_epistemic_instructions(epistemic_status_counts_sql(db, user.id)))
        _evidence_lines.append(format_landscape_instructions(summarize_owner_evidence_landscape(db, user.id)))
    except Exception:
        logger.warning("epistemic rollup failed; system prompt degrades to trust text alone", exc_info=True)
    if _evidence_lines:
        system_content += "\n\nEVIDENSSTATUS:\n" + "\n".join(_evidence_lines)
    # Round 9 M1: measured lesson standings + recurring-failure clusters ride the same
    # instruction block (fail-open like everything above). This is planner FEEDBACK in the
    # cognitive sense: the deterministic create_plan() ordering is unchanged — the prompt
    # layer now KNOWS which rules are measured-useful, which are hypotheses, and which
    # components keep failing under every rule pointed at them.
    try:
        lesson_feedback = format_lesson_feedback_instructions(db, owner_id=user.id)
        if lesson_feedback:
            system_content += "\n\n" + lesson_feedback
    except Exception:
        logger.warning("lesson feedback failed; system prompt degrades without it", exc_info=True)
    # Round 9 M3: the claim graph IN the answer path — consequences, question-relevant
    # cross-domain overlaps, and the retrieved claims' topic landscape are computed for the
    # retrieved sources and appended (fail-open). The graph now influences what the model
    # sees, not just what an endpoint would return.
    try:
        graph_context = format_graph_context_instructions(
            db, user.id, [h.get("document_id") for h in hits], user_message.content,
            top_hit_text=hits[0].get("text") if hits else None,
        )
        if graph_context:
            system_content += "\n\n" + graph_context
    except Exception:
        logger.warning("graph context failed; system prompt degrades without it", exc_info=True)
    # Founder Alpha R13/R15: the composed project status — blueprint, buckets, GitHub
    # provenance, the founder's locked decisions and the LIVE work chain — so "var är vi?" is
    # answered from the registers instead of the model's impression. Fail-open for the same
    # reason as every block above: a status derivation problem must never cost the founder the
    # reply they already earned.
    try:
        system_content += "\n\n" + format_project_status_instructions(
            db, owner_id=user.id, question="\n".join([m.content for m in history if m.role == MessageRole.user][-2:] + [user_message.content]))
    except Exception:
        logger.warning("project status block failed; reply unaffected", exc_info=True)
    messages = [Message(role="system", content=system_content)]
    messages += [Message(role=m.role.value, content=m.content) for m in history]
    messages.append(Message(role="user", content=user_message.content))

    limits = effective_limits(db, user)
    attempt_budget = ChatAttemptBudget(db, user=user, limits=limits,
                                      estimated_tokens=sum(len(m.content.encode("utf-8")) for m in messages) + 1024,
                                      source_ref=f"chat:{user_message.id}:{assistant_row.error_category}",
                                      reply_slot=assistant_row, user_message_id=user_message.id,
                                      enforce=get_settings().billing_enforcement_enabled)

    try:
        result, attempted = await chat_with_fallback(
            db,
            messages,
            owner_id=user.id,
            purpose="chat",
            requested_by="routers.chat._attempt_assistant_reply",
            selection_policy=_chat_selection_policy(db),
            effect_guard=True,
            max_tokens=1024,
            attempt_observer=attempt_budget,
        )
    except BillingBudgetExceededError as exc:
        # Proven pre-invoke refusal is retryable only after budget/policy changes.
        assistant_row.error_category = "billing_refused"
        db.commit()
        raise HTTPException(status_code=402, detail=str(exc)) from None
    except BillingEnforcementUnavailableError as exc:
        assistant_row.error_category = ("billing_outcome_unknown" if getattr(exc, "reservation_outcome_unknown", True)
                                        else "billing_refused")
        db.commit()
        raise HTTPException(status_code=503, detail="Provider budget could not be verified.") from None
    except ModelSelectionError as exc:
        # No provider was invoked: the active model could not satisfy exact-model policy.
        # This is a configuration failure, not a retryable provider outage.
        assistant_row.status = MessageStatus.failed
        assistant_row.error_category = "provider_unverified"
        assistant_row.content = ""
        db.commit()
        db.refresh(assistant_row)
        return ChatMessageOut(
            conversation_id=conversation.id,
            user_message_id=user_message.id,
            assistant_status="failed",
            assistant_message_id=assistant_row.id,
            error_category="provider_unverified",
            error_message=str(exc),
            retryable=False,
            context_status=context_status_out,
            grounding_status=grounding_status,
            grounding_reason=retrieval_degraded_reason,
        )
    except (ProviderError, EgressDeniedError) as exc:
        # Life Vault / External-AI Egress Control (docs/LIFE_VAULT_EGRESS_CONTROL.md, V1): an
        # EgressDeniedError means the gate refused the WHOLE call before any provider was ever
        # reached (never a provider outage) -- always non-retryable, since the content that
        # triggered the denial (this conversation's own history/retrieval) is unchanged on
        # retry and would be denied identically again.
        category = "egress_denied" if isinstance(exc, EgressDeniedError) else (exc.category or "unreachable")
        if assistant_row is None:
            assistant_row = MessageModel(
                conversation_id=conversation.id,
                role=MessageRole.assistant,
                content="",
                in_reply_to_id=user_message.id,
                status=MessageStatus.failed,
                error_category=category,
            )
            db.add(assistant_row)
        else:
            assistant_row.status = MessageStatus.failed
            assistant_row.error_category = category
            assistant_row.content = ""
        db.commit()
        db.refresh(assistant_row)
        # The failed reply row is still an honest part of the founder's history — capture
        # its provenance too (fail-open, same helper as the success path).
        _capture_message_source(db, message=assistant_row, conversation=conversation)
        return ChatMessageOut(
            conversation_id=conversation.id,
            user_message_id=user_message.id,
            assistant_status="failed",
            assistant_message_id=assistant_row.id,
            error_category=category,
            error_message=str(exc),
            retryable=category in _RETRYABLE_ERROR_CATEGORIES,
            context_status=context_status_out,
            grounding_status=grounding_status,
            grounding_reason=retrieval_degraded_reason,
        )

    usage = result.raw_usage or {}
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    # Revenue & Compute Engine: the SINGLE accounting writer (per-call audit row +
    # enforcement rollups in one pipeline). Tenant/agent/task dimensions are platform
    # plumbing; tier is snapshotted for audit. See app/billing/__init__.py.
    record_usage(
        db,
        user_id=user.id,
        tenant_id=getattr(user, "tenant_id", None),
        role="chat",
        provider=result.provider,
        model=result.model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        agent="chat",
        task="conversation_reply",
        conversation_id=conversation.id,
        tier=effective_limits(db, user).tier,
        idempotency_key=f"chat-reply:{user_message.id}",
    )

    # Founder re-review round (PR #36): the runtime truthfulness contract (app/
    # mainai_runtime_contract.py) existed but was never actually wired into a live surface --
    # this is that wiring. sanitize_unverified_execution_claims() is the secondary,
    # defense-in-depth check for the one channel the SYSTEM_PROMPT instruction above can't
    # structurally guarantee (the model's own free text); build_answer_response()'s own
    # Pydantic validation is the structural guarantee that this reply is shaped as a plain
    # `answer` with `job_id=None` -- this endpoint has no background-job concept at all, so
    # that is the only mode a chat reply could ever truthfully be.
    safe_content = sanitize_unverified_execution_claims(result.content)
    build_answer_response(safe_content)  # raises on a contract violation; never actually can for mode=answer/job_id=None, but every call site goes through this the same way

    if assistant_row is None:
        assistant_row = MessageModel(conversation_id=conversation.id, role=MessageRole.assistant, in_reply_to_id=user_message.id)
        db.add(assistant_row)
    assistant_row.content = safe_content
    assistant_row.provider = result.provider
    assistant_row.model = result.model
    assistant_row.source_document_ids = ",".join(h["document_id"] for h in hits)
    assistant_row.status = MessageStatus.succeeded
    assistant_row.error_category = None

    # Touch updated_at explicitly — adding Message rows doesn't trigger the Conversation
    # row's onupdate, and /api/conversations sorts by this to surface recent activity first.
    conversation.updated_at = datetime.utcnow()
    db.add(conversation)
    db.commit()
    db.refresh(assistant_row)
    _capture_message_source(db, message=assistant_row, conversation=conversation)

    sources = [
        SourceRef(
            document_id=uuid.UUID(h["document_id"]),
            title=h["title"],
            snippet=h["text"][:240],
            score=h["score"],
            active_truth_status=h.get("active_truth_status"),
            start_seconds=h.get("start_seconds"),
            end_seconds=h.get("end_seconds"),
        )
        for h in hits
    ]
    return ChatMessageOut(
        conversation_id=conversation.id,
        user_message_id=user_message.id,
        assistant_status="succeeded",
        assistant_message_id=assistant_row.id,
        reply=safe_content,
        provider=result.provider,
        model=result.model,
        sources=sources,
        confidence=trust.level,
        confidence_score=trust.score,
        providers_attempted=attempted,
        conflicts_detected=trust.conflicts_detected,
        context_intent=context_resolution.intent,
        context_confidence=context_resolution.confidence,
        context_status=context_status_out,
        grounding_status=grounding_status,
        grounding_reason=retrieval_degraded_reason,
    )



def _capture_message_source(db: Session, *, message, conversation) -> None:
    """Capture the S1C immutable provenance unit (memory_source_units/message_source_units)
    for a message AT WRITE TIME (Founder Alpha audit gap A1-a: previously these existed
    only after a founder-triggered backfill job, so the provenance layer lagged the chat).

    Deliberately FAIL-OPEN for chat delivery: provenance must never break or delay the
    founder's conversation — the backfill job remains the safety net for anything this
    misses, and it is idempotent (same identity key returns the same unit). Reuses the
    existing race-safe find-or-create service and the DB-level immutability triggers;
    no new storage is introduced."""
    try:
        get_or_create_message_source_unit(
            db,
            MessageSourceLocator(
                owner_id=conversation.user_id,
                message_id=message.id,
                conversation_id=conversation.id,
                role=message.role,
                observed_at=message.created_at,
                content_text=message.content,
            ),
        )
        db.commit()
    except Exception:
        logger.exception(
            "chat: message_source_unit capture failed for message %s — backfill will cover it",
            message.id,
        )
        db.rollback()


@router.post("", response_model=ChatMessageOut)
@limiter.limit(f"{settings.rate_limit_chat_per_minute}/minute")
async def chat(
    request: Request,
    payload: ChatMessageIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    if payload.client_message_id is not None:
        existing_claim = _get_chat_request_claim(
            db, owner_id=user.id, client_message_id=payload.client_message_id
        )
        if existing_claim is not None:
            return _replay_chat_request(db, claim=existing_claim, payload=payload, user=user)

    conversation = None
    if payload.conversation_id:
        # RLS on `conversations` already prevents cross-user reads, but we check
        # explicitly too so a mismatched id gives a clean 404 instead of silently
        # falling through to "create a new conversation".
        conversation = db.get(Conversation, payload.conversation_id)
        if conversation is not None and conversation.user_id != user.id:
            raise HTTPException(status_code=404, detail="Konversationen hittades inte.")
    if conversation is None:
        conversation = Conversation(title=payload.message[:60], user_id=user.id)
        db.add(conversation)
        if payload.client_message_id is None:
            db.commit()
        else:
            # Keep the new conversation uncommitted until the claim and user message are
            # durable together; a losing concurrent duplicate rolls all three back.
            db.flush()
        # No db.refresh(): id/created_at/updated_at are populated client-side by SQLAlchemy
        # at flush (see app/models/conversation.py defaults), so the object is already
        # fully populated — a refresh would just be an unnecessary extra round-trip.

    # THE FIX: persisted and committed before any provider is ever called. With a client
    # idempotency key the user message and its claim are committed in the SAME transaction,
    # so a concurrent duplicate either wins the unique constraint or replays the winner —
    # never a second user message and never a second billable provider call.
    user_message = MessageModel(conversation_id=conversation.id, role=MessageRole.user, content=payload.message)
    db.add(user_message)
    if payload.client_message_id is None:
        db.commit()
    else:
        db.flush()
        db.add(
            ChatRequestClaim(
                owner_id=user.id,
                client_message_id=payload.client_message_id,
                conversation_id=conversation.id,
                user_message_id=user_message.id,
            )
        )
        try:
            db.commit()
        except IntegrityError:
            # Another request with the same (owner, client_message_id) won the race. The
            # rollback removes only this request's conversation/message/claim; replay the
            # winner's durable outcome instead of creating a second provider effect.
            db.rollback()
            existing_claim = _get_chat_request_claim(
                db, owner_id=user.id, client_message_id=payload.client_message_id
            )
            if existing_claim is None:
                raise HTTPException(
                    status_code=409,
                    detail="Förfrågan kunde inte idempotenssäkras.",
                ) from None
            return _replay_chat_request(db, claim=existing_claim, payload=payload, user=user)
    db.refresh(user_message)
    _capture_message_source(db, message=user_message, conversation=conversation)

    return await _attempt_assistant_reply(db, conversation=conversation, user_message=user_message, user=user)


@router.post("/messages/{message_id}/retry", response_model=ChatMessageOut)
@limiter.limit(f"{settings.rate_limit_chat_per_minute}/minute")
async def retry_assistant_reply(
    request: Request,
    message_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """Re-attempts generating the assistant's reply for an already-saved user message —
    never creates a second user message, never duplicates the assistant reply (migration
    0016's partial unique index on `in_reply_to_id` makes a second row impossible even if
    this handler had a bug). Idempotent: if a reply already succeeded, it's returned as-is
    without spending a new provider call or risking a different, non-reproducible answer."""
    user_message = db.get(MessageModel, message_id)
    if user_message is None or user_message.role != MessageRole.user:
        raise HTTPException(status_code=404, detail="Meddelandet hittades inte.")
    conversation = db.get(Conversation, user_message.conversation_id)
    if conversation is None or conversation.user_id != user.id:
        raise HTTPException(status_code=404, detail="Meddelandet hittades inte.")

    existing = _get_or_create_reply_slot(db, user_message.id)
    if existing is not None and existing.status == MessageStatus.succeeded:
        return ChatMessageOut(
            conversation_id=conversation.id,
            user_message_id=user_message.id,
            assistant_status="succeeded",
            assistant_message_id=existing.id,
            reply=existing.content,
            provider=existing.provider,
            model=existing.model,
        )

    return await _attempt_assistant_reply(db, conversation=conversation, user_message=user_message, user=user)


def _unresolved_attempt_out(attempt) -> ChatUnresolvedAttemptOut:
    age = chat_attempts.attempt_age_seconds(attempt)
    stale = age >= chat_attempts.DEFAULT_STALE_AFTER_SECONDS
    release_allowed = stale and chat_attempts.release_is_provably_safe(attempt)
    if release_allowed:
        note = "Kan släppas: ingen anropsmarkering finns, så ingen begäran kan ha lämnat processen."
    elif attempt.state == "reserved":
        note = "Väntar på att bli stale innan den kan granskas och släppas."
    else:
        note = (
            "Kan inte släppas utan leverantörsbevis. Avräkna konservativt eller från "
            "dokumenterad användning."
        )
    return ChatUnresolvedAttemptOut(
        id=attempt.id,
        assistant_message_id=attempt.assistant_message_id,
        user_message_id=attempt.user_message_id,
        state=attempt.state,
        effect_boundary=attempt.effect_boundary,
        provider=attempt.provider,
        model=attempt.model,
        created_at=attempt.created_at,
        updated_at=attempt.updated_at,
        age_seconds=int(age),
        release_allowed=release_allowed,
        review_note=note,
    )


@router.get("/unresolved", response_model=list[ChatUnresolvedAttemptOut])
def unresolved_chat_attempts(
    db: Session = Depends(get_db), user: User = Depends(require_founder)
):
    """List this owner's unresolved billable chat attempts for review.

    Never invokes a provider and never changes budget state; it is the read half of the
    recovery workflow. Owner isolation is enforced by both the explicit filter and RLS.
    """

    return [
        _unresolved_attempt_out(attempt)
        for attempt in chat_attempts.list_unresolved_attempts(db, owner_id=user.id)
    ]


@router.post("/unresolved/{attempt_id}/reconcile", response_model=ChatUnresolvedAttemptOut)
def reconcile_chat_attempt(
    attempt_id: uuid.UUID,
    payload: ChatAttemptReconcileIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """Evidence-backed reconciliation of one unresolved attempt.

    `release_pre_egress` is accepted only for a stale attempt that was never marked as
    invoked; `settle_conservative` charges the already-reserved worst case; and
    `settle_from_usage` requires recorded usage evidence. No path calls the provider or
    creates a second billable attempt.
    """

    try:
        attempt = chat_attempts.reconcile_attempt(
            db,
            owner_id=user.id,
            attempt_id=attempt_id,
            resolution=payload.resolution,
            evidence_ref=payload.evidence_ref,
            actor_id=user.id,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Avstämningen hittades inte.") from None
    except ReconciliationDenied as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ReconciliationUnavailable:
        raise HTTPException(
            status_code=503,
            detail="Budgetreservationen kunde inte slutföras; försöket är fortfarande oavstämt.",
        ) from None
    return _unresolved_attempt_out(attempt)
