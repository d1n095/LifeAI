import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.cleanup import run_token_cleanup
from app.config import get_settings
from app.db import get_db
from app.deps import require_founder
from app.models.provider_config import ProviderConfig
from app.models.usage import UsageLog
from app.models.user import User
from app.providers.catalog import (
    ModelSelectionError,
    ModelSelectionPolicy,
    model_specs,
    resolve_model_spec,
)
from app.providers.registry import get_provider, provider_names, select_model
from app.providers.verification import latest_check, verify_now
from app.models.memory_source_backfill_run import BackfillRunMode, MemorySourceBackfillFailure, MemorySourceBackfillRun
from app.rag.claims import backfill_claim_types
from app.rag.backfill.memory_source_run import (
    BackfillRunBusy,
    BackfillRunNotAdvanceable,
    advance_backfill_run,
    cancel_backfill_run,
    create_or_resume_backfill_run,
    get_backfill_run,
    list_backfill_runs,
)
from app.schemas import (
    BackfillRunCreateIn,
    BackfillRunFailureOut,
    BackfillRunOut,
    ModelCatalogOut,
    ModelSelectionPreviewIn,
    ModelSelectionPreviewOut,
    ProviderConfigIn,
    ProviderConfigOut,
    ProviderStatus,
    ProviderVerificationOut,
    ProviderVerifyIn,
    UsageSummaryRow,
)

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_founder)])


def _verification_out(db: Session, provider_name: str, role: str) -> ProviderVerificationOut | None:
    check = latest_check(db, provider_name, role)
    if check is None:
        return None
    return ProviderVerificationOut(
        result=check.result.value, message=check.message, checked_by=check.checked_by, checked_at=check.checked_at, model=check.model
    )


@router.get("/providers/status", response_model=list[ProviderStatus])
def provider_status(db: Session = Depends(get_db)):
    active_chat = db.query(ProviderConfig).filter_by(role="chat", is_active=True).first()
    active_embedding = db.query(ProviderConfig).filter_by(role="embedding", is_active=True).first()
    settings = get_settings()

    statuses = []
    for name in provider_names():
        provider = get_provider(name)
        is_chat_active = (active_chat.provider if active_chat else settings.default_llm_provider) == name
        is_embedding_active = (active_embedding.provider if active_embedding else settings.default_embedding_provider) == name
        statuses.append(
            ProviderStatus(
                name=name,
                configured=provider.is_configured(),
                active_chat=is_chat_active,
                active_embedding=is_embedding_active,
                # P1: the latest REAL verification result, regardless of whether this
                # provider is active right now — so a founder can check a provider before
                # switching to it. `None` means "never verified", shown neutrally, not as
                # a failure, by the frontend.
                chat_verification=_verification_out(db, name, "chat"),
                embedding_verification=_verification_out(db, name, "embedding"),
            )
        )
    return statuses


@router.get("/providers/models", response_model=list[ModelCatalogOut])
def provider_model_catalog(db: Session = Depends(get_db)):
    """Founder-visible catalog joined with live configuration and exact-model evidence."""

    rows: list[ModelCatalogOut] = []
    for spec in model_specs():
        check = latest_check(db, spec.provider, "embedding" if "embedding" in spec.capabilities else "chat", model=spec.model_id)
        provider = get_provider(spec.provider)
        rows.append(
            ModelCatalogOut(
                provider=spec.provider,
                model_id=spec.model_id,
                version=spec.version,
                capabilities=sorted(spec.capabilities),
                supported_capabilities=sorted(spec.supported_capabilities),
                context_window_tokens=spec.context_window_tokens,
                locality=spec.locality.value,
                license_name=spec.license_name,
                license_url=spec.license_url,
                input_price_per_million_usd=(
                    float(spec.pricing.input_per_million_usd)
                    if spec.pricing.input_per_million_usd is not None
                    else None
                ),
                output_price_per_million_usd=(
                    float(spec.pricing.output_per_million_usd)
                    if spec.pricing.output_per_million_usd is not None
                    else None
                ),
                pricing_source=spec.pricing.source,
                pricing_as_of=spec.pricing.as_of,
                requests_per_minute=spec.quota.requests_per_minute,
                tokens_per_minute=spec.quota.tokens_per_minute,
                quota_note=spec.quota.note,
                availability=spec.availability.value,
                configured=provider.is_configured(),
                verification_result=check.result.value if check is not None else None,
                verification_checked_at=check.checked_at if check is not None else None,
            )
        )
    return rows


