from fastapi import APIRouter, Depends, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.config import get_settings
from app.db import get_db
from app.deps import require_admin
from app.models.provider_config import ProviderConfig
from app.models.usage import UsageLog
from app.models.user import User
from app.providers.registry import get_provider, provider_names
from app.schemas import ProviderConfigIn, ProviderConfigOut, ProviderStatus, UsageSummaryRow

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


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
            )
        )
    return statuses


@router.get("/providers/config", response_model=list[ProviderConfigOut])
def get_provider_config(db: Session = Depends(get_db)):
    return db.query(ProviderConfig).all()


@router.put("/providers/config", response_model=ProviderConfigOut)
def set_provider_config(
    payload: ProviderConfigIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Switch the active provider/model for a role ("chat" or "embedding").

    This is the single write path that changes what LLM the whole platform uses —
    no redeploy, no code change.
    """
    entry = db.query(ProviderConfig).filter_by(role=payload.role).first()
    if entry is None:
        entry = ProviderConfig(role=payload.role, provider=payload.provider, model=payload.model, is_active=True)
    else:
        entry.provider = payload.provider
        entry.model = payload.model
        entry.is_active = True
    db.add(entry)
    db.commit()
    db.refresh(entry)
    record_audit(
        db,
        user_id=user.id,
        action="provider_config_change",
        entity_type="provider_config",
        entity_id=payload.role,
        detail=f"{payload.provider}/{payload.model}",
        request=request,
    )
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
