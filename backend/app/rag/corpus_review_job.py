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

Restart-safe, bounded batches: one document is one unit of work. Progress and a proposal (or
a recorded per-document skip event) are committed together after every document, so a crash
between documents loses at most the in-flight one, and re-running (after a worker restart, or
after `retry_job()`) skips every document that already has a MainAIJobProposal row for this
exact job — see `_already_reviewed_document_ids()`. Checks `cancel_requested` and renews the
worker lease (heartbeat) before every document, so a cancel or a stale-lease reclaim is never
delayed by more than one document's processing time.

Lease fencing (migration 0028, founder re-review round on PR #36): every write this loop makes
against the claimed job — heartbeat, progress, a reviewed outcome, a skipped outcome, or a
terminal status — goes through app/rag/mainai_jobs_service.py's fencing-guarded functions,
passing the exact (worker_id, lease_generation) this run was claimed with. The instant any of
those calls raises JobLeaseLostError, this function stops immediately and returns without
making any further writes — a different worker has legitimately reclaimed this job (this
worker's own lease already expired) and may already be processing it concurrently; continuing
would race that worker's writes, exactly the incident this fencing scheme exists to prevent.

Per-document outcome truthfulness (same review round): "reviewed N of N documents" used to be
claimed even when some of those N were never actually sent to a provider — silently skipped
because the document was deleted mid-run, had no reviewable content, or a provider call failed
for that one document specifically. Every non-reviewed outcome now gets its own
`document_skipped` event with a closed-vocabulary `reason` (`deleted` / `unavailable` /
`provider_failed`), and the final completion message reports the real breakdown — reviewed,
skipped-deleted, unavailable, and provider-failed counts against the job's fixed snapshot
total — never a single number that blurs "actually reviewed" with "counted as done because we
moved on". A provider failure for ONE document no longer aborts the whole job (mixed outcomes
in a single run — some documents reviewed, some failed — are the normal, expected case for a
real provider having a bad moment on one call, not a systemic failure); only a genuinely
unexpected error (a bug, not a per-document content/provider problem) still fails the job as a
whole, via mark_failed.
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs.mainai_job_lease import JobLeaseLostError, renew_mainai_job_lease
from app.jobs.retry import is_transient_error
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.mainai_job import MainAIJob, MainAIJobErrorCategory, MainAIJobProposal, MainAIJobStatus
from app.providers.base import Message, ProviderError
from app.providers.registry import chat_with_fallback
from app.rag.mainai_jobs_service import mark_cancelled, mark_completed, mark_failed, record_document_reviewed, record_document_skipped, update_progress

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


async def run_corpus_review_job(
    db: Session, job_id: uuid.UUID, owner_id: uuid.UUID, *, worker_id: str, lease_generation: int, lease_seconds: int
) -> None:
    """Entry point called by app/worker.py's poll loop once a `corpus_review` job has been
    claimed (see app/jobs/mainai_job_lease.py). `db` must already be scoped to `owner_id`'s
    RLS context (see app/worker.py's `_set_mainai_job_rls_owner`) before this is called.
    `lease_generation` must be the exact value this job was (re)claimed with — every write
    below is fenced against it, see this module's own docstring."""
    job = db.get(MainAIJob, job_id)
    if job is None:
        return

    document_ids = [uuid.UUID(str(ref["id"])) for ref in job.input_refs if ref.get("type") == "document"]
    already_reviewed = _already_reviewed_document_ids(db, job_id)
    # Fixed once, at the top of this run, from the job's own original input_refs — the
    # "snapshot total" the founder's review asked this module to be explicit about: it is the
    # size of the corpus THIS job was asked to review, not a live count that could shrink or
    # grow if documents are added/deleted elsewhere while this job runs.
    total = len(document_ids)
    reviewed_count = len(already_reviewed)
    skipped_deleted_count = 0
    unavailable_count = 0
    provider_failed_count = 0

    def _lease_lost(where: str) -> None:
        logger.warning("corpus_review job %s: lease lost (%s) — worker %s, generation %s. Stopping.", job_id, where, worker_id, lease_generation)

    try:
        update_progress(db, job, worker_id=worker_id, lease_generation=lease_generation, current=reviewed_count, total=total, phase="reviewing")
        db.commit()
    except JobLeaseLostError:
        _lease_lost("initial progress")
        return

    for document_id in document_ids:
        if document_id in already_reviewed:
            continue

        job = _refresh(db, job_id)
        if job is None:
            return
        if job.cancel_requested:
            try:
                mark_cancelled(db, job, worker_id=worker_id, lease_generation=lease_generation)
            except JobLeaseLostError:
                _lease_lost("cancel")
            return

        try:
            renew_mainai_job_lease(db, job_id, worker_id, lease_generation, lease_seconds)
            db.commit()
        except JobLeaseLostError:
            _lease_lost("heartbeat")
            return

        document = db.get(Document, document_id)
        if document is None:
            # Deleted between job creation and this batch — a real, expected per-item
            # outcome, not a job-aborting failure. Recorded as a `document_skipped` event
            # with reason `deleted` (never silently folded into a bare progress increment)
            # so the founder can see exactly which documents were never actually reviewed and
            # why, in the event history itself, not just inferred from a lower final count.
            skipped_deleted_count += 1
            job = _refresh(db, job_id)
            if job is None:
                return
            try:
                record_document_skipped(
                    db, job, worker_id=worker_id, lease_generation=lease_generation, document_id=document_id, reason="deleted"
                )
                db.commit()
            except JobLeaseLostError:
                _lease_lost("skip:deleted")
                return
            continue

        review_text = _document_review_text(db, document)
        if not review_text:
            # No chunks AND no content_preview — nothing to actually send a provider. Sending
            # an empty prompt and letting the model "review" nothing would produce a proposal
            # that looks real but reviewed no actual content — worse than being honest that
            # this document had nothing available to review.
            unavailable_count += 1
            job = _refresh(db, job_id)
            if job is None:
                return
            try:
                record_document_skipped(
                    db, job, worker_id=worker_id, lease_generation=lease_generation, document_id=document_id, reason="unavailable"
                )
                db.commit()
            except JobLeaseLostError:
                _lease_lost("skip:unavailable")
                return
            continue

        try:
            result, _attempted = await chat_with_fallback(
                db,
                [
                    Message(role="system", content=CORPUS_REVIEW_SYSTEM_PROMPT),
                    Message(role="user", content=f"Document title: {document.title}\n\n{review_text}"),
                ],
            )
        except ProviderError as exc:
            # A provider failure for THIS ONE document does not abort the whole job — see
            # module docstring. Recorded as its own skipped outcome (reason `provider_failed`,
            # with the same safe, closed-vocabulary error_category mark_failed() would use —
            # never str(exc)) and the loop moves on to the next document.
            db.rollback()
            provider_failed_count += 1
            category = MainAIJobErrorCategory.transient_io if is_transient_error(exc) else MainAIJobErrorCategory.permanent
            logger.warning("corpus_review job %s: document %s failed (%s): %s", job_id, document_id, category.value, exc)
            job = _refresh(db, job_id)
            if job is None:
                return
            try:
                record_document_skipped(
                    db,
                    job,
                    worker_id=worker_id,
                    lease_generation=lease_generation,
                    document_id=document_id,
                    reason="provider_failed",
                    error_category=category.value,
                )
                db.commit()
            except JobLeaseLostError:
                _lease_lost("skip:provider_failed")
                return
            continue
        except Exception:  # noqa: BLE001 - a genuinely unexpected (non-provider, non-per-document) failure still fails the whole job
            db.rollback()
            job = _refresh(db, job_id)
            if job is None:
                return
            logger.exception("corpus_review job %s: unexpected error on document %s", job_id, document_id)
            try:
                mark_failed(db, job, worker_id=worker_id, lease_generation=lease_generation, error_category=MainAIJobErrorCategory.unexpected)
            except JobLeaseLostError:
                _lease_lost("mark_failed:unexpected")
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
        reviewed_count += 1
        try:
            record_document_reviewed(
                db,
                job,
                worker_id=worker_id,
                lease_generation=lease_generation,
                current=reviewed_count,
                total=total,
                provider=result.provider,
                model=result.model,
                proposal_output_ref={"type": "mainai_job_proposal", "id": str(proposal_id)},
            )
            db.commit()
        except JobLeaseLostError:
            db.rollback()
            _lease_lost("record_reviewed")
            return

    job = _refresh(db, job_id)
    if job is None:
        return
    if job.status == MainAIJobStatus.running:
        skipped_total = skipped_deleted_count + unavailable_count + provider_failed_count
        parts = [f"Reviewed {reviewed_count} of {total} document(s) (snapshot at job start)."]
        if skipped_total:
            detail_parts = []
            if skipped_deleted_count:
                detail_parts.append(f"{skipped_deleted_count} deleted")
            if unavailable_count:
                detail_parts.append(f"{unavailable_count} unavailable")
            if provider_failed_count:
                detail_parts.append(f"{provider_failed_count} failed")
            parts.append(f"{skipped_total} not reviewed ({', '.join(detail_parts)}).")
        try:
            mark_completed(db, job, worker_id=worker_id, lease_generation=lease_generation, public_message=" ".join(parts))
        except JobLeaseLostError:
            _lease_lost("mark_completed")