@router.post("/providers/select/preview", response_model=ModelSelectionPreviewOut)
def preview_model_selection(payload: ModelSelectionPreviewIn, db: Session = Depends(get_db)):
    """Read-only deterministic preview. Selection never creates authority or invokes a model."""

    policy = ModelSelectionPolicy(
        role=payload.role,
        required_capabilities=frozenset(payload.required_capabilities),
        minimum_context_tokens=payload.minimum_context_tokens,
        estimated_input_tokens=payload.estimated_input_tokens,
        estimated_output_tokens=payload.estimated_output_tokens,
        max_pricing_age_days=payload.max_pricing_age_days,
        allow_external=payload.allow_external,
        prefer_local=payload.prefer_local,
        allowed_providers=frozenset(payload.allowed_providers),
        allowed_models=frozenset(payload.allowed_models),
        max_input_price_per_million_usd=(
            Decimal(str(payload.max_input_price_per_million_usd))
            if payload.max_input_price_per_million_usd is not None
            else None
        ),
        max_output_price_per_million_usd=(
            Decimal(str(payload.max_output_price_per_million_usd))
            if payload.max_output_price_per_million_usd is not None
            else None
        ),
        require_verified=payload.require_verified,
    )
    try:
        _, model, spec = select_model(
            db,
            policy,
            requested_provider=payload.requested_provider,
            requested_model=payload.requested_model,
        )
    except ModelSelectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ModelSelectionPreviewOut(
        provider=spec.provider,
        model=model,
        version=spec.version,
        capabilities=sorted(spec.capabilities),
        locality=spec.locality.value,
        verified_required=policy.require_verified,
    )


