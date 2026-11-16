"""Supervised local-first historical-ingestion -> retrieval demonstration.

This is the end-to-end proof for the Life "historical knowledge integration" unit (see
docs/LIFE_LOCAL_FIRST_INGESTION_DEMO.md and the recon in PR #325): a set of the Founder's
documents is ingested through the REAL library-import pipeline (storage -> extract -> chunk ->
egress-gated embed -> pgvector upsert) using an ON-HOST local embedding provider (Ollama), and
after a worker/session RESTART an accurate answer is retrieved and linked back to its original
source -- while the disclosure ledger proves nothing left the host.

It uses synthetic data and a DETERMINISTIC local-embedding stub (a hashed bag-of-words), so it
is reproducible offline with no real model, no network and no paid provider. Swapping in a real
Ollama embedding model is a separate, Founder-gated step (its vector dimension differs from the
hardwired embedding_dim=1536 -- the architectural blocker reported in the recon).

Acceptance criteria asserted below (recon §I):
  1. ingestion runs end-to-end on the real pipeline and the documents reach `indexed`;
  2. the embedding provider used is the local (on-host) one;
  3. retrieval happens on a FRESH session (worker/session restart), reading only durable state;
  4. the retrieved answer is the correct one among several sources (selective retrieval);
  5. the hit is linked to its exact original source (document id + title);
  6-7. NO external-AI disclosure occurred: every disclosure-ledger row for this owner is
       `local` or `denied`, never `allowed`; both the ingest and the retrieval embeds are
       present as `local`.
"""

import math
import re

import pytest
from sqlalchemy import select
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.models.document import Document, IndexStatus
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.provider_config import ProviderConfig
from app.models.provider_disclosure import ProviderDisclosureEvent
from app.providers.ollama_provider import OllamaProvider
from app.rag.library_import import run_import_job
from app.rag.retrieve import retrieve_context
from app.request_context import current_user_id as current_user_id_var
from app.storage import get_storage

EMBEDDING_DIM = get_settings().embedding_dim

# A synthetic "historical" corpus: three separate documents, each carrying one fact with a
# distinctive rare token. Ingested as three independent import jobs so retrieval must SELECT
# the right source among several, not just return the only thing present.
_DOCS = {
    "flytt-2019.txt": "Familjen flyttade till Granbergavagen under varfloden MILNINGSVECKAN artonde.",
    "foretag-nord.txt": "Bolaget Nordklatt grundades med organisationsnummer KVARNFALK trettiotre.",
    "bilen-2021.txt": "Den bla bilen registrerades med fastkoden ZIRKONPASS sjuttiotva.",
}


def _tokenize(s: str) -> list[str]:
    return re.findall(r"[a-z0-9aao]+", s.lower())


def _deterministic_local_embed(texts: list[str]) -> list[list[float]]:
    """A hashed bag-of-words embedding: each token lands in a fixed bucket, the vector is
    L2-normalized. Deterministic and offline -- two strings that share a distinctive token are
    close; a query that is a unique token retrieves exactly the chunk that contains it. Good
    enough to prove the retrieval wiring without a real embedding model."""
    out: list[list[float]] = []
    for t in texts:
        vec = [0.0] * EMBEDDING_DIM
        for tok in _tokenize(t):
            vec[hash(tok) % EMBEDDING_DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        out.append(vec)
    return out


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _read_chunk_for(data: bytes, size: int = 1 << 16):
    pos = 0

    def _read():
        nonlocal pos
        chunk = data[pos : pos + size]
        pos += len(chunk)
        return chunk

    return _read


def _make_job(db_session, owner_id, raw: bytes, filename: str) -> ImportJob:
    _set_rls_user(db_session, owner_id)
    blob = get_storage().write_stream(_read_chunk_for(raw), max_bytes=max(len(raw), 1))
    job = ImportJob(
        owner_id=owner_id,
        status=ImportJobStatus.pending,
        source_filename=filename,
        source_checksum=blob.sha256,
        source_storage_key=blob.storage_key,
        source_size_bytes=blob.size_bytes,
    )
    db_session.add(job)
    db_session.commit()
    return job


@pytest.mark.asyncio
async def test_local_first_ingestion_then_cross_session_retrieval_with_zero_external_egress(
    db_session, superuser_db, make_verified_user, monkeypatch
):
    # (2) Make the ON-HOST local provider (Ollama, default loopback/in-deployment base URL) the
    # active embedding provider, and stub its embed() deterministically (the stub also answers
    # the verification probe, so ensure_verified() passes offline).
    monkeypatch.setattr(OllamaProvider, "is_external_egress", property(lambda self: False))

    async def _fake_local_embed(self, texts, model, **kwargs):
        return _deterministic_local_embed(texts)

    monkeypatch.setattr(OllamaProvider, "embed", _fake_local_embed)

    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    db_session.add(ProviderConfig(role="embedding", provider="ollama", model="nomic-embed-text", is_active=True))
    db_session.commit()

    # (1) Ingest the three documents through the real end-to-end import pipeline.
    doc_ids: dict[str, str] = {}
    for filename, body in _DOCS.items():
        job = _make_job(db_session, user.id, body.encode("utf-8"), filename)
        await run_import_job(db_session, job.id, user.id)
        doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename=filename).one()
        assert doc.status == IndexStatus.indexed, f"{filename}: {doc.status} / {doc.error_message}"
        assert doc.chunk_count and doc.chunk_count >= 1
        doc_ids[filename] = str(doc.id)

    # (3) Worker/session RESTART: discard the ingest session entirely and open a brand-new one.
    # Retrieval must work from durable DB state alone -- nothing is carried over in memory.
    db_session.close()
    from app.db import SessionLocal

    fresh_db = SessionLocal()
    try:
        _set_rls_user(fresh_db, user.id)
        # (4)+(5) Ask for the fact unique to one source; the distinctive token selects exactly
        # that document's chunk, and the hit carries its original-source identity.
        hits = await retrieve_context(fresh_db, user.id, "ZIRKONPASS", top_k=3)
        assert hits, "retrieval returned nothing after restart"
        top = hits[0]
        assert "ZIRKONPASS" in top["text"]  # the correct fact
        assert top["document_id"] == doc_ids["bilen-2021.txt"]  # linked to its exact source
        assert top["title"] == "bilen-2021.txt"
        # selective: the other sources did not win
        assert "KVARNFALK" not in top["text"] and "MILNINGSVECKAN" not in top["text"]
        # Commit so the retrieval embed's disclosure row is durable (a real request's
        # transaction commits; enforce_egress_policy only flushes, by design).
        fresh_db.commit()
    finally:
        fresh_db.close()

    # (6)+(7) No-egress invariant: every disclosure-ledger row for this owner is on-host or a
    # denial -- never an external `allowed` disclosure -- and both the ingest and the retrieval
    # embeds are recorded as `local`.
    rows = superuser_db.execute(
        select(ProviderDisclosureEvent).where(ProviderDisclosureEvent.owner_id == user.id)
    ).scalars().all()
    assert rows, "expected disclosure-ledger rows for the embedding calls"
    assert all(r.decision in ("local", "denied") for r in rows)
    assert not any(r.decision == "allowed" for r in rows), "content left the host to an external AI"
    purposes_local = {r.purpose for r in rows if r.decision == "local"}
    assert "rag_ingest" in purposes_local
    assert "rag_retrieval" in purposes_local
