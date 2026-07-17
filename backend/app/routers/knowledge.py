from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.company import CompanyInfo
from app.rag.retrieve import retrieve_context
from app.schemas import CompanyInfoIn, CompanyInfoOut

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


class SearchQuery(BaseModel):
    query: str
    top_k: int = 8


@router.post("/search")
async def search_knowledge(payload: SearchQuery, db: Session = Depends(get_db)):
    return await retrieve_context(db, payload.query, top_k=payload.top_k)


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
