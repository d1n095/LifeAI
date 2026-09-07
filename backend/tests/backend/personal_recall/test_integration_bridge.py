from __future__ import annotations

from dataclasses import replace
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from app.personal_recall.adapters import IterableAdapter
from app.personal_recall.authorization import DisclosureLevel, IdentifierScope, RecallAuthorizationContext, RecallAuthorizationError
from app.personal_recall.locator import SourceRegistryRecord
from app.personal_recall.service import PersonalRecallService, RecallContextHints, RecallMode, RecallOpenSourceRequest, RecallQueryRequest, RecallStatusRequest
from app.personal_recall.snapshot_protection import DeterministicTestSnapshotProtector, RecallSnapshotProtectionError
from app.personal_recall.types import DecisionState, IndexState, PersonalKnowledgeItem, Provenance, SourceAuthority, SourceType, VerificationState


# Keep the fixture grant comfortably in the future so tests do not expire as calendar time advances.
NOW = datetime(2035, 9, 5, tzinfo=timezone.utc)


def _context(**changes):
    base = RecallAuthorizationContext(
        authorization_id="grant-1", authorization_version="v1", owner_id="alice", session_user_id="alice",
        session_jti="session-1", session_issued_at=NOW - timedelta(minutes=5), expires_at=NOW + timedelta(days=2),
        allowed_source_types=frozenset({SourceType.CONVERSATION, SourceType.FILE}),
        project_scope=IdentifierScope(frozenset({"project-a"})), conversation_scope=IdentifierScope(frozenset({"conversation-a"})),
        allow_current=True, allow_historical=True, disclosure_level=DisclosureLevel.SNIPPET, allow_locator_open=True,
    )
    return replace(base, **changes)


def _item(*, owner="alice", project="project-a", conversation="conversation-a", item_id="item-1", index_state=IndexState.INDEXED):
    return PersonalKnowledgeItem(
        item_id=item_id, source_type=SourceType.CONVERSATION, source_id=item_id, owner_id=owner,
        content_reference=f"conversation:{item_id}", provenance=Provenance(SourceType.CONVERSATION, item_id, f"conversation://{conversation}/{item_id}"),
        text="tandkrämsrecept med nano HAp", conversation_id=conversation, project_id=project, subject="tandkrämsrecept",
        decision_state=DecisionState.DECISION, verification_state=VerificationState.VERIFIED,
        source_authority=SourceAuthority.USER, index_state=index_state,
    )


class MutableResolver:
    def __init__(self, current):
        self.current = current
        self.calls = 0
        self.on_call = None

    def resolve(self, presented, *, now):
        self.calls += 1
        if self.on_call:
            self.on_call(self.calls)
        return self.current


class MutableRegistry:
    available = True

    def resolve(self, *, source_id, owner_id, source_type, locator):
        if not self.available:
            return None
        return SourceRegistryRecord(source_id, owner_id, source_type, locator, True)


class IdentityRegistry(MutableRegistry):
    generation = 1
    identity = hashlib.sha256("tandkrämrecept med nano HAp".encode()).hexdigest()
    lifecycle = "active"

    def resolve(self, *, source_id, owner_id, source_type, locator):
        return SourceRegistryRecord(source_id, owner_id, source_type, locator, self.available, self.generation, self.identity, self.lifecycle)


def _service(resolver, registry, items=None):
    values = [_item()] if items is None else items
    return PersonalRecallService(
        authority_resolver=resolver,
        adapter_factory=lambda context: [IterableAdapter("test", values)],
        source_registry_factory=lambda context: registry,
        expected_source_types={SourceType.CONVERSATION},
    )


