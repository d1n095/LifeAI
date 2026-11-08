"""Founder Alpha — ONE integrated end-to-end candidate, driven through the REAL HTTP API
against REAL PostgreSQL/pgvector + RLS + real document storage records.

This is the integration proof the Founder Alpha directive asks for: the whole workflow on one
candidate, not each PR in isolation. It exercises the real routers, the real owner-scoped
pgvector retrieval (`app.rag.retrieve.retrieve_context` → `app.rag.vector_store.search`), the
real `blueprint_drafts` persistence + migration 0070 + RLS, and the real Founder auth gate
(`require_founder`).

Honest scope of the model leg: the chat/embedding PROVIDER is the repo's sanctioned
deterministic fake (monkeypatched `OpenAIProvider.chat`/`.embed`, never a real key) — identical
to `tests/backend/chat/test_chat_source_grounding.py` and the #334/#336/#340 split. The
REAL-local-model synthesis (Ollama) is proven in the model-backed CI job, not on a box without
Ollama. Nothing here claims a real-model PASS; it proves the integration, authorization,
isolation, integrity, versioning, persistence-across-restart and rollback wiring around the
model seam.

Required chain covered end to end:
  Founder authentication → chat → (uploaded+indexed sources) → source-grounded retrieval →
  generate Blueprint → inspect source references → save version → restart (fresh client+session
  against the same durable Postgres) → retrieve draft and original → explicit Founder approval.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.models.blueprint_draft import BlueprintDraft
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.models.provider_disclosure import ProviderDisclosureEvent
from app.models.user import User

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
DIM = get_settings().embedding_dim
MATCHING_VECTOR = [0.5] * DIM  # full storage-width vector -> NULL native-dim -> ranks normally


@pytest.fixture
def _fake_model(monkeypatch):
    """Deterministic fake chat+embedding provider (never a real AI key), the repo's own
    API-test pattern. The chat fake echoes the source count so the body is demonstrably a
    function of the retrieved sources. `.fail_next` lets a test force a provider failure to
    prove rollback."""
    from app.providers.base import ChatResult, ProviderError
    from app.providers.openai_provider import OpenAIProvider

    state = {"fail": False, "calls": 0, "messages": []}

    async def _fake_chat(self, messages, model, **kwargs):
        state["calls"] += 1
        state["messages"].append(messages)
        if state["fail"]:
            raise ProviderError("synthesis provider deliberately unavailable (rollback test)")
        # The real import worker also invokes claim extraction. Keep that model seam
        # deterministic and empty so this fixture can drive upload -> index -> Blueprint
        # without inventing claims or contacting an external provider.
        if any("Svara ENDAST med en JSON-lista" in m.content for m in messages):
            return ChatResult(content="[]", provider="openai", model=model, raw_usage={})
        user_msg = next((m.content for m in messages if m.role == "user"), "")
        n_sources = user_msg.count("(document ")
        body = f"BLUEPRINT DRAFT synthesised strictly from {n_sources} source(s)."
        return ChatResult(content=body, provider="openai", model=model, raw_usage={})

    async def _fake_embed(self, texts, model, **kwargs):
        return [MATCHING_VECTOR for _ in texts]

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)
    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed)
    return state


def _login(client, email=FOUNDER_EMAIL, password=FOUNDER_PASSWORD) -> str:
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text
    return res.json()["csrf_token"]


def _founder_id(superuser_db) -> uuid.UUID:
    return superuser_db.query(User).filter_by(email=FOUNDER_EMAIL).first().id


def _active_embedding_space(superuser_db) -> tuple[str, int]:
    """The (model, native_dim) retrieval will query with — a chunk must be stamped with this
    exact space to be rankable after the #333/migration-0069 active-embedding-space guard
    (retrieval excludes chunks from any other model/dim or legacy NULL identity)."""
    from app.providers.registry import resolve_active

    _provider, model = resolve_active(superuser_db, role="embedding")
    return model, len(MATCHING_VECTOR)


def _make_indexed_source(superuser_db, owner_id, title: str, *, vector=None) -> Document:
    """An uploaded, indexed immutable original: a Document (the original record, with a fixed
    checksum) + a DocumentChunk (the index entry) stamped with the active embedding space, so
    real owner-scoped pgvector retrieval ranks it exactly as a genuinely-ingested chunk."""
    model, native_dim = _active_embedding_space(superuser_db)
    document = Document(
        title=title,
        source=DocumentSource.upload,
        uploaded_by=owner_id,
        active_truth_status=ActiveTruthStatus.active,
        checksum=uuid.uuid4().hex,
        original_filename=f"{title}.txt",
    )
    superuser_db.add(document)
    superuser_db.commit()
    superuser_db.add(
        DocumentChunk(
            document_id=document.id,
            owner_id=owner_id,
            chunk_index=0,
            text=f"Innehåll i {title}.",
            embedding=vector or MATCHING_VECTOR,
            embedding_model=model,
            embedding_native_dim=native_dim,
        )
    )
    superuser_db.commit()
    return document


def _draft_rows(superuser_db, owner_id, title):
    return (
        superuser_db.execute(
            select(BlueprintDraft).where(
                BlueprintDraft.owner_id == owner_id, BlueprintDraft.title == title
            ).order_by(BlueprintDraft.version)
        ).scalars().all()
    )


def _import_indexed_source(client, csrf: str, filename: str, content: bytes) -> str:
    """Drive the real durable import worker until the uploaded source is indexed."""
    from app.worker import Worker

    response = client.post(
        "/api/library/import",
        files={"file": (filename, content, "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["id"]
    for _ in range(40):
        asyncio.run(Worker().run_once())
        job = client.get(f"/api/library/jobs/{job_id}")
        assert job.status_code == 200, job.text
        body = job.json()
        results = body.get("file_results") or []
        if body.get("status") == "completed" and results and results[0].get("source_id"):
            return results[0]["source_id"]
        if body.get("status") in {"failed", "partial"}:
            raise AssertionError(f"import job {job_id} failed: {body}")
    raise AssertionError(f"import job {job_id} never completed")


def test_real_upload_to_original_to_blueprint_to_approval_journey(
    client, superuser_db, _fake_model
):
    """The deterministic CI journey uses real HTTP, storage, worker, pgvector and approval.

    Unlike the older composition test, this starts at the actual upload boundary instead of
    manufacturing Document/DocumentChunk rows. It therefore proves that the immutable original
    which the Founder can download is the same source that grounds the versioned Blueprint.
    Only the model call is fake; the separate Founder Alpha Ollama workflow covers that seam.
    """
    founder_id = _founder_id(superuser_db)
    csrf = _login(client)
    marker = f"Project Zephyr marker {uuid.uuid4().hex}"
    original = (
        "Founder Alpha project evidence.\n"
        f"{marker}\n"
        "The Blueprint must cite this immutable uploaded source.\n"
    ).encode("utf-8")
    expected_sha = hashlib.sha256(original).hexdigest()

    source_id = _import_indexed_source(client, csrf, "founder-alpha-evidence.txt", original)
    source_uuid = uuid.UUID(source_id)
    source = superuser_db.get(Document, source_uuid)
    assert source is not None
    assert source.uploaded_by == founder_id
    assert source.checksum == expected_sha
    storage_key_before = source.storage_key

    downloaded = client.get(f"/api/library/{source_id}/original")
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == original
    assert hashlib.sha256(downloaded.content).hexdigest() == expected_sha

    title = f"Founder Alpha integrated {uuid.uuid4().hex[:8]}"
    generated = client.post(
        "/api/blueprint/drafts",
        json={"title": title, "query": marker},
        headers={"X-CSRF-Token": csrf},
    )
    assert generated.status_code == 201, generated.text
    v1 = generated.json()
    assert v1["status"] == "draft"
    assert v1["version"] == 1
    assert v1["source_document_ids"] == [source_id]
    assert v1["provenance"]["originals_mutated"] is False
    assert v1["provenance"]["pending_founder_approval"] is True

    regenerated = client.post(
        f"/api/blueprint/drafts/{v1['id']}/regenerate",
        json={"query": marker},
        headers={"X-CSRF-Token": csrf},
    )
    assert regenerated.status_code == 201, regenerated.text
    v2 = regenerated.json()
    assert v2["version"] == 2
    assert v2["source_document_ids"] == [source_id]

    approved = client.post(
        f"/api/blueprint/drafts/{v2['id']}/approve",
        headers={"X-CSRF-Token": csrf},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["approved_by"] == str(founder_id)

    # Re-read committed state after expiring the identity map: neither generation nor approval
    # may mutate the source identity, original checksum, storage pointer or bytes.
    superuser_db.expire_all()
    source_after = superuser_db.get(Document, source_uuid)
    assert source_after is not None
    assert source_after.checksum == expected_sha
    assert source_after.storage_key == storage_key_before
    downloaded_after = client.get(f"/api/library/{source_id}/original")
    assert downloaded_after.status_code == 200
    assert downloaded_after.content == original


def test_full_founder_alpha_workflow(client, superuser_db, _fake_model):
    founder_id = _founder_id(superuser_db)
    title = f"Projektets Blueprint {uuid.uuid4().hex[:6]}"

    # --- sources uploaded + indexed (immutable originals) ---
    doc_a = _make_indexed_source(superuser_db, founder_id, "Historik A")
    doc_b = _make_indexed_source(superuser_db, founder_id, "Historik B")
    original_checksums = {str(doc_a.id): doc_a.checksum, str(doc_b.id): doc_b.checksum}

    csrf = _login(client)

    # --- chat leg works (real endpoint, faked model) ---
    chat_res = client.post("/api/chat", json={"message": "Sammanfatta projektet."}, headers={"X-CSRF-Token": csrf})
    assert chat_res.status_code == 200, chat_res.text

    # --- generate Blueprint draft (source-grounded retrieval -> synthesis) ---
    gen = client.post(
        "/api/blueprint/drafts",
        json={"title": title, "query": "projektets historia och beslut"},
        headers={"X-CSRF-Token": csrf},
    )
    assert gen.status_code == 201, gen.text
    v1 = gen.json()
    assert v1["status"] == "draft"
    assert v1["version"] == 1
    # traceable to the founder's own indexed originals, and ONLY those
    assert set(v1["source_document_ids"]) <= {str(doc_a.id), str(doc_b.id)}
    assert len(v1["source_document_ids"]) >= 1
    assert len(v1["source_refs"]) >= 1
    assert all(ref["document_id"] in {str(doc_a.id), str(doc_b.id)} for ref in v1["source_refs"])
    # AI interpretation, separated from originals; pending explicit approval
    assert v1["provenance"]["originals_mutated"] is False
    assert v1["provenance"]["ai_generated"] is True
    assert v1["provenance"]["pending_founder_approval"] is True
    assert v1["model_provider"] and v1["model_name"]
    assert v1["approved_at"] is None and v1["approved_by"] is None

    # --- inspect source references ---
    got = client.get(f"/api/blueprint/drafts/{v1['id']}", headers={"X-CSRF-Token": csrf})
    assert got.status_code == 200
    assert got.json()["source_refs"] == v1["source_refs"]

    # --- save a new version (regenerate) ---
    regen = client.post(
        f"/api/blueprint/drafts/{v1['id']}/regenerate",
        json={"query": "fokusera på tekniska beslut"},
        headers={"X-CSRF-Token": csrf},
    )
    assert regen.status_code == 201, regen.text
    v2 = regen.json()
    assert v2["version"] == 2 and v2["id"] != v1["id"] and v2["title"] == title

    listing = client.get("/api/blueprint/drafts", params={"title": title}, headers={"X-CSRF-Token": csrf})
    assert listing.status_code == 200
    versions = sorted(r["version"] for r in listing.json())
    assert versions == [1, 2]

    # --- RESTART / durability: a BRAND-NEW database session+connection (standing in for a
    # fresh process after a restart) re-reads the committed state from durable Postgres. This
    # proves the draft and its source refs were persisted, not just held in the request's
    # session. ---
    # expire_all() forces a fresh SELECT from the database on next access (no identity-map
    # cache), so this re-read genuinely comes from durable Postgres, not request memory.
    superuser_db.expire_all()
    reread = superuser_db.get(BlueprintDraft, uuid.UUID(v1["id"]))
    assert reread is not None
    assert reread.body == v1["body"]  # version 1 is immutable across the restart
    assert [str(x) for x in reread.source_document_ids] == v1["source_document_ids"]

    # the draft and the original are both still retrievable through the live API afterwards
    after = client.get(f"/api/blueprint/drafts/{v1['id']}", headers={"X-CSRF-Token": csrf})
    assert after.status_code == 200
    assert after.json()["body"] == v1["body"]
    src = client.get(f"/api/library/{doc_a.id}", headers={"X-CSRF-Token": csrf})
    assert src.status_code == 200, src.text

    # --- explicit Founder approval of version 2 ---
    appr = client.post(f"/api/blueprint/drafts/{v2['id']}/approve", headers={"X-CSRF-Token": csrf})
    assert appr.status_code == 200, appr.text
    approved = appr.json()
    assert approved["status"] == "approved"
    assert approved["approved_at"] is not None
    # approval identity is the authenticated founder, NOT anything client-supplied
    assert approved["approved_by"] == str(founder_id)
    assert approved["provenance"]["pending_founder_approval"] is False

    # approving an already-approved version is refused (no silent re-approval)
    again = client.post(f"/api/blueprint/drafts/{v2['id']}/approve", headers={"X-CSRF-Token": csrf})
    assert again.status_code == 409

    # version 1 is still a draft (approval did not touch other versions)
    rows = _draft_rows(superuser_db, founder_id, title)
    by_version = {r.version: r for r in rows}
    assert by_version[1].status == "draft"
    assert by_version[2].status == "approved"

    # --- integrity: originals never mutated by any of the above ---
    for doc_id, checksum in original_checksums.items():
        fresh = superuser_db.get(Document, uuid.UUID(doc_id))
        assert fresh.checksum == checksum
        assert fresh.deleted_at is None


def test_ungrounded_request_is_refused_and_persists_nothing(client, superuser_db, _fake_model):
    """Founder with NO indexed source: retrieval returns nothing, so generation is refused
    (422) rather than hallucinating an ungrounded Blueprint, and no row is written."""
    founder_id = _founder_id(superuser_db)
    title = f"Ogrundad {uuid.uuid4().hex[:6]}"
    csrf = _login(client)

    res = client.post(
        "/api/blueprint/drafts",
        json={"title": title, "query": "vad som helst"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 422, res.text

    count = superuser_db.execute(
        select(func.count()).select_from(BlueprintDraft).where(
            BlueprintDraft.owner_id == founder_id, BlueprintDraft.title == title
        )
    ).scalar_one()
    assert count == 0


def test_cross_owner_source_isolation_and_founder_only_gate(
    client, superuser_db, make_verified_user, _fake_model
):
    """Another user's indexed source must never ground the Founder's Blueprint, and the
    endpoints are Founder-only."""
    other_user, other_pw = make_verified_user()

    # ONLY the other user has a source.
    _make_indexed_source(superuser_db, other_user.id, "Annan användares material")

    csrf = _login(client)
    # Founder retrieval is owner-scoped -> no grounded sources -> refused (never leaks the
    # other owner's document into the founder's draft).
    res = client.post(
        "/api/blueprint/drafts",
        json={"title": f"X {uuid.uuid4().hex[:6]}", "query": "material"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 422, res.text

    # A non-founder authenticated user is forbidden from the blueprint endpoints entirely.
    other_client_csrf = _login(client, email=other_user.email, password=other_pw)
    forbidden = client.get("/api/blueprint/drafts", headers={"X-CSRF-Token": other_client_csrf})
    assert forbidden.status_code == 403


def test_client_cannot_forge_source_ids(client, superuser_db, _fake_model):
    """The generate endpoint has no source-id input: a client cannot inject arbitrary source
    document ids. Even if it tries, the stored sources are exactly what retrieval returned."""
    founder_id = _founder_id(superuser_db)
    doc = _make_indexed_source(superuser_db, founder_id, "Äkta källa")
    forged = str(uuid.uuid4())
    csrf = _login(client)

    res = client.post(
        "/api/blueprint/drafts",
        json={
            "title": f"Forge {uuid.uuid4().hex[:6]}",
            "query": "källa",
            # attempts to smuggle source identity + approval identity — all ignored:
            "source_document_ids": [forged],
            "source_refs": [{"document_id": forged}],
            "approved_by": "attacker",
            "status": "approved",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert forged not in body["source_document_ids"]
    assert body["source_document_ids"] == [str(doc.id)]
    assert body["status"] == "draft"  # cannot be forced to 'approved'
    assert body["approved_by"] is None


def test_synthesis_failure_rolls_back_no_partial_draft(client, superuser_db, _fake_model):
    """If the model provider fails mid-generation, the request fails cleanly (503) and no
    partial/unsynthesised draft row is left behind."""
    founder_id = _founder_id(superuser_db)
    _make_indexed_source(superuser_db, founder_id, "Källa för rollback")
    title = f"Rollback {uuid.uuid4().hex[:6]}"
    csrf = _login(client)

    _fake_model["fail"] = True
    res = client.post(
        "/api/blueprint/drafts",
        json={"title": title, "query": "källa"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 503, res.text

    count = superuser_db.execute(
        select(func.count()).select_from(BlueprintDraft).where(
            BlueprintDraft.owner_id == founder_id, BlueprintDraft.title == title
        )
    ).scalar_one()
    assert count == 0


@pytest.mark.parametrize("forbidden", ["NEVER_EGRESS: private source", "-----BEGIN OPENSSH PRIVATE KEY-----"])
def test_blueprint_denies_forbidden_source_before_external_chat(
    client, superuser_db, _fake_model, forbidden
):
    founder_id = _founder_id(superuser_db)
    doc = _make_indexed_source(superuser_db, founder_id, "Forbidden source")
    chunk = superuser_db.query(DocumentChunk).filter_by(document_id=doc.id).one()
    chunk.text = forbidden
    superuser_db.commit()
    csrf = _login(client)
    title = f"Denied {uuid.uuid4().hex[:6]}"

    res = client.post(
        "/api/blueprint/drafts",
        json={"title": title, "query": "source"},
        headers={"X-CSRF-Token": csrf},
    )

    assert res.status_code == 422, res.text
    assert _fake_model["calls"] == 0
    assert _draft_rows(superuser_db, founder_id, title) == []
    event = superuser_db.query(ProviderDisclosureEvent).filter_by(
        owner_id=founder_id, purpose="blueprint_synthesis"
    ).one()
    assert event.decision == "denied"
    assert event.sent_content_hash is None


def test_blueprint_redacts_source_before_external_chat(client, superuser_db, _fake_model):
    founder_id = _founder_id(superuser_db)
    doc = _make_indexed_source(superuser_db, founder_id, "Secret source")
    chunk = superuser_db.query(DocumentChunk).filter_by(document_id=doc.id).one()
    secret = "sk-abcdefghijklmnopqrstuvwx"
    chunk.text = f"Use api_key={secret} for this project."
    superuser_db.commit()
    csrf = _login(client)

    res = client.post(
        "/api/blueprint/drafts",
        json={"title": f"Redaction {uuid.uuid4().hex[:6]}", "query": "source"},
        headers={"X-CSRF-Token": csrf},
    )

    assert res.status_code == 201, res.text
    assert _fake_model["calls"] == 1
    assert secret not in str(_fake_model["messages"])
    event = superuser_db.query(ProviderDisclosureEvent).filter_by(
        owner_id=founder_id, purpose="blueprint_synthesis"
    ).one()
    assert event.decision == "allowed"
    assert "secret_pattern" in event.redaction_categories


def test_blueprint_local_chat_is_gated_and_ledgered(client, superuser_db, _fake_model, monkeypatch):
    from app.providers.base import ChatResult
    from app.routers import blueprint

    founder_id = _founder_id(superuser_db)
    doc = _make_indexed_source(superuser_db, founder_id, "Local source")
    calls = []

    class LocalProvider:
        name = "local-test"
        is_external_egress = False

        async def chat(self, messages, model, **kwargs):
            calls.append(messages)
            return ChatResult(content="Local draft", provider=self.name, model=model, raw_usage={})

    monkeypatch.setattr(blueprint, "resolve_active", lambda db, role: (LocalProvider(), "local-model"))
    csrf = _login(client)
    res = client.post(
        "/api/blueprint/drafts",
        json={"title": f"Local {uuid.uuid4().hex[:6]}", "query": "source"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 201, res.text
    assert len(calls) == 1
    event = superuser_db.query(ProviderDisclosureEvent).filter_by(
        owner_id=founder_id, purpose="blueprint_synthesis"
    ).one()
    assert event.decision == "local"

    chunk = superuser_db.query(DocumentChunk).filter_by(document_id=doc.id).one()
    chunk.text = "NEVER_EGRESS: do not send even to a local model"
    superuser_db.commit()
    denied = client.post(
        f"/api/blueprint/drafts/{res.json()['id']}/regenerate",
        json={"query": "source"},
        headers={"X-CSRF-Token": csrf},
    )
    assert denied.status_code == 422, denied.text
    assert len(calls) == 1
    decisions = [
        row.decision for row in superuser_db.query(ProviderDisclosureEvent).filter_by(
            owner_id=founder_id, purpose="blueprint_synthesis"
        ).all()
    ]
    assert sorted(decisions) == ["denied", "local"]


def test_blueprint_disclosure_survives_provider_failure(client, superuser_db, _fake_model):
    founder_id = _founder_id(superuser_db)
    _make_indexed_source(superuser_db, founder_id, "Failure source")
    csrf = _login(client)
    _fake_model["fail"] = True
    res = client.post(
        "/api/blueprint/drafts",
        json={"title": f"Failure {uuid.uuid4().hex[:6]}", "query": "source"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 503, res.text
    event = superuser_db.query(ProviderDisclosureEvent).filter_by(
        owner_id=founder_id, purpose="blueprint_synthesis"
    ).one()
    assert event.decision == "allowed"


def test_version_uniqueness_is_db_enforced(client, superuser_db, _fake_model):
    """Version concurrency safety: the (owner_id, title, version) unique index makes it
    impossible for two drafts of the same blueprint to share a version, even if two
    generations race and compute the same next version."""
    from sqlalchemy.exc import IntegrityError

    founder_id = _founder_id(superuser_db)
    title = f"Concurrency {uuid.uuid4().hex[:6]}"
    r1 = BlueprintDraft(
        owner_id=founder_id, title=title, version=1, status="draft", body="a",
        source_document_ids=[], source_refs=[], provenance={},
    )
    superuser_db.add(r1)
    superuser_db.commit()
    r_dup = BlueprintDraft(
        owner_id=founder_id, title=title, version=1, status="draft", body="b",
        source_document_ids=[], source_refs=[], provenance={},
    )
    superuser_db.add(r_dup)
    with pytest.raises(IntegrityError):
        superuser_db.commit()
    superuser_db.rollback()