@router.post("/providers/verify", response_model=ProviderVerificationOut)
async def verify_provider_now(
    payload: ProviderVerifyIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """P1: "Testa nu" — an explicit, founder-initiated real verification call, ALWAYS
    bypassing the cache (unlike the automatic pre-flight check in app/rag/ingest.py, which
    reuses a cached result within PROVIDER_VERIFICATION_CACHE_SECONDS). Never triggered
    automatically for all providers on page load — only this one, explicit action makes a
    real network call, so loading Admin -> Providers never itself bombards every configured
    leverantör."""
    if payload.provider not in provider_names():
        raise HTTPException(status_code=400, detail="Okänd leverantör.")
    if payload.model is not None:
        try:
            spec = resolve_model_spec(
                payload.provider,
                payload.model,
                role=payload.role,
                allow_custom_local=True,
            )
        except ModelSelectionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if payload.role not in spec.capabilities:
            raise HTTPException(status_code=400, detail="Modellen stöder inte den begärda rollen.")
    outcome = await verify_now(
        db,
        payload.provider,
        payload.role,
        checked_by="founder",
        model=payload.model,
    )
    record_audit(
        db,
        user_id=user.id,
        action="provider_verification_triggered",
        entity_type="provider_verification",
        entity_id=f"{payload.provider}:{payload.role}",
        detail=outcome.result.value,
        request=request,
    )
    return ProviderVerificationOut(
        result=outcome.result.value, message=outcome.message, checked_by="founder",
        checked_at=outcome.checked_at, model=outcome.model,
    )


@router.get("/providers/config", response_model=list[ProviderConfigOut])
def get_provider_config(db: Session = Depends(get_db)):
    return db.query(ProviderConfig).all()


@router.put("/providers/config", response_model=ProviderConfigOut)
def set_provider_config(
    payload: ProviderConfigIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """Switch the active provider/model for a role ("chat" or "embedding").

    This is the single write path that changes what LLM the whole platform uses —
    no redeploy, no code change.
    """
    # Serialize role writes including the first INSERT. No last-writer race can create
    # duplicate active rows; this transaction lock is released by commit/rollback.
    from sqlalchemy import text
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"),
               {"key": 46301 if payload.role == "chat" else 46302})
    try:
        # Manual choice is exact, registered and freshly verified. This validates selection
        # only; it does not authorize provider spend or content egress.
        select_model(
            db,
            ModelSelectionPolicy(
                role=payload.role,
                allow_external=True,
                prefer_local=False,
                require_verified=True,
                allow_custom_local=True,
            ),
            requested_provider=payload.provider,
            requested_model=payload.model,
        )
    except ModelSelectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    entry = db.query(ProviderConfig).filter_by(role=payload.role).first()
    if entry is None:
        entry = ProviderConfig(role=payload.role, provider=payload.provider, model=payload.model, is_active=True)
    else:
        entry.provider = payload.provider
        entry.model = payload.model
        entry.is_active = True
    db.add(entry)
    record_audit(
        db,
        user_id=user.id,
        action="provider_config_change",
        entity_type="provider_config",
        entity_id=payload.role,
        detail=f"{payload.provider}/{payload.model}",
        request=request,
        commit=False,
    )
    db.commit()
    db.refresh(entry)
    return entry


@router.get("/usage/summary", response_model=list[UsageSummaryRow])
def usage_summary(db: Session = Depends(get_db)):
    """Aggregated cost/usage per provider+model+role, across all users. Any group containing
    at least one call with unknown pricing (see app/providers/pricing.py) reports cost as
    None rather than an incomplete/misleading total."""
    rows = (
        db.query(
            UsageLog.provider,
            UsageLog.model,
            UsageLog.role,
            func.count(UsageLog.id),
            func.sum(UsageLog.prompt_tokens),
            func.sum(UsageLog.completion_tokens),
            func.sum(UsageLog.cost_usd),
            func.count(UsageLog.id).filter(UsageLog.cost_usd.is_(None)),
        )
        .group_by(UsageLog.provider, UsageLog.model, UsageLog.role)
        .order_by(UsageLog.provider)
        .all()
    )
    return [
        UsageSummaryRow(
            provider=provider,
            model=model,
            role=role,
            request_count=count,
            prompt_tokens=int(prompt_tokens or 0),
            completion_tokens=int(completion_tokens or 0),
            cost_usd=float(cost_sum) if unknown_count == 0 and cost_sum is not None else None,
        )
        for provider, model, role, count, prompt_tokens, completion_tokens, cost_sum, unknown_count in rows
    ]


@router.post("/cleanup")
def trigger_cleanup(request: Request, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    """Manual trigger for the token-retention cleanup (app/cleanup.py) — the same job also
    runs on its own schedule (see app/scheduler.py) whenever ENABLE_SCHEDULED_CLEANUP is on.
    Exposed here for ops visibility (run it on demand, see exactly what it purged) and so it
    can be exercised in tests without waiting for the scheduler."""
    counts = run_token_cleanup(db)
    record_audit(
        db,
        user_id=user.id,
        action="token_cleanup_triggered",
        detail=str(counts) if counts is not None else "skipped (already running elsewhere)",
        request=request,
    )
    if counts is None:
        return {"status": "skipped", "reason": "another cleanup run holds the lock"}
    return {"status": "completed", "purged": counts}


MAX_BACKFILL_BATCHES_PER_REQUEST = 10


@router.post("/claims/backfill-types")
async def trigger_claim_type_backfill(
    request: Request,
    max_batches: int = MAX_BACKFILL_BATCHES_PER_REQUEST,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """P3 gap fix (2026-07-28): migration 0018 left every pre-existing knowledge_claims row
    at claim_type=uncategorized, and the normal import path never re-classifies an
    already-`indexed` document's claims (see app/rag/claims.py's backfill_claim_types
    docstring for the full rationale). Manual trigger, same pattern as /cleanup above — safe
    to call repeatedly (idempotent, see that docstring), and safe to interrupt (each batch
    commits before the next one starts, so a partial run just leaves fewer candidates for the
    next call, never a duplicate or a half-written row).

    2026-07-28 correction: bounded to a capped number of batches per HTTP call rather than
    running unbounded inside one request — a large library would otherwise hold the
    connection open indefinitely with no job row, no progress visible to any other request,
    and no lease if the request itself dies mid-run. A caller with more candidates than one
    call covers simply calls this again (idempotent — already-typed claims are skipped, see
    backfill_claim_types), until `candidates_total` comes back 0. The real fix — running this
    as a durable memory_processing_jobs job with its own status/lease/retry, the same
    worker-container pattern app/worker.py already uses for imports — is tracked as the next
    step before this is exposed for a library at real scale; this cap is the interim safety
    net, not the final design."""
    max_batches = max(1, min(max_batches, MAX_BACKFILL_BATCHES_PER_REQUEST))
    result = await backfill_claim_types(db, user.id, max_batches=max_batches)
    record_audit(
        db,
        user_id=user.id,
        action="claim_type_backfill_triggered",
        detail=str(result.__dict__),
        request=request,
    )
    return {
        "candidates_total": result.candidates_total,
        "typed": result.typed,
        "still_uncategorized": result.still_uncategorized,
        "failed": result.failed,
    }


def _backfill_run_out(run: MemorySourceBackfillRun) -> BackfillRunOut:
    return BackfillRunOut(
        id=run.id,
        mode=run.mode.value,
        status=run.status.value,
        batch_size=run.batch_size,
        total_candidates_snapshot=run.total_candidates_snapshot,
        already_done_snapshot=run.already_done_snapshot,
        processed_count=run.processed_count,
        exact_chunk_count=run.exact_chunk_count,
        degraded_version_count=run.degraded_version_count,
        missing_document_only_count=run.missing_document_only_count,
        skipped_unresolvable_count=run.skipped_unresolvable_count,
        failed_count=run.failed_count,
        batches_completed=run.batches_completed,
        last_cursor_created_at=run.last_cursor_created_at,
        last_cursor_claim_id=run.last_cursor_claim_id,
        error_summary=run.error_summary,
        started_at=run.started_at,
        completed_at=run.completed_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


@router.post("/memory-source-backfill/runs", response_model=BackfillRunOut)
def create_backfill_run(
    body: BackfillRunCreateIn, request: Request, db: Session = Depends(get_db), user: User = Depends(require_founder)
):
    """S1A durable production backfill-run reporting (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md
    §4.8, PR #31's own memory_source.py module docstring — the last blocker that
    named before a real production backfill run). Creates a new run, or returns the caller's
    existing active (pending/running) run if one already exists (idempotent — see
    app/rag/backfill/memory_source_run.py's create_or_resume_backfill_run() docstring).

    This endpoint only CREATES/RESUMES the durable run row — it performs no backfill work
    itself. Call POST .../runs/{run_id}/advance to actually do bounded work, exactly one
    batch per call, same bounded-per-request discipline as /claims/backfill-types above."""
    try:
        mode = BackfillRunMode(body.mode)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"mode must be 'dry_run' or 'real', got {body.mode!r}")

    run = create_or_resume_backfill_run(db, user.id, mode=mode)
    record_audit(
        db,
        user_id=user.id,
        action="memory_source_backfill_run_created",
        entity_type="memory_source_backfill_run",
        entity_id=str(run.id),
        detail=f"mode={run.mode.value} status={run.status.value}",
        request=request,
    )
    return _backfill_run_out(run)


@router.post("/memory-source-backfill/runs/{run_id}/advance", response_model=BackfillRunOut)
def advance_backfill_run_endpoint(
    run_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User = Depends(require_founder)
):
    """Performs exactly ONE bounded batch of work for `run_id` and returns its updated durable
    status — see app/rag/backfill/memory_source_run.py's advance_backfill_run() docstring for
    why this is bounded to one batch per call (no unbounded loop held open inside a request).
    A caller driving a run to completion calls this repeatedly until `status` is no longer
    `running`/`pending`."""
    run = get_backfill_run(db, user.id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="backfill run not found")
    try:
        run = advance_backfill_run(db, run)
    except BackfillRunNotAdvanceable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except BackfillRunBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    record_audit(
        db,
        user_id=user.id,
        action="memory_source_backfill_run_advanced",
        entity_type="memory_source_backfill_run",
        entity_id=str(run.id),
        detail=(
            f"status={run.status.value} batches_completed={run.batches_completed} "
            f"processed_count={run.processed_count} failed_count={run.failed_count}"
        ),
        request=request,
    )
    return _backfill_run_out(run)


@router.post("/memory-source-backfill/runs/{run_id}/cancel", response_model=BackfillRunOut)
def cancel_backfill_run_endpoint(
    run_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User = Depends(require_founder)
):
    """Operator-triggered stop — remaining candidates simply stay valid for a future run."""
    run = get_backfill_run(db, user.id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="backfill run not found")
    try:
        run = cancel_backfill_run(db, run)
    except BackfillRunNotAdvanceable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except BackfillRunBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    record_audit(
        db,
        user_id=user.id,
        action="memory_source_backfill_run_cancelled",
        entity_type="memory_source_backfill_run",
        entity_id=str(run.id),
        request=request,
    )
    return _backfill_run_out(run)


@router.get("/memory-source-backfill/runs/{run_id}", response_model=BackfillRunOut)
def get_backfill_run_endpoint(run_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    run = get_backfill_run(db, user.id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="backfill run not found")
    return _backfill_run_out(run)


@router.get("/memory-source-backfill/runs", response_model=list[BackfillRunOut])
def list_backfill_runs_endpoint(db: Session = Depends(get_db), user: User = Depends(require_founder)):
    return [_backfill_run_out(run) for run in list_backfill_runs(db, user.id)]


@router.get("/memory-source-backfill/runs/{run_id}/failures", response_model=list[BackfillRunFailureOut])
def list_backfill_run_failures_endpoint(
    run_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)
):
    """Claim-level failure report — identifiers and structural reasons only, never
    `KnowledgeClaim.claim_text` (see migration 0025's module docstring)."""
    run = get_backfill_run(db, user.id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="backfill run not found")
    failures = (
        db.query(MemorySourceBackfillFailure)
        .filter(MemorySourceBackfillFailure.run_id == run.id)
        .order_by(MemorySourceBackfillFailure.created_at.asc())
        .all()
    )
    return [
        BackfillRunFailureOut(
            claim_id=f.claim_id,
            reason=f.reason,
            attempt_count=f.attempt_count,
            created_at=f.created_at,
            updated_at=f.updated_at,
        )
        for f in failures
    ]