def test_facade_preserves_structured_truth_and_coverage_without_flattening():
    context, registry = _context(), MutableRegistry()
    result = _service(MutableResolver(context), registry).query(RecallQueryRequest("allt om tandkrämsrecept", context), now=NOW)
    evidence = result.handoff.evidence[0]
    assert evidence.source_authority == "user"
    assert evidence.verification_state == "verified"
    assert evidence.why_matched
    assert result.handoff.coverage.state.value == "complete"
    assert dict(result.handoff.coverage.source_states)[SourceType.CONVERSATION].value == "complete"
    assert result.authorization_receipt.owner_id == "alice"
    assert result.authorization_receipt.disclosure_evidence[0].result.value == "VALID"


def test_disclosure_rejects_generation_and_content_identity_changes():
    context, resolver, registry = _context(), MutableResolver(_context()), IdentityRegistry()
    item = _item()
    item.content_hash = registry.identity
    item.metadata["canonical_generation"] = 1
    service = _service(resolver, registry, [item])
    result = service.query(RecallQueryRequest("tandkrämsrecept", context), now=NOW)
    registry.generation = 2
    with pytest.raises(RecallAuthorizationError, match="STALE"):
        service.open_source(RecallOpenSourceRequest(item, context, result.authorization_receipt))
    registry.generation = 1
    registry.identity = "different-content"
    with pytest.raises(RecallAuthorizationError, match="CONTENT_MISMATCH"):
        service.open_source(RecallOpenSourceRequest(item, context, result.authorization_receipt))


@pytest.mark.parametrize("bad", [
    {"session_user_id": "bob"}, {"allowed_source_types": frozenset()}, {"disclosure_level": DisclosureLevel.NONE},
])
def test_query_contract_fails_closed_for_identity_or_disclosure_gaps(bad):
    context = _context(**bad)
    with pytest.raises(RecallAuthorizationError):
        _service(MutableResolver(context), MutableRegistry()).query(RecallQueryRequest("tandkrämsrecept", context), now=NOW)


def test_forged_project_and_conversation_hints_cannot_expand_authority():
    context = _context()
    service = _service(MutableResolver(context), MutableRegistry())
    for hints in (RecallContextHints(active_project_id="project-b"), RecallContextHints(current_conversation_id="conversation-b")):
        with pytest.raises(RecallAuthorizationError):
            service.query(RecallQueryRequest("tandkrämsrecept", context, hints), now=NOW)


def test_context_hints_only_narrow_results():
    context = replace(_context(), project_scope=IdentifierScope(unrestricted=True), conversation_scope=IdentifierScope(unrestricted=True))
    items = [_item(), _item(project="project-b", conversation="conversation-b", item_id="item-2")]
    result = _service(MutableResolver(context), MutableRegistry(), items).query(RecallQueryRequest("tandkrämsrecept", context, RecallContextHints(active_project_id="project-a")), now=NOW)
    assert [row.item.item_id for row in result.recall.results] == ["item-1"]


def test_authority_narrowing_during_query_blocks_disclosure():
    context, resolver = _context(), MutableResolver(_context())
    resolver.on_call = lambda calls: setattr(resolver, "current", replace(context, authorization_version="v2", allowed_source_types=frozenset())) if calls == 2 else None
    with pytest.raises(RecallAuthorizationError, match="changed during recall"):
        _service(resolver, MutableRegistry()).query(RecallQueryRequest("tandkrämsrecept", context), now=NOW)


def test_session_revocation_before_serialization_blocks_result():
    context, resolver = _context(), MutableResolver(_context())
    resolver.on_call = lambda calls: setattr(resolver, "current", None) if calls == 2 else None
    with pytest.raises(RecallAuthorizationError, match="revoked"):
        _service(resolver, MutableRegistry()).query(RecallQueryRequest("tandkrämsrecept", context), now=NOW)


def test_canonical_deletion_before_serialization_blocks_stale_snapshot_result():
    context, resolver, registry = _context(), MutableResolver(_context()), MutableRegistry()
    resolver.on_call = lambda calls: setattr(registry, "available", False) if calls == 2 else None
    with pytest.raises(RecallAuthorizationError, match="changed before disclosure"):
        _service(resolver, registry).query(RecallQueryRequest("tandkrämsrecept", context), now=NOW)


