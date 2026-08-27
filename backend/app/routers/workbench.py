import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import require_founder
from app.egress_policy import EgressDeniedError
from app.limiter import limiter
from app.models.document import ActiveTruthStatus, Document, DocumentSource, KnowledgeClassification
from app.models.source_relationship import RelationshipType, SourceRelationship
from app.models.user import User
from app.providers.base import Message, ProviderError
from app.providers.registry import chat_with_fallback
from app.rag.ingest import index_document
from app.rag.retrieve import retrieve_context
from app.rag.trust import assess_confidence, build_trust_instructions, detect_conflicts
from app.schemas import KnowledgeSourceOut, SourceRef, WorkbenchAnalyzeIn, WorkbenchAnalyzeOut, WorkbenchSaveIn

router = APIRouter(prefix="/api/workbench", tags=["workbench"], dependencies=[Depends(require_founder)])

settings = get_settings()

# Foundation for a future dedicated agent station (see docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md's
# "not built tonight" section) — deliberately not the full agent organization the work order
# explicitly scopes tonight's Workbench down to. This system prompt asks for two clearly
# delimited sections so the response can be split into `conclusion` and `critique` without
# a second model call — CRITIQUE_MARKER must stay in sync with _split_conclusion below.
CRITIQUE_MARKER = "KRITIK/ALTERNATIV:"
SYSTEM_PROMPT = (
    "Du är MainAI:s analysläge i Founder Workbench. Grundaren väljer en fråga, ett projekt "
    "och/eller en specifik källa och vill ha en strukturerad analys, inte ett vanligt "
    "chattsvar. Svara i två tydligt avgränsade delar:\n"
    "SLUTSATS: din huvudsakliga slutsats, grundad i KONTEXT nedan.\n"
    f"{CRITIQUE_MARKER} minst en alternativ tolkning, risk, eller invändning mot din egen "
    "slutsats — hitta inte på en om det inte finns någon rimlig, men tänk igenom om något "
    "talar emot. Skriv på samma språk som frågan."
)


def _split_conclusion(raw: str) -> tuple[str, str | None]:
    if CRITIQUE_MARKER in raw:
        conclusion, _, critique = raw.partition(CRITIQUE_MARKER)
        return conclusion.strip(), critique.strip() or None
    return raw.strip(), None


_LABEL_MAPPING = {
    "idea": (KnowledgeClassification.general, ActiveTruthStatus.proposed),
    "proposal": (KnowledgeClassification.decisions, ActiveTruthStatus.proposed),
    "decision": (KnowledgeClassification.decisions, ActiveTruthStatus.active),
    "history": (KnowledgeClassification.history, ActiveTruthStatus.historical),
}


@router.post("/analyze", response_model=WorkbenchAnalyzeOut)
@limiter.limit(f"{settings.rate_limit_workbench_per_minute}/minute")
async def analyze(
    request: Request,
    payload: WorkbenchAnalyzeIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """DEL 9's "start analysis": choose a project and/or a specific document, ask a
    question, get sources + a grounded conclusion + a critique. Reuses the exact same
    retrieval/trust/provider machinery as /api/chat (app/routers/chat.py) rather than a
    parallel implementation — a historical/disputed source must be capped the same way here
    as it is in ordinary chat, not accidentally re-implemented (and re-verified) twice."""
    if payload.document_id is not None:
        doc = db.query(Document).filter(Document.id == payload.document_id, Document.deleted_at.is_(None)).first()
        if doc is None:
            raise HTTPException(status_code=404, detail="Källan hittades inte.")

    hits = await retrieve_context(
        db, user.id, payload.question, top_k=5, project_id=payload.project_id, document_id=payload.document_id
    )
    conflicting_pairs = detect_conflicts(db, user.id, [h["document_id"] for h in hits])
    trust = assess_confidence(hits, conflicting_pairs)

    context_block = "\n\n".join(f"[{h['title']}]\n{h['text']}" for h in hits) or "Ingen relevant kunskap hittades."
    system_content = (
        f"{SYSTEM_PROMPT}\n\nKONTEXT:\n{context_block}\n\n"
        f"TILLFÖRLITLIGHETSINSTRUKTION:\n"
        f"{build_trust_instructions(trust.level, trust.top_source_status, trust.conflicts_detected)}"
    )
    messages = [Message(role="system", content=system_content), Message(role="user", content=payload.question)]

    try:
        result, _ = await chat_with_fallback(
            db, messages, owner_id=user.id, purpose="workbench_analyze", requested_by="routers.workbench.analyze"
        )
    except (ProviderError, EgressDeniedError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    conclusion, critique = _split_conclusion(result.content)

    return WorkbenchAnalyzeOut(
        question=payload.question,
        conclusion=conclusion,
        critique=critique,
        sources=[
            SourceRef(
                document_id=uuid.UUID(h["document_id"]),
                title=h["title"],
                snippet=h["text"][:240],
                score=h["score"],
                active_truth_status=h.get("active_truth_status"),
            )
            for h in hits
        ],
        confidence=trust.level,
        confidence_score=trust.score,
        conflicts_detected=trust.conflicts_detected,
        provider=result.provider,
        model=result.model,
    )


@router.post("/save", response_model=KnowledgeSourceOut)
async def save_result(
    payload: WorkbenchSaveIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """DEL 9's "save result as a new knowledge object, mark as idea/proposal/decision/
    history". The new object becomes a first-class Document — searchable, citable in future
    chats, exportable and deletable exactly like an imported source — with `derived_from`
    relationships back to whichever sources the analysis was based on, so the provenance
    chain (DEL 1's SourceRelationship) survives past this one Workbench session."""
    classification, active_truth_status = _LABEL_MAPPING[payload.label]

    text_content = f"Fråga: {payload.question}\n\nSlutsats: {payload.conclusion}"
    if payload.critique:
        text_content += f"\n\nKritik/alternativ: {payload.critique}"

    document = Document(
        title=payload.question[:200],
        source=DocumentSource.manual,
        uploaded_by=user.id,
        classification=classification,
        active_truth_status=active_truth_status,
        project_id=payload.project_id,
    )
    db.add(document)
    db.commit()

    if payload.source_document_ids:
        # Only relate to sources the founder actually owns and that still exist — a stale or
        # forged id in the request body must not silently create a dangling/foreign edge.
        owned_ids = {
            row.id
            for row in db.query(Document.id)
            .filter(Document.id.in_(payload.source_document_ids), Document.uploaded_by == user.id, Document.deleted_at.is_(None))
            .all()
        }
        for source_id in owned_ids:
            db.add(
                SourceRelationship(
                    owner_id=user.id,
                    from_source_id=document.id,
                    to_source_id=source_id,
                    relationship_type=RelationshipType.derived_from,
                    note="Skapad via Founder Workbench-analys.",
                )
            )
        db.commit()

    await index_document(db, document, text_content)
    return document
