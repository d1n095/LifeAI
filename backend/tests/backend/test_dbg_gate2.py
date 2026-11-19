from app.rag.graph_instructions import format_graph_context_instructions, _content_tokens
from app.rag.evidence_metadata import ClaimEvidenceMetadata
from sqlalchemy import text as t

def test_dbg(db_session, client, monkeypatch):
    from tests.backend.test_scientific_reasoning import _study, get_dim
    from app.models.user import User
    from tests.backend.test_claim_graph_integration import _login
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider
    state = {}
    async def _embed(self, texts, model, **kwargs):
        return [[0.1] * get_dim() for _ in texts]
    async def _chat(self, messages, model, **kwargs):
        state["system"] = messages[0].content
        return ChatResult(content="Svar.", provider="openai", model=model, raw_usage={})
    monkeypatch.setattr(OpenAIProvider, "embed", _embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _chat)
    founder = db_session.query(User).filter(User.email == "founder@lifeos.local").first()
    _study(db_session, founder.id, topic="sci_dbg", meta={"study_design": "randomized_trial", "sample_size": 100, "competing_explanations": ["controlled"]})
    _study(db_session, founder.id, topic="sci_dbg2", meta={"study_design": "in_vitro", "_text": "Kaffe och puls mekanistik."})
    csrf = _login(client)
    # retrieve directly to see hit order
    from app.rag.retrieve import retrieve_context
    import asyncio
    hits = asyncio.get_event_loop().run_until_complete(retrieve_context(db_session, founder.id, "Vad vet vi om kaffe och puls?", top_k=5))
    print("HITS:", [(h.get("title"), h.get("text", "")[:40]) for h in hits])
    res = client.post("/api/chat", json={"conversation_id": None, "message": "Vad vet vi om kaffe och puls?"}, headers={"X-CSRF-Token": csrf})
    sys = state["system"]
    print("SCIENCE:", "VETENSKAPLIG" in sys, "| GRAF:", "GRAFKONTEXT" in sys)
    rows = db_session.query(ClaimEvidenceMetadata).filter(ClaimEvidenceMetadata.owner_id == founder.id).all()
    print("TOPICS:", [r.metadata_json.get("research_topic") for r in rows])
    assert False, "dbg"
