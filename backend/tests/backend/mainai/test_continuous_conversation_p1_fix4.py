"""Adversarial P1-fix-4 coverage for stale tips, paraphrase gates, relay, and decisions."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.mainai_continuous_conversation.classify import classify_inbound
from app.mainai_continuous_conversation.context import assemble_live_context, record_decision_from_text
from app.mainai_continuous_conversation.discover import discover_software_truth
from app.mainai_continuous_conversation.entities import bind_subject_from_db, record_observation
from app.mainai_continuous_conversation.orchestrate import handle_founder_message
from app.mainai_continuous_conversation.outbound import filter_outbound
from app.mainai_continuous_conversation.provider import ObservedRepositoryState, PrefetchedStateProvider
from app.mainai_continuous_conversation.service import get_or_create_canonical_conversation
from app.mainai_continuous_conversation.types import InboundKind, InterruptKind, OutboundDisposition
from app.models.continuous_conversation import FounderConversationDecision, GovernedRepositoryObservation
from app.models.conversation import Message, MessageRole, MessageStatus
from app.models.user import User

P1_BRANCH = "cursor/mainai-continuous-conversation-p1-fix"
STALE_SHA = "dddddddddddddddddddddddddddddddddddddddd"
LIVE_SHA = "cccccccccccccccccccccccccccccccccccccccc"


def _owner(db) -> User:
    user = User(email=f"cc-fix4-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def _message(db, conversation_id, content, *, index=0) -> Message:
    message = Message(
        conversation_id=conversation_id,
        role=MessageRole.user,
        content=content,
        status=MessageStatus.succeeded,
        created_at=datetime.utcnow() + timedelta(seconds=index),
    )
    db.add(message)
    db.flush()
    return message


def test_stale_github_observation_is_not_current_truth(superuser_db):
    stale = GovernedRepositoryObservation(
        repository="d1n095/LifeAI",
        branch=P1_BRANCH,
        sha=STALE_SHA,
        source="github_ref",
        observed_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    superuser_db.add(stale)
    superuser_db.flush()
    bound = bind_subject_from_db(superuser_db, "What is this P1 fix branch currently at?")
    assert bound is not None
    assert bound.sha is None
    assert bound.authoritative_source == "unavailable"
    truth = bound.as_software_truth()
    assert truth.founder_answer is not None
    assert "UNKNOWN" in truth.founder_answer
    assert STALE_SHA not in truth.founder_answer


def test_non_github_source_is_not_current_truth_even_if_fresh(superuser_db):
    checkout = ObservedRepositoryState(
        repository="d1n095/LifeAI",
        branch=P1_BRANCH,
        sha=STALE_SHA,
        source="checkout",
        observed_at=datetime.now(timezone.utc),
    )
    assert record_observation(superuser_db, checkout) is None
    bound = bind_subject_from_db(
        superuser_db, "What is this P1 fix branch currently at?", observed=checkout
    )
    assert bound is not None
    assert bound.sha is None


def test_failed_live_github_lookup_does_not_fall_back_to_stale_row(superuser_db):
    superuser_db.add(
        GovernedRepositoryObservation(
            repository="d1n095/LifeAI",
            branch=P1_BRANCH,
            sha=STALE_SHA,
            source="github_ref",
            observed_at=datetime.now(timezone.utc) - timedelta(hours=6),
        )
    )
    superuser_db.flush()
    provider = PrefetchedStateProvider({})
    import asyncio

    truth = asyncio.run(
        discover_software_truth(
            "What is this P1 fix branch currently at?",
            db=superuser_db,
            provider=provider,
        )
    )
    assert truth.sha is None
    assert STALE_SHA not in (truth.founder_answer or "")
    assert "UNKNOWN" in (truth.founder_answer or "")


def test_fresh_github_ref_still_binds_as_current_truth(superuser_db):
    live = ObservedRepositoryState(
        repository="d1n095/LifeAI",
        branch=P1_BRANCH,
        sha=LIVE_SHA,
        source="github_ref",
        observed_at=datetime.now(timezone.utc),
    )
    bound = bind_subject_from_db(
        superuser_db, "What is this P1 fix branch currently at?", observed=live
    )
    assert bound is not None
    assert bound.sha == LIVE_SHA
    assert bound.authoritative_source == "github_ref"


def test_paraphrase_gates_cover_recall_release_and_destructive_db():
    cases = {
        "Activate Recall in production right now.": InterruptKind.FOUNDER_ONLY_AUTHORITY,
        "Please turn on personal recall.": InterruptKind.FOUNDER_ONLY_AUTHORITY,
        "Aktivera minnet nu.": InterruptKind.FOUNDER_ONLY_AUTHORITY,
        "Release to production tonight.": InterruptKind.DESTRUCTIVE_ACTION,
        "Go live with this branch.": InterruptKind.DESTRUCTIVE_ACTION,
        "Publicera till produktion.": InterruptKind.DESTRUCTIVE_ACTION,
        "Truncate the production database.": InterruptKind.DESTRUCTIVE_ACTION,
        "Purge prod postgres.": InterruptKind.DESTRUCTIVE_ACTION,
        "Töm databasen.": InterruptKind.DESTRUCTIVE_ACTION,
    }
    for phrase, kind in cases.items():
        inbound = classify_inbound(phrase)
        result = handle_founder_message(phrase)
        assert inbound.kind is InboundKind.AUTHORITY_REQUEST, phrase
        assert inbound.interrupt is kind, phrase
        assert result.gated is True, phrase
        assert any(action.kind.value == "refuse_authority" for action in result.internal_actions), phrase


def test_educational_how_to_is_not_gated_as_authority():
    for phrase in (
        "How do I configure Recall in the documentation?",
        "Hur gör jag för att läsa CI-statussidan?",
        "How to run pytest locally for learning.",
    ):
        inbound = classify_inbound(phrase)
        assert inbound.interrupt is InterruptKind.NONE, phrase
        assert inbound.kind is not InboundKind.AUTHORITY_REQUEST, phrase


def test_natural_en_sv_relay_inbound_and_outbound_without_blocking_education():
    inbound_relays = (
        "What hash is on the remote?",
        "Vad är senaste committen på grenen?",
        "Did the tests pass?",
        "Kan du kolla om CI gick igenom?",
        "Please send the SHA to Claude.",
    )
    for phrase in inbound_relays:
        inbound = classify_inbound(phrase)
        assert inbound.kind is InboundKind.RELAY_REQUEST, phrase

    outbound_relays = (
        "Please send the SHA to Claude.",
        "Kan du klistra in SHA:n?",
        "Kan du berätta om CI gick igenom?",
    )
    for phrase in outbound_relays:
        decision = filter_outbound(phrase)
        assert decision.disposition is OutboundDisposition.REWRITE, phrase

    educational = (
        "You can configure GitHub Actions to run pytest on every push.",
        "The documentation explains how to check if CI is green on the status page.",
        "Dokumentationen förklarar hur du ser CI-status. Till exempel: statussidan listar körningar.",
        "I will mention GitHub, CI, commit, branch, and test in an ordinary sentence.",
    )
    for phrase in educational:
        decision = filter_outbound(phrase)
        assert decision.disposition is OutboundDisposition.SEND, phrase
        assert decision.content == phrase


def test_database_rejects_two_active_decisions_for_same_key(superuser_db):
    owner = _owner(superuser_db)
    conversation = get_or_create_canonical_conversation(superuser_db, owner_id=owner.id)
    first = _message(superuser_db, conversation.id, "Use provider A", index=0)
    second = _message(superuser_db, conversation.id, "Use provider B", index=1)
    superuser_db.add(
        FounderConversationDecision(
            owner_id=owner.id,
            conversation_id=conversation.id,
            message_id=first.id,
            topic="provider",
            decision_key="provider",
            statement="Use provider A",
            value="A",
            status="active",
        )
    )
    superuser_db.flush()
    superuser_db.add(
        FounderConversationDecision(
            owner_id=owner.id,
            conversation_id=conversation.id,
            message_id=second.id,
            topic="provider",
            decision_key="provider",
            statement="Use provider B",
            value="B",
            status="active",
        )
    )
    with pytest.raises(IntegrityError):
        superuser_db.flush()
    superuser_db.rollback()


def test_record_decision_function_serializes_concurrent_active_writes(superuser_db):
    owner = _owner(superuser_db)
    conversation = get_or_create_canonical_conversation(superuser_db, owner_id=owner.id)
    first = _message(superuser_db, conversation.id, "Use provider A", index=0)
    second = _message(superuser_db, conversation.id, "Switch to provider B", index=1)
    superuser_db.commit()

    errors: list[BaseException] = []
    settings = get_settings()
    engine = create_engine(settings.database_url)
    Session = sessionmaker(bind=engine)

    def _write(message_id: uuid.UUID, value: str) -> None:
        session = Session()
        try:
            session.execute(
                text(
                    "SELECT record_founder_conversation_decision("
                    ":owner_id, :conversation_id, :message_id, 'provider', "
                    ":statement, :value, '{}'::jsonb, now())"
                ),
                {
                    "owner_id": owner.id,
                    "conversation_id": conversation.id,
                    "message_id": message_id,
                    "statement": f"Use provider {value}",
                    "value": value,
                },
            )
            session.commit()
        except BaseException as exc:  # keep both writers' outcomes for the assertion
            errors.append(exc)
            session.rollback()
        finally:
            session.close()

    threads = [
        threading.Thread(target=_write, args=(first.id, "A")),
        threading.Thread(target=_write, args=(second.id, "B")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    engine.dispose()

    assert errors == []
    superuser_db.expire_all()
    rows = (
        superuser_db.query(FounderConversationDecision)
        .filter_by(owner_id=owner.id, conversation_id=conversation.id, decision_key="provider")
        .all()
    )
    active = [row for row in rows if row.status == "active"]
    assert len(active) == 1
    assert {row.value for row in rows} <= {"A", "B"}
    assert len({row.value for row in rows}) == len(rows)


def test_python_path_keeps_one_active_provider_decision(superuser_db):
    owner = _owner(superuser_db)
    conversation = get_or_create_canonical_conversation(superuser_db, owner_id=owner.id)
    first = _message(superuser_db, conversation.id, "Use provider A", index=0)
    second = _message(superuser_db, conversation.id, "Byt leverantör till D.", index=1)
    record_decision_from_text(
        superuser_db, owner_id=owner.id, conversation_id=conversation.id, message=first
    )
    record_decision_from_text(
        superuser_db, owner_id=owner.id, conversation_id=conversation.id, message=second
    )
    ctx = assemble_live_context(superuser_db, owner_id=owner.id, conversation_id=conversation.id)
    current = [item for item in ctx.active_decisions if item.decision_key == "provider"]
    assert len(current) == 1
    assert current[0].value == "D"
