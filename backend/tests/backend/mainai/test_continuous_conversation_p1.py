"""P1 continuous-conversation regressions: subject binding, erasure, context, policy."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.mainai_continuous_conversation.capability import IMPLEMENTED, assert_not_claiming_unimplemented, capability_disclaimer
from app.mainai_continuous_conversation.classify import classify_inbound
from app.mainai_continuous_conversation.context import (
    assemble_live_context,
    retrieve_original_turn,
)
from app.mainai_continuous_conversation.entities import (
    CONTINUOUS_CONVERSATION_PARENT_SHA,
    FROZEN_FOUNDER_ALPHA_SHA,
    bind_subject,
    read_remote_branch_sha,
)
from app.mainai_continuous_conversation.occupancy import occupancy_snapshot
from app.mainai_continuous_conversation.orchestrate import handle_founder_message
from app.mainai_continuous_conversation.outbound import filter_outbound
from app.mainai_continuous_conversation.service import get_or_create_canonical_conversation, persist_turn, record_event
from app.mainai_continuous_conversation.types import (
    InboundKind,
    InterruptKind,
    OccupancyObservation,
    OccupancySnapshot,
    OccupancyState,
    OutboundDisposition,
    SoftwareTruth,
    WorkspaceMutability,
)
from app.mainai_continuous_conversation.workspace import (
    CLAUDE_EXAMINER_WORKTREE,
    CURSOR_BUILDER_WORKTREE,
    WorkspaceClaim,
    WorkspaceOwnershipError,
    claim_workspace,
    sha_sharing_allowed,
    workspaces_are_isolated,
)
from app.models.continuous_conversation import (
    FounderCanonicalConversation,
    FounderConversationCompaction,
    FounderConversationDecision,
    FounderConversationEvent,
    FounderConversationProvenance,
)
from app.models.conversation import Conversation, Message, MessageRole, MessageStatus
from app.models.user import User
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.config import get_settings
from tests.backend.test_account_erasure import erase_account_data

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
DIM = get_settings().embedding_dim
OLD_MARKER_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
P1_BRANCH = "cursor/mainai-continuous-conversation-p1-fix"


def _owner(db):
    user = User(email=f"cc-p1-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def test_subject_binding_does_not_conflate_adjacent_entities():
    founder_alpha = bind_subject("What is the frozen Founder Alpha SHA?")
    parent = bind_subject("What SHA is the Continuous Conversation parent?")
    current = bind_subject("What is this P1 fix branch currently at?")
    sovereignty = bind_subject("What is the Founder Sovereignty SHA?")
    assert founder_alpha is not None and founder_alpha.sha == FROZEN_FOUNDER_ALPHA_SHA
    assert parent is not None and parent.sha == CONTINUOUS_CONVERSATION_PARENT_SHA
    assert sovereignty is not None and sovereignty.sha == "ffbdb6328b8ff594caf2eea71c77c32ef96e516b"
    assert current is not None
    assert current.branch == P1_BRANCH
    assert current.sha != FROZEN_FOUNDER_ALPHA_SHA
    assert current.sha != CONTINUOUS_CONVERSATION_PARENT_SHA
    assert {founder_alpha.entity_key, parent.entity_key, current.entity_key, sovereignty.entity_key} == {
        "founder_alpha_frozen",
        "continuous_conversation_parent",
        "continuous_conversation_p1",
        "founder_sovereignty",
    }


def test_current_p1_tip_uses_remote_ref_not_checkout():
    bound = bind_subject("What is this P1 fix branch currently at?")
    remote = read_remote_branch_sha(P1_BRANCH)
    assert bound is not None
    assert remote is not None
    assert bound.sha == remote
    assert bound.authoritative_source == "remote_branch_tip"


def test_swedish_and_english_relay_requests_are_looked_up_or_unknown():
    for text in (
        "Kan du klistra in SHA:n?",
        "Är CI grönt?",
        "Vad sa Cursor?",
        "Could you share the commit id?",
        "What did Claude report?",
    ):
        inbound = classify_inbound(text)
        assert inbound.kind is InboundKind.RELAY_REQUEST
        result = handle_founder_message(text)
        assert result.interrupt_founder is False
        assert any(action.kind.value in {"discover_from_github", "lookup_unknown", "coordinate_internally"} for action in result.internal_actions)


def test_legitimate_replies_survive_words_like_branch_github_test_ci_commit():
    for text in (
        "I will read GitHub and keep coordinating Cursor on a separate lane.",
        "The test plan covers CI later this week.",
        "This commit message mentions the branch name only as documentation.",
        "CI is a word in this ordinary status sentence.",
    ):
        decision = filter_outbound(text)
        assert decision.disposition is OutboundDisposition.SEND
        assert decision.content == text
    assert_not_claiming_unimplemented(capability_disclaimer())
    with pytest.raises(AssertionError):
        assert_not_claiming_unimplemented("MainAI handles machine coordination internally")


def test_swedish_founder_interrupts_gate_behavior():
    cases = {
        "Radera grenen codex/x": InterruptKind.DESTRUCTIVE_ACTION,
        "Ska vi lansera nu?": InterruptKind.MATERIAL_PRODUCT_DECISION,
        "Ändra säkerhetspolicyn": InterruptKind.SECURITY_POLICY,
        "Godkänn köpet": InterruptKind.MONEY_BUDGET,
    }
    for text, kind in cases.items():
        result = handle_founder_message(text, busy_agents=("cursor",), idle_agents=("codex",))
        assert result.inbound.interrupt is kind
        assert result.interrupt_founder is True
        assert result.gated is True
        assert result.founder_message
        assert not any(action.kind.value == "assign_idle_agent" for action in result.internal_actions)
        assert any(action.kind.value == "refuse_authority" for action in result.internal_actions)


def test_capability_surface_does_not_claim_assignment_execution():
    assert IMPLEMENTED["machine_assignment_execution"] is False
    assert "not wired" in capability_disclaimer()


def test_cross_owner_binding_and_events_fail(superuser_db):
    owner_a = _owner(superuser_db)
    owner_b = _owner(superuser_db)
    conversation_a = get_or_create_canonical_conversation(superuser_db, owner_id=owner_a.id)
    superuser_db.flush()
    superuser_db.add(
        FounderCanonicalConversation(owner_id=owner_b.id, conversation_id=conversation_a.id)
    )
    with pytest.raises(IntegrityError):
        superuser_db.flush()
    superuser_db.rollback()
    owner_a = _owner(superuser_db)
    owner_b = _owner(superuser_db)
    conversation_a = get_or_create_canonical_conversation(superuser_db, owner_id=owner_a.id)
    with pytest.raises(Exception):
        record_event(
            superuser_db,
            owner_id=owner_b.id,
            conversation_id=conversation_a.id,
            direction="inbound",
            kind="ordinary_chat",
            excerpt="cross-owner",
        )
        superuser_db.flush()
    superuser_db.rollback()


def test_canonical_conversation_delete_is_governed(superuser_db):
    owner = _owner(superuser_db)
    conversation = get_or_create_canonical_conversation(superuser_db, owner_id=owner.id)
    superuser_db.flush()
    with pytest.raises(Exception):
        superuser_db.delete(conversation)
        superuser_db.flush()
    superuser_db.rollback()


def test_erase_account_data_succeeds_with_canonical_conversation():
    from tests.backend.test_account_erasure import _make_user

    session = SessionLocal()
    try:
        owner = _make_user(session)
        conversation = get_or_create_canonical_conversation(session, owner_id=owner.id)
        persist_turn(session, owner_id=owner.id, text="hello lifelong thread")
        session.commit()
        owner_id = owner.id
        conversation_id = conversation.id
        assert session.get(FounderCanonicalConversation, owner_id) is not None
        erase_account_data(session, owner)
        assert session.get(User, owner_id) is None
        assert session.get(Conversation, conversation_id) is None
        assert session.get(FounderCanonicalConversation, owner_id) is None
        assert session.query(FounderConversationEvent).filter_by(owner_id=owner_id).count() == 0
    finally:
        session.rollback()
        session.close()


def test_occupancy_snapshot_is_unknown_without_runtime_identity(superuser_db):
    owner = _owner(superuser_db)
    snapshot = occupancy_snapshot(superuser_db, owner_id=owner.id)
    assert snapshot.source in {"runtime_snapshot_empty", "runtime_snapshot", "unavailable"}
    if not snapshot.observations:
        assert snapshot.running_agents == ()
        assert snapshot.idle_agents == ()


def test_authoritative_occupancy_binds_assignment_identity():
    assignment_id = uuid.uuid4()
    task_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    snapshot = OccupancySnapshot(
        observations=(
            OccupancyObservation(
                agent_key="cursor",
                state=OccupancyState.RUNNING,
                assignment_id=assignment_id,
                task_id=task_id,
                execution_id=execution_id,
                source="runtime_snapshot",
                authoritative=True,
            ),
        ),
        source="runtime_snapshot",
        authoritative=True,
    )
    result = handle_founder_message("What is Cursor doing?", occupancy=snapshot)
    assert snapshot.running_agents == ("cursor",)
    held = [action for action in result.internal_actions if action.kind.value == "hold_busy_agent"]
    assert held and "assignment" in held[0].detail
    observed = [action for action in result.internal_actions if action.kind.value == "observe_occupancy"]
    assert observed and str(assignment_id) in observed[0].detail


def test_workspace_ownership_allows_sha_share_forbids_workspace_share(superuser_db):
    assert workspaces_are_isolated(CURSOR_BUILDER_WORKTREE, CLAUDE_EXAMINER_WORKTREE)
    owner = _owner(superuser_db)
    sha = FROZEN_FOUNDER_ALPHA_SHA
    builder = claim_workspace(
        superuser_db,
        WorkspaceClaim(
            owner_id=owner.id,
            agent_key="cursor",
            branch="cursor/mainai-continuous-conversation-p1-fix",
            worktree_path=f"{CURSOR_BUILDER_WORKTREE}-{owner.id}",
            mutability=WorkspaceMutability.MUTABLE_BUILDER,
            task_id=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            shared_sha=sha,
        ),
    )
    examiner = claim_workspace(
        superuser_db,
        WorkspaceClaim(
            owner_id=owner.id,
            agent_key="claude",
            branch="cursor/mainai-continuous-conversation-foundation",
            worktree_path=f"{CLAUDE_EXAMINER_WORKTREE}-{owner.id}",
            mutability=WorkspaceMutability.READ_ONLY_EXAMINER,
            shared_sha=sha,
        ),
    )
    assert sha_sharing_allowed(builder.shared_sha, examiner.shared_sha)
    with pytest.raises(WorkspaceOwnershipError):
        claim_workspace(
            superuser_db,
            WorkspaceClaim(
                owner_id=owner.id,
                agent_key="cursor",
                branch="cursor/mainai-continuous-conversation-foundation",
                worktree_path=f"/tmp/illegal-mutable-{owner.id}",
                mutability=WorkspaceMutability.MUTABLE_BUILDER,
                shared_sha=sha,
            ),
        )


def test_long_thread_keeps_latest_active_and_oldest_retrievable(superuser_db):
    owner = _owner(superuser_db)
    conversation = get_or_create_canonical_conversation(superuser_db, owner_id=owner.id)
    first_id = None
    for index in range(32):
        if index == 0:
            content = f"Remember this old SHA {OLD_MARKER_SHA} and use provider A"
        elif index == 24:
            content = "Switch to provider B decision_id 11111111-1111-1111-1111-111111111111"
        else:
            content = f"turn {index} latest window probe {index}"
        message = Message(
            conversation_id=conversation.id,
            role=MessageRole.user if index % 2 == 0 else MessageRole.assistant,
            content=content,
            status=MessageStatus.succeeded,
            created_at=datetime.utcnow() + timedelta(seconds=index),
        )
        superuser_db.add(message)
        superuser_db.flush()
        if index == 0:
            first_id = message.id
    ctx = assemble_live_context(superuser_db, owner_id=owner.id, conversation_id=conversation.id)
    superuser_db.flush()
    assert ctx.original_count >= 30
    assert len(ctx.active_messages) == 20
    assert all("latest window probe" in (item.content or "") or "provider B" in (item.content or "") for item in ctx.active_messages[-5:])
    assert OLD_MARKER_SHA not in " ".join(item.content or "" for item in ctx.active_messages)
    assert ctx.compacted
    original = retrieve_original_turn(superuser_db, owner_id=owner.id, message_id=first_id)
    assert original is not None
    assert OLD_MARKER_SHA in original.content
    assert superuser_db.get(Message, first_id) is not None
    assert any(OLD_MARKER_SHA in (item.summary_text or "") or any(pointer.get("value") == OLD_MARKER_SHA for pointer in (item.source_provenance or [])) for item in ctx.compacted)
    assert any(pointer.value == OLD_MARKER_SHA and pointer.kind == "sha" for pointer in ctx.provenance)
    decisions = superuser_db.query(FounderConversationDecision).filter_by(owner_id=owner.id, conversation_id=conversation.id).all()
    assert len(decisions) >= 2
    current = [row for row in decisions if not row.superseded]
    historic = [row for row in decisions if row.superseded]
    assert any("provider B" in row.statement for row in current)
    assert any("provider A" in row.statement for row in historic)
    assert all(row.superseded_by is not None for row in historic)
    bindings = superuser_db.query(FounderCanonicalConversation).filter_by(owner_id=owner.id).all()
    assert len(bindings) == 1
    assert bindings[0].conversation_id == conversation.id


def test_hello_world_three_sha_questions_are_not_conflated(client, superuser_db, monkeypatch):
    async def _embed(self, texts, model, **kwargs):
        return [[0.1] * DIM for _ in texts]

    async def _chat(self, messages, model, **kwargs):
        return ChatResult(
            content="I will mention GitHub, CI, commit, branch, and test in an ordinary sentence.",
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 5, "completion_tokens": 3},
        )

    monkeypatch.setattr(OpenAIProvider, "embed", _embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _chat)
    csrf = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD}).json()["csrf_token"]
    first = client.post(
        "/api/chat",
        json={"message": "What is the frozen Founder Alpha SHA?", "continuous": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert first.status_code == 200, first.text
    assert FROZEN_FOUNDER_ALPHA_SHA in first.json()["reply"]
    assert CONTINUOUS_CONVERSATION_PARENT_SHA not in first.json()["reply"]
    conversation_id = first.json()["conversation_id"]
    second = client.post(
        "/api/chat",
        json={"message": "What SHA is the Continuous Conversation parent?", "continuous": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert second.status_code == 200, second.text
    assert second.json()["conversation_id"] == conversation_id
    assert CONTINUOUS_CONVERSATION_PARENT_SHA in second.json()["reply"]
    assert FROZEN_FOUNDER_ALPHA_SHA not in second.json()["reply"]
    third = client.post(
        "/api/chat",
        json={"message": "What is this P1 fix branch currently at?", "continuous": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert third.status_code == 200, third.text
    assert third.json()["conversation_id"] == conversation_id
    remote = read_remote_branch_sha(P1_BRANCH)
    assert remote
    assert remote in third.json()["reply"]
    assert FROZEN_FOUNDER_ALPHA_SHA not in third.json()["reply"]
    assert CONTINUOUS_CONVERSATION_PARENT_SHA not in third.json()["reply"]
    swedish = client.post(
        "/api/chat",
        json={"message": "Radera grenen codex/x", "continuous": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert swedish.status_code == 200, swedish.text
    assert swedish.json()["conversation_id"] == conversation_id
    assert "utför den inte" in swedish.json()["reply"].lower() or "gated" in swedish.json()["reply"].lower() or "destruktiv" in swedish.json()["reply"].lower()
