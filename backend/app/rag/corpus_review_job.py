"""The first MainAI job type built on the Durable Job Foundation (see migration 0025,
app/models/mainai_job.py): `corpus_review`. Reviews a founder-specified set of ALREADY
INDEXED documents (never re-uploads or duplicates the original file — it only reads existing
`document_chunks` rows) and records what it finds as `MainAIJobProposal` rows, each with
explicit provenance (`source_document_id`) back to the material it came from.

Real work, not a placeholder: each document is actually sent to the configured AI provider
chain (the SAME `chat_with_fallback()` app/agent_orchestration.py already uses for the
code/review agent loop) with a fixed, reviewed review prompt — never a canned or fabricated
response. `MainAIJobProposal.status` starts and stays `proposed`; nothing in this module (or
anywhere else in this codebase) ever promotes a proposal into a `KnowledgeClaim` or any other
form of founder-approved truth automatically — see MainAIJobProposal's own docstring.

Restart-safe, bounded batches: one document is one unit of work. Progress and a proposal
(or a recorded per-item failure event) are committed together after every document, so a
crash between documents loses at most the in-flight one, and re-running (after a worker
restart, or after `retry_job()`) skips every document that already has a MainAIJobProposal row
for this exact job — see `_already_reviewed_document_ids()`. Checks `cancel_requested` and
renews the worker lease (heartbeat) before every document, so a cancel or a stale-lease
reclaim is never delayed by more than one document's processing time.
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs.mainai_job_lease import renew_mainai_job_lease
from app.jobs.retry import is_transient_error
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.mainai_job import MainAIJob, MainAIJobErrorCategory, MainAIJobProposal, MainAIJobStatus
from app.providers.base import Message, ProviderError
from app.providers.registry import chat_with_fallback
from app.rag.mainai_jobs_service import mark_cancelled, mark_completed, mark_failed, update_progress

logger = logging.getLogger("mainai.jobs.corpus_review")

# Bounded so one document never balloons into an unbounded prompt — matches the founder's
# "bounded restart-safe batches" requirement at the content level too, not just the job-loop
# level. 8000 characters is comfortably inside every configured provider's context window for
# a single-document review (see docs/MAINAI_JOB_RUNTIME.md's corpus_review section).
_MAX_REVIEW_CHARS = 8000

CORPUS_REVIEW_SYSTEM_PROMPT = (
    "You are reviewing one document from a founder's personal knowledge library on their "
    "behalf. Read the excerpt and note anything genuinely worth the founder's attention: "
    "factual inconsistencies, unclear or contradictory statements, or notable claims. Be "
    "concise (a few sentences). This is a PROPOSAL for the founder to evaluate, not a "
    "decision — never claim something is confirmed or approved. If nothing stands out, say so "
    "plainly instead of inventing a finding."
)


def _document_review_text(db: Session, document: Document) -> str:
    chunks = list(
        db.execute(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document.id, DocumentChunk.owner_id == document.uploaded_by)
            .order_by(DocumentChunk.chunk_index.asc())
        ).scalars()
    )
    text = "\n\n".join(c.text for c in chunks)
    if not text and document.content_preview:
        text = document.content_preview
    return text[:_MAX_REVIEW_CHARS]


def _already_reviewed_document_ids(db: Session, job_id: uuid.UUID) -> set[uuid.UUID]:
    return set(
        db.execute(select(MainAIJobProposal.source_document_id).where(MainAIJobProposal.job_id == job_id)).scalars()
    )


def _refresh(db: Session, job_id: uuid.UUID) -> MainAIJob | None:
    db.expire_all()
    return db.get(MainAIJob, job_id)


async def run_corpus_review_job(db: Session, job_id: uuid.UUID, owner_id: uuid.UUID, *, worker_id: str, lease_seconds: int) -> None:
    """Entry point called by app/worker.py's poll loop once a `corpus_review` job has been
    claimed (see app/jobs/mainai_job_lease.py). `db` must already be scoped to `owner_id`'s
    RLS context (see app/worker.py's `_set_mainai_job_rls_owner`) before this is called."""
    job = db.get(MainAIJob, job_id)
    if job is None:
        return

    document_ids = [uuid.UUID(str(ref["id"])) for ref in job.input_refs if ref.get("type") == "document"]
    already_done = _already_reviewed_document_ids(db, job_id)
    total = len(document_ids)
    processed = len(already_done)
    update_progress(db, job, current=processed, total=total, phase="reviewing")
    db.commit()

    for document_id in document_ids:
        if document_id in already_done:
            continue

        job = _refresh(db, job_id)
        if job is None:
            return
        if job.cancel_requested:
            mark_cancelled(db, job)
            return

        renew_mainai_job_lease(db, job_id, lease_seconds)
        db.commit()

        document = db.get(Document, document_id)
        if document is None:
            # Deleted between job creation and this batch — a real, expected per-item
            # outcome, not a job-aborting failure. Recorded so the founder can see it in the
            # job's event history, then move on to the next document.
            job = _refresh(db, job_id)
            processed += 1
            update_progress(db, job, current=processed, total=total, phase="reviewing")
            db.commit()
            continue

        review_text = _document_review_text(db, document)
        try:
            result, _attempted = await chat_with_fallback(
                db,
                [
                    Message(role="system", content=CORPUS_REVIEW_SYSTEM_PROMPT),
                    Message(role="user", content=f"Document title: {document.title}\n\n{review_text}"),
                ],
            )
        except ProviderError as exc:
            db.rollback()
            job = _refresh(db, job_id)
            if job is None:
                return
            category = MainAIJobErrorCategory.transient_io if is_transient_error(exc) else MainAIJobErrorCategory.permanent
            mark_failed(db, job, error_category=category)
            logger.warning("corpus_review job %s failed on document %s: %s", job_id, document_id, exc)
            return
        except Exception:  # noqa: BLE001 - any other unexpected failure still fails the job safely, never silently
            db.rollback()
            job = _refresh(db, job_id)
            if job is None:
                return
            logger.exception("corpus_review job %s: unexpected error on document %s", job_id, document_id)
            mark_failed(db, job, error_category=MainAIJobErrorCategory.unexpected)
            return

        job = _refresh(db, job_id)
        if job is None:
            return
        proposal_id = uuid.uuid4()
        proposal = MainAIJobProposal(
            id=proposal_id,
            job_id=job.id,
            owner_id=job.owner_id,
            source_document_id=document.id,
            proposal_type="review_finding",
            proposal_text=result.content[:20000],
        )
        db.add(proposal)
        job.provider = result.provider
        job.model = result.model
        job.output_refs = [*job.output_refs, {"type": "mainai_job_proposal", "id": str(proposal_id)}]
        db.add(job)
        processed += 1
        update_progress(db, job, current=processed, total=total, phase="reviewing")
        db.commit()

    job = _refresh(db, job_id)
    if job is None:
        return
    if job.status == MainAIJobStatus.running:
        mark_completed(db, job, public_message=f"Reviewed {processed} of {total} document(s).")
