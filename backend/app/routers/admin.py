from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models.provider_config import ProviderConfig
from app.providers.registry import get_provider, provider_names
from app.schemas import ProviderConfigIn, ProviderConfigOut, ProviderStatus

router = APIRouter(prefix="/api/admin", tags=["admin"])


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
def set_provider_config(payload: ProviderConfigIn, db: Session = Depends(get_db)):
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
    return entry