def test_source_deleted_between_query_and_open_is_revalidated():
    context, resolver, registry = _context(), MutableResolver(_context()), MutableRegistry()
    service = _service(resolver, registry)
    result = service.query(RecallQueryRequest("tandkrämsrecept", context), now=NOW)
    registry.available = False
    with pytest.raises(ValueError, match="missing or unavailable"):
        service.open_source(RecallOpenSourceRequest(result.recall.results[0].item, context, result.authorization_receipt))


def test_open_requires_fresh_permission_matching_receipt_and_returns_inert_bounded_snippet():
    context, resolver, registry = _context(), MutableResolver(_context()), MutableRegistry()
    service = _service(resolver, registry)
    result = service.query(RecallQueryRequest("tandkrämsrecept", context), now=NOW)
    opened = service.open_source(RecallOpenSourceRequest(result.recall.results[0].item, context, result.authorization_receipt, 8))
    assert opened.snippet == "tandkräm"
    resolver.current = replace(context, allow_locator_open=False)
    with pytest.raises(RecallAuthorizationError, match="permission"):
        service.open_source(RecallOpenSourceRequest(result.recall.results[0].item, context, result.authorization_receipt))


def test_status_contract_is_not_a_route_and_requires_fresh_authority():
    context, resolver = _context(), MutableResolver(_context())
    assert _service(resolver, MutableRegistry()).status(RecallStatusRequest(context))["routes_registered"] is False
    resolver.current = None
    with pytest.raises(RecallAuthorizationError):
        _service(resolver, MutableRegistry()).status(RecallStatusRequest(context))


def test_history_permission_filters_broad_mode_and_rejects_explicit_history():
    current = _item(item_id="current")
    old = _item(item_id="old")
    old.superseded_by = "current"
    context = replace(_context(), allow_historical=False)
    service = _service(MutableResolver(context), MutableRegistry(), [old, current])
    broad = service.query(RecallQueryRequest("tandkrämsrecept", context), now=NOW)
    assert "old" not in {r.item.item_id for r in broad.recall.results}
    assert "old" not in broad.recall.historical_items
    assert "old" not in {item_id for values in broad.recall.clusters.values() for item_id in values}
    with pytest.raises(RecallAuthorizationError, match="historical"):
        service.query(RecallQueryRequest("tandkrämsrecept", context, mode=RecallMode.HISTORICAL), now=NOW)


def test_disallowed_adapter_is_not_called_as_a_side_effect():
    context = replace(_context(), allowed_source_types=frozenset({SourceType.FILE}))
    called = []

    class ForbiddenConversationAdapter:
        name = "forbidden"
        source_types = frozenset({SourceType.CONVERSATION})

        def discover(self, *, owner_id):
            called.append(owner_id)
            return [_item()]

    service = PersonalRecallService(
        authority_resolver=MutableResolver(context), adapter_factory=lambda current: [ForbiddenConversationAdapter()],
        source_registry_factory=lambda current: MutableRegistry(), expected_source_types={SourceType.FILE},
    )
    result = service.query(RecallQueryRequest("tandkrämsrecept", context), now=NOW)
    assert result.recall.results == []
    assert called == []


def test_snapshot_protector_contract_detects_tamper_owner_and_context():
    protector = DeterministicTestSnapshotProtector()
    sealed = protector.seal(b"private recall", owner_id="alice", associated_data=b"snapshot-v2")
    assert protector.open(sealed, owner_id="alice", associated_data=b"snapshot-v2") == b"private recall"
    assert protector.key_reference(owner_id="alice").startswith("test-only://")
    with pytest.raises(RecallSnapshotProtectionError):
        protector.open(sealed, owner_id="bob", associated_data=b"snapshot-v2")
    with pytest.raises(RecallSnapshotProtectionError):
        protector.open(sealed[:-1] + b"A", owner_id="alice", associated_data=b"snapshot-v2")
