"""Acceptance for the tool capability view.

It must reuse the existing manifest, runtime status check and founder gate -- and it must
never let a claim stand in for evidence: an unavailable provider, a raising config read, a
denied identity and a record without a verification timestamp all stay visibly unresolved.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.founder import FOUNDER_USER_ID
from app.mainai_executive import capability_truth
from app.models.user import UserRole


def _founder_identity():
    # require_founder reads exactly these two attributes; the gate itself is the real one.
    return SimpleNamespace(id=FOUNDER_USER_ID, role=UserRole.founder)


def _by_key(view) -> dict:
    return {tool.key: tool for tool in view.tools}


def test_founder_identity_is_permitted_but_nothing_is_executed(superuser_db):
    view = capability_truth.tool_capability_view(superuser_db, user=_founder_identity())

    assert view.tools
    assert all(tool.permitted is True for tool in view.tools)
    assert view.execution_authorized is False


def test_a_non_founder_identity_is_denied_while_availability_is_unchanged(
    superuser_db, make_verified_user
):
    user, _ = make_verified_user()

    view = capability_truth.tool_capability_view(superuser_db, user=user)
    corpus = _by_key(view)["corpus_review"]

    assert corpus.permitted is False
    assert corpus.implemented is True
    assert view.execution_authorized is False


def test_missing_identity_leaves_permission_unknown(superuser_db):
    view = capability_truth.tool_capability_view(superuser_db)

    assert all(tool.permitted is None for tool in view.tools)
    assert all(tool.verified is None for tool in view.tools)


def test_a_missing_provider_configuration_is_reported_unavailable(superuser_db, monkeypatch):
    from app.providers.openai_provider import OpenAIProvider

    monkeypatch.setattr(OpenAIProvider, "is_configured", lambda self: False)

    corpus = _by_key(capability_truth.tool_capability_view(superuser_db))["corpus_review"]

    assert corpus.implemented is True
    assert corpus.configured is False
    assert corpus.available is False
    assert corpus.unavailable_reason == "not_configured"


def test_a_raising_config_read_fails_closed(superuser_db, monkeypatch):
    from app.providers.openai_provider import OpenAIProvider

    def _malformed(self):
        raise RuntimeError("provider returned an incomplete health payload")

    monkeypatch.setattr(OpenAIProvider, "is_configured", _malformed)

    corpus = _by_key(capability_truth.tool_capability_view(superuser_db))["corpus_review"]

    assert corpus.configured is False
    assert corpus.available is False
    assert corpus.unavailable_reason == "not_configured"


def test_verified_requires_a_record_and_a_verification_timestamp(
    superuser_db, make_verified_user
):
    from app.capability_reality import record_capability_observation
    from app.models.capability_reality import CapabilityRecord

    user, _ = make_verified_user()
    record_capability_observation(
        superuser_db,
        owner_id=user.id,
        capability_key="corpus_review",
        domain="runtime",
        status="verified_available",
    )

    view = capability_truth.tool_capability_view(superuser_db, user=user)
    corpus = _by_key(view)["corpus_review"]
    assert corpus.verified is False, "a verified claim without a timestamp is not evidence"
    assert corpus.note and "verifieringstidsstämpel" in corpus.note
    assert _by_key(view)["task_execution"].verified is None, "no record means unknown"

    row = (
        superuser_db.query(CapabilityRecord)
        .filter_by(owner_id=user.id, capability_key="corpus_review")
        .one()
    )
    row.last_verified_at = datetime.utcnow()
    superuser_db.commit()

    refreshed = _by_key(capability_truth.tool_capability_view(superuser_db, user=user))
    assert refreshed["corpus_review"].verified is True


def test_an_unknown_status_stays_unknown(superuser_db, make_verified_user):
    from app.capability_reality import record_capability_observation

    user, _ = make_verified_user()
    record_capability_observation(
        superuser_db,
        owner_id=user.id,
        capability_key="task_execution",
        domain="runtime",
        status="unknown",
    )

    tool = _by_key(capability_truth.tool_capability_view(superuser_db, user=user))["task_execution"]

    assert tool.verified is None
    assert tool.implemented is True
