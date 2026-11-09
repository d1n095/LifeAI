"""PRIORITY 2 — one connected synthetic demonstration of the Founder→source→Blueprint→readiness→
safe-approval flow, driven through the REAL HTTP/API boundary (not only service functions).

Flow proven end to end against real PostgreSQL/pgvector + RLS + the real Founder auth gate:
  Founder requirement -> authorized retrieval -> versioned, source-grounded Blueprint ->
  readiness explanation -> (source goes stale) -> UNSAFE approval REFUSED by the server ->
  (source restored) -> valid explicit Founder approval -> previous versions preserved ->
  no agent dispatched automatically at any point.

SYNTHETIC-MODEL LABEL: the chat/embedding provider is the repo's sanctioned deterministic fake
(monkeypatched OpenAIProvider.chat/.embed — never a real key), exactly as
test_founder_alpha_blueprint_e2e. Nothing here claims real external-provider execution; it proves
the integration/authorization/enforcement wiring around the model seam.
"""

import uuid

import pytest

from app.config import get_settings
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.models.user import User

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
DIM = get_settings().embedding_dim
MATCHING_VECTOR = [0.5] * DIM


@pytest.fixture
def _fake_model(monkeypatch):
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    async def _fake_chat(self, messages, model, **kwargs):
        user_msg = next((m.content for m in messages if m.role == "user"), "")
        n = user_msg.count("(document ")
        return ChatResult(content=f"BLUEPRINT (synthetic) from {n} source(s).", provider="openai", model=model, raw_usage={})

    async def _fake_embed(self, texts, model, **kwargs):
        return [MATCHING_VECTOR for _ in texts]

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)
    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed)


@pytest.fixture
def _no_dispatch(monkeypatch):
    """Arm the agent dispatch entrypoint to explode: the whole Blueprint flow must never
    automatically dispatch an agent or execute code."""
    import app.agent_coordination.dispatch as dispatch_mod

    def _explode(*args, **kwargs):
        raise AssertionError("Blueprint flow must not dispatch any agent automatically")

    monkeypatch.setattr(dispatch_mod, "dispatch_assignment", _explode, raising=False)


def _founder_id(superuser_db) -> uuid.UUID:
    return superuser_db.query(User).filter_by(email=FOUNDER_EMAIL).first().id


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()["csrf_token"]


def _make_indexed_source(superuser_db, owner_id, title: str) -> Document:
    from app.providers.registry import resolve_active

    _provider, model = resolve_active(superuser_db, role="embedding")
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
            embedding=MATCHING_VECTOR,
            embedding_model=model,
            embedding_native_dim=len(MATCHING_VECTOR),
        )
    )
    superuser_db.commit()
    return document


def test_connected_founder_to_blueprint_readiness_and_safe_approval(client, superuser_db, _fake_model, _no_dispatch):
    founder_id = _founder_id(superuser_db)
    doc = _make_indexed_source(superuser_db, founder_id, f"Historik {uuid.uuid4().hex[:5]}")
    csrf = _login(client)
    hdr = {"X-CSRF-Token": csrf}
    title = f"Blueprint {uuid.uuid4().hex[:6]}"

    # 1. Founder requirement -> authorized retrieval -> versioned, source-grounded draft (v1).
    gen1 = client.post("/api/blueprint/drafts", json={"title": title, "query": "projektets historia"}, headers=hdr)
    assert gen1.status_code == 201, gen1.text
    v1 = gen1.json()
    assert v1["status"] == "draft" and v1["version"] == 1
    assert str(doc.id) in v1["source_document_ids"]

    # 2. A revised version (v2) — previous version is preserved.
    gen2 = client.post(f"/api/blueprint/drafts/{v1['id']}/regenerate", json={"query": "uppdaterad historia"}, headers=hdr)
    assert gen2.status_code == 201, gen2.text
    v2 = gen2.json()
    assert v2["version"] == 2

    # 3. Readiness explanation: READY (source active, no open questions).
    r = client.get(f"/api/blueprint/drafts/{v2['id']}/readiness", headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()["readiness"] == "READY"
    assert r.json()["blocks_approval"] is False

    # 4. Source becomes STALE (Founder reclassifies it historical).
    superuser_db.refresh(doc)
    doc.active_truth_status = ActiveTruthStatus.historical
    superuser_db.commit()

    r_stale = client.get(f"/api/blueprint/drafts/{v2['id']}/readiness", headers=hdr)
    assert r_stale.json()["readiness"] == "STALE"
    assert r_stale.json()["blocks_approval"] is True

    # 5. UNSAFE approval is REFUSED by the server (not merely a frontend warning).
    bad = client.post(f"/api/blueprint/drafts/{v2['id']}/approve", headers=hdr)
    assert bad.status_code == 409, bad.text
    assert "approval_blocked" in bad.text
    still = client.get(f"/api/blueprint/drafts/{v2['id']}", headers=hdr)
    assert still.json()["status"] == "draft"  # nothing approved

    # 6. Source restored -> valid explicit Founder approval succeeds.
    superuser_db.refresh(doc)
    doc.active_truth_status = ActiveTruthStatus.active
    superuser_db.commit()
    ok = client.post(f"/api/blueprint/drafts/{v2['id']}/approve", headers=hdr)
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "approved"
    assert ok.json()["approved_by"] == str(founder_id)

    # 7. Previous version preserved and NOT auto-approved by approving v2.
    drafts = client.get("/api/blueprint/drafts", params={"title": title}, headers=hdr).json()
    by_version = {d["version"]: d for d in drafts}
    assert set(by_version) == {1, 2}
    assert by_version[1]["status"] == "draft"  # v1 untouched / history preserved

    # 8. No agent was dispatched automatically anywhere in the flow (the _no_dispatch fixture
    #    would have raised). Reaching here proves it.
