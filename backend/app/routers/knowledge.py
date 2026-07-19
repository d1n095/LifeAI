from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models.company import CompanyInfo
from app.models.user import User
from app.rag.retrieve import retrieve_context
from app.schemas import CompanyInfoIn, CompanyInfoOut

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"], dependencies=[Depends(get_current_user)])


class SearchQuery(BaseModel):
    query: str
    top_k: int = 8

    @field_validator("query")
    @classmethod
    def non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Sökfrågan får inte vara tom.")
        if len(v) > 2000:
            raise ValueError("Sökfrågan är för lång (max 2000 tecken).")
        return v

    @field_validator("top_k")
    @classmethod
    def bounded_top_k(cls, v: int) -> int:
        return max(1, min(v, 25))


@router.post("/search")
async def search_knowledge(payload: SearchQuery, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return await retrieve_context(db, user.id, payload.query, top_k=payload.top_k)


@router.get("/company", response_model=list[CompanyInfoOut])
def list_company_info(db: Session = Depends(get_db)):
    return db.query(CompanyInfo).order_by(CompanyInfo.updated_at.desc()).all()


@router.put("/company", response_model=CompanyInfoOut)
def upsert_company_info(payload: CompanyInfoIn, db: Session = Depends(get_db)):
    entry = db.query(CompanyInfo).filter_by(key=payload.key).first()
    if entry is None:
        entry = CompanyInfo(**payload.model_dump())
    else:
        entry.label = payload.label
        entry.content = payload.content
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry
