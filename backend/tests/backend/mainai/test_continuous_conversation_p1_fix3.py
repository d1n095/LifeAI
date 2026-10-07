"""Adversarial P1-fix-3 coverage for continuous conversation authority and memory."""

from __future__ import annotations

import ast
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.mainai_continuous_conversation.classify import classify_inbound
from app.mainai_continuous_conversation.context import (
    PROMPT_COMPACTION_CHAR_BUDGET,
    assemble_live_context,
    retrieve_original_turn,
)
from app.mainai_continuous_conversation.discover import discover_software_truth
from app.mainai_continuous_conversation.entities import bind_subject_from_db
from app.mainai_continuous_conversation.orchestrate import handle_founder_message
from app.mainai_continuous_conversation.outbound import filter_outbound
from app.mainai_continuous_conversation.provider import ObservedRepositoryState, PrefetchedStateProvider
from app.mainai_continuous_conversation.service import get_or_create_canonical_conversation
from app.mainai_continuous_conversation.types import InboundKind, InterruptKind, OutboundDisposition, WorkspaceMutability
from app.mainai_continuous_conversation.workspace import WorkspaceClaim, claim_workspace, workspace_for_agent
from app.models.continuous_conversation import (
    FounderConversationDecision,
    FounderConversationProvenance,
    GovernedArtifactCertification,
)
from app.models.conversation import Message, MessageRole, MessageStatus
from app.models.user import User

PACKAGE_DIR = Path(__file__).resolve().parents[3] / "app" / "mainai_continuous_conversation"
FORBIDDEN_CURRENT_SHAS = {
    "2fbe20aacf1203fc0e16d216ef55b666b2181619",
    "691490edd82fa4bff6188f4f038f7579ee4f3df5",
    "ffbdb6328b8ff594caf2eea71c77c32ef96e516b",
    "e21ad76bce0ed18c7f69f31939de5b57c84c046f",
    "80ecfc73270fd10b7409dd3c36af9c8a6fbd07ff",
}


def _owner(db) -> User:
    user = User(email=f"cc-fix3-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def _message(db, conversation_id, content, *, index=0, role=MessageRole.user) -> Message:
    message = Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
        status=MessageStatus.succeeded,
        created_at=datetime.utcnow() + timedelta(seconds=index),
    )
    db.add(message)
    db.flush()
    return message


def test_package_has_no_hard_coded_current_sha():
    hits: dict[str, set[str]] = {}
    for path in PACKAGE_DIR.glob("*.py"):
        text_body = path.read_text(encoding="utf-8").lower()
        found = {sha for sha in FORBIDDEN_CURRENT_SHAS if sha in text_body}
        if found:
            hits[path.name] = found
    assert hits == {}


def test_package_does_not_call_blocking_git():
    for path in PACKAGE_DIR.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "ls-remote" not in source
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name != "subprocess" for alias in node.names), path.name
            if isinstance(node, ast.ImportFrom):
                assert node.module != "subprocess", path.name


def test_refreeze_changes_resolved_answer_without_code_change(superuser_db):
    first = bind_subject_from_db(superuser_db, "What is the frozen Founder Alpha SHA?")
    assert first is not None and first.sha
    original = first.sha
    new_sha = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    row = superuser_db.get(GovernedArtifactCertification, "founder_alpha_frozen")
    assert row is not None
    row.sha = new_sha
    superuser_db.flush()
    second = bind_subject_from_db(superuser_db, "What is the frozen Founder Alpha SHA?")
    assert second is not None
    assert second.sha == new_sha
    assert original != new_sha


def test_github_current_tip_changes_resolved_answer(superuser_db):
    first_obs = ObservedRepositoryState(
        repository="d1n095/LifeAI",
        branch="cursor/mainai-continuous-conversation-p1-fix",
        sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        source="github_ref",
        observed_at=datetime.now(timezone.utc),
    )
    first = bind_subject_from_db(
        superuser_db, "What is this P1 fix branch currently at?", observed=first_obs
    )
    second_obs = ObservedRepositoryState(
        repository="d1n095/LifeAI",
        branch="cursor/mainai-continuous-conversation-p1-fix",
        sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        source="github_ref",
        observed_at=datetime.now(timezone.utc),
    )
    second = bind_subject_from_db(
        superuser_db, "What is this P1 fix branch currently at?", observed=second_obs
    )
    assert first is not None and second is not None
    assert first.sha == first_obs.sha
    assert second.sha == second_obs.sha
    assert first.sha != second.sha


def test_discover_does_not_require_local_git_checkout(superuser_db):
    provider = PrefetchedStateProvider(
        {
            ("d1n095/LifeAI", "cursor/mainai-continuous-conversation-p1-fix"): ObservedRepositoryState(
                repository="d1n095/LifeAI",
                branch="cursor/mainai-continuous-conversation-p1-fix",
                sha="ffffffffffffffffffffffffffffffffffffffff",
                source="github_ref",
                observed_at=datetime.now(timezone.utc),
            )
        }
    )
    truth = asyncio.run(
        discover_software_truth(
            "What is this P1 fix branch currently at?",
            db=superuser_db,
            provider=provider,
        )
    )
    assert truth.sha == "ffffffffffffffffffffffffffffffffffffffff"
    assert truth.source == "github_ref"


def test_erasure_helper_outside_governed_erasure_denied(superuser_db):
    owner = _owner(superuser_db)
    get_or_create_canonical_conversation(superuser_db, owner_id=owner.id)
    superuser_db.flush()
    superuser_db.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner.id)})
    with pytest.raises(Exception, match="governed account-erasure"):
        superuser_db.execute(text("SELECT erase_own_continuous_conversation_children()"))


def test_forged_erasure_guc_denied(superuser_db):
    owner = _owner(superuser_db)
    conversation = get_or_create_canonical_conversation(superuser_db, owner_id=owner.id)
    superuser_db.flush()
    superuser_db.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner.id)})
    superuser_db.execute(text("SET LOCAL app.continuous_conversation_erasure_in_progress = 'true'"))
    superuser_db.execute(text("SET LOCAL app.account_erasure_operation_id = :oid"), {"oid": str(uuid.uuid4())})
    with pytest.raises(Exception, match="governed account-erasure"):
        superuser_db.execute(text("SELECT erase_own_continuous_conversation_children()"))
    with pytest.raises(Exception, match="governed"):
        superuser_db.delete(conversation)
        superuser_db.flush()


def test_cross_owner_provenance_message_rejected(superuser_db):
    owner_a = _owner(superuser_db)
    owner_b = _owner(superuser_db)
    conversation_a = get_or_create_canonical_conversation(superuser_db, owner_id=owner_a.id)
    conversation_b = get_or_create_canonical_conversation(superuser_db, owner_id=owner_b.id)
    message_a = _message(superuser_db, conversation_a.id, "owner A secret sha aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    superuser_db.add(
        FounderConversationProvenance(
            owner_id=owner_b.id,
            conversation_id=conversation_b.id,
            message_id=message_a.id,
            kind="sha",
            value="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
    )
    with pytest.raises(IntegrityError):
        superuser_db.flush()
    superuser_db.rollback()


def test_cross_owner_decision_message_rejected(superuser_db):
    owner_a = _owner(superuser_db)
    owner_b = _owner(superuser_db)
    conversation_a = get_or_create_canonical_conversation(superuser_db, owner_id=owner_a.id)
    conversation_b = get_or_create_canonical_conversation(superuser_db, owner_id=owner_b.id)
    message_a = _message(superuser_db, conversation_a.id, "Use provider A")
    superuser_db.add(
        FounderConversationDecision(
            owner_id=owner_b.id,
            conversation_id=conversation_b.id,
            message_id=message_a.id,
            topic="provider",
            decision_key="provider",
            statement="Use provider A",
            value="A",
        )
    )
    with pytest.raises(IntegrityError):
        superuser_db.flush()
    superuser_db.rollback()


def test_provider_a_b_c_swedish_d_resolves_d_and_keeps_history(superuser_db):
    owner = _owner(superuser_db)
    conversation = get_or_create_canonical_conversation(superuser_db, owner_id=owner.id)
    phrases = (
        "Use provider A",
        "Switch to provider B",
        "Let's go with provider C instead.",
        "Byt leverantör till D.",
    )
    for index, phrase in enumerate(phrases):
        _message(superuser_db, conversation.id, phrase, index=index)
    ctx = assemble_live_context(superuser_db, owner_id=owner.id, conversation_id=conversation.id)
    current = [item for item in ctx.active_decisions if item.decision_key == "provider"]
    assert len(current) == 1
    assert current[0].value == "D"
    rows = superuser_db.query(FounderConversationDecision).filter_by(owner_id=owner.id, conversation_id=conversation.id).all()
    historic_values = {row.value for row in rows if row.superseded or row.status == "historical"}
    assert historic_values == {"A", "B", "C"}
    assert any(row.value == "D" and not row.superseded for row in rows)


def test_stale_compacted_summary_cannot_override_d(superuser_db):
    owner = _owner(superuser_db)
    conversation = get_or_create_canonical_conversation(superuser_db, owner_id=owner.id)
    for index in range(30):
        if index == 0:
            content = "Use provider A"
        elif index == 1:
            content = "Switch to provider B"
        elif index == 2:
            content = "Let's go with provider C instead."
        elif index == 29:
            content = "Byt leverantör till D."
        else:
            content = f"filler turn {index} about provider B still being mentioned in chatter"
        _message(superuser_db, conversation.id, content, index=index)
    ctx = assemble_live_context(superuser_db, owner_id=owner.id, conversation_id=conversation.id)
    assert ctx.compacted
    assert any("provider B" in (item.summary_text or "") or "provider A" in (item.summary_text or "") for item in ctx.compacted)
    current = [item for item in ctx.active_decisions if item.decision_key == "provider"]
    assert current and current[0].value == "D"
    blocks = ctx.prompt_blocks()
    assert "D" in blocks
    assert "ACTIVE DECISIONS" in blocks
    assert "summaries cannot override" in blocks


def test_natural_english_and_swedish_relay_variants_are_looked_up():
    phrases = (
        "Can you drop the latest commit hash in chat?",
        "Kan du skicka testresultaten?",
        "Vad rapporterade Codex?",
        "Vad kom Claude fram till?",
        "Kan du kolla om CI gick igenom?",
        "Har Cursor pushat senaste ändringen?",
        "Could you share the commit id?",
        "What did Claude report?",
    )
    for text_value in phrases:
        inbound = classify_inbound(text_value)
        assert inbound.kind is InboundKind.RELAY_REQUEST, text_value
        result = handle_founder_message(text_value)
        assert result.interrupt_founder is False
        assert any(
            action.kind.value in {"discover_from_github", "lookup_unknown", "coordinate_internally"}
            for action in result.internal_actions
        )


def test_legitimate_github_ci_educational_replies_preserved():
    for text_value in (
        "You can configure GitHub Actions to run pytest on every push.",
        "Your CI status page lists every workflow run.",
        "I will mention GitHub, CI, commit, branch, and test in an ordinary sentence.",
    ):
        decision = filter_outbound(text_value)
        assert decision.disposition is OutboundDisposition.SEND, text_value
        assert decision.content == text_value


def test_thousand_turns_keep_prompt_bounded_and_incremental(superuser_db):
    owner = _owner(superuser_db)
    conversation = get_or_create_canonical_conversation(superuser_db, owner_id=owner.id)
    for index in range(1000):
        content = f"turn {index} filler"
        if index == 0:
            content = "Use provider A"
        elif index == 999:
            content = "Byt leverantör till D."
        _message(superuser_db, conversation.id, content, index=index)
    first_id = (
        superuser_db.query(Message)
        .filter_by(conversation_id=conversation.id)
        .order_by(Message.created_at.asc(), Message.id.asc())
        .first()
        .id
    )
    first = assemble_live_context(superuser_db, owner_id=owner.id, conversation_id=conversation.id)
    assert first.original_count >= 1000
    assert len(first.prompt_blocks()) <= PROMPT_COMPACTION_CHAR_BUDGET + 800
    assert first.messages_scanned == 1000
    original = retrieve_original_turn(superuser_db, owner_id=owner.id, message_id=first_id)
    assert original is not None
    assert "Use provider A" in original.content
    _message(superuser_db, conversation.id, "follow-up after 1000", index=1001)
    second = assemble_live_context(superuser_db, owner_id=owner.id, conversation_id=conversation.id)
    assert second.messages_scanned < 50
    assert second.messages_scanned != first.original_count
    current = [item for item in second.active_decisions if item.decision_key == "provider"]
    assert current and current[0].value == "D"


def test_dynamic_workspace_ownership_from_coordination_state(superuser_db):
    owner = _owner(superuser_db)
    builder_path = f"/tmp/dynamic-builder-{uuid.uuid4()}"
    examiner_path = f"/tmp/dynamic-examiner-{uuid.uuid4()}"
    builder = claim_workspace(
        superuser_db,
        WorkspaceClaim(
            owner_id=owner.id,
            agent_key="cursor",
            branch="cursor/dynamic-builder",
            worktree_path=builder_path,
            mutability=WorkspaceMutability.MUTABLE_BUILDER,
            shared_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ),
    )
    examiner = claim_workspace(
        superuser_db,
        WorkspaceClaim(
            owner_id=owner.id,
            agent_key="claude",
            branch="cursor/dynamic-examiner",
            worktree_path=examiner_path,
            mutability=WorkspaceMutability.READ_ONLY_EXAMINER,
            shared_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ),
    )
    assert workspace_for_agent(superuser_db, owner_id=owner.id, agent_key="cursor").worktree_path == builder_path
    assert workspace_for_agent(superuser_db, owner_id=owner.id, agent_key="claude").worktree_path == examiner_path
    assert builder.worktree_path != examiner.worktree_path


def test_database_deletion_and_payment_requests_are_gated():
    destroy = handle_founder_message("Ta bort databasen.")
    pay = handle_founder_message("Betala fakturan på 4000 kr.")
    assert destroy.inbound.interrupt is InterruptKind.DESTRUCTIVE_ACTION
    assert destroy.gated is True
    assert pay.inbound.interrupt is InterruptKind.MONEY_BUDGET
    assert pay.gated is True


def test_existing_swedish_founder_only_cases_remain_gated():
    cases = {
        "Radera grenen codex/x": InterruptKind.DESTRUCTIVE_ACTION,
        "Ska vi lansera nu?": InterruptKind.MATERIAL_PRODUCT_DECISION,
        "Ändra säkerhetspolicyn": InterruptKind.SECURITY_POLICY,
        "Godkänn köpet": InterruptKind.MONEY_BUDGET,
    }
    for text_value, kind in cases.items():
        result = handle_founder_message(text_value)
        assert result.inbound.interrupt is kind
        assert result.gated is True
        assert any(action.kind.value == "refuse_authority" for action in result.internal_actions)
