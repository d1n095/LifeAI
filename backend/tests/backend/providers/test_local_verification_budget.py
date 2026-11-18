"""Pre-flight verification budget and model choice for LOCAL runtimes.

Two local-first failure modes this pins:

  1. A cold local runtime loads model weights on the FIRST call (routinely ~15 s on CPU). With
     the cloud providers' 10 s pre-flight budget that call times out and the provider is
     recorded as `unreachable` — a false negative that parks a perfectly healthy import behind
     `awaiting_provider`. Local providers therefore get their own, longer budget, while cloud
     providers keep the fast-fail contract.

  2. On a local runtime there is no canonical model name to assume: what exists is whatever
     the founder pulled. "Test now" must prefer the model this provider last verified OK
     against; a failed check is not evidence of anything.
"""

from __future__ import annotations

import types
import uuid
from datetime import datetime

from app.config import get_settings
from app.models.provider_verification import ProviderVerificationCheck, VerificationResult
from app.providers import verification


def test_local_runtime_gets_the_longer_verification_budget():
    settings = get_settings()
    ollama = types.SimpleNamespace(name="ollama")
    assert verification.verification_timeout_seconds(ollama) == settings.local_verification_timeout_seconds
    assert verification.verification_timeout_seconds(ollama) > settings.provider_verification_timeout_seconds


def test_cloud_provider_keeps_the_fast_fail_budget():
    settings = get_settings()
    for name in ("openai", "gemini", "anthropic"):
        assert (
            verification.verification_timeout_seconds(types.SimpleNamespace(name=name))
            == settings.provider_verification_timeout_seconds
        )


def _record(db, *, model: str, result: VerificationResult) -> None:
    db.add(
        ProviderVerificationCheck(
            id=uuid.uuid4(),
            provider_name="ollama",
            role="embedding",
            model=model,
            result=result,
            message="test",
            checked_by="test",
            checked_at=datetime.utcnow(),
        )
    )
    db.commit()


def test_default_verification_model_reuses_the_last_verified_local_model(db_session):
    _record(db_session, model="nomic-embed-text:latest", result=VerificationResult.ok)
    ollama = types.SimpleNamespace(name="ollama")
    assert (
        verification._default_verification_model(ollama, "embedding", db_session)
        == "nomic-embed-text:latest"
    )


def test_default_verification_model_ignores_a_failed_check(db_session):
    _record(db_session, model="a-model-that-failed", result=VerificationResult.unreachable)
    ollama = types.SimpleNamespace(name="ollama")
    # Falls back to the documented default rather than trusting a model that never worked.
    assert verification._default_verification_model(ollama, "embedding", db_session) == "nomic-embed-text"
