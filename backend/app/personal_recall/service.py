"""Isolated MainAI bridge. This module deliberately registers no HTTP routes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

from app.personal_recall.adapters import PersonalSourceAdapter
from app.personal_recall.authorization import AuthorizationReceipt, DisclosureCheckResult, DisclosureEvidence, DisclosureLevel, RecallAuthorizationContext, RecallAuthorizationError, RecallAuthorityResolver, require_fresh_authority, require_item_authority
from app.personal_recall.locator import SourceRegistry, ValidatedLocator, validate_open_locator
from app.personal_recall.retrieval import PersonalRecallEngine
from app.personal_recall.types import CoverageReport, PersonalKnowledgeItem, RecallResponse, RetrievalResult, SourceType


class RecallMode(str, Enum):
    BROAD = "broad"
    CURRENT = "current"
    HISTORICAL = "historical"


@dataclass(frozen=True)
class RecallContextHints:
    current_conversation_id: str | None = None
    active_project_id: str | None = None
    active_intent_id: str | None = None
    workspace_file_id: str | None = None


@dataclass(frozen=True, repr=False)
class RecallEvidenceHandoff:
    item_id: str
    source_type: SourceType
    source_id: str
    source_authority: str
    verification_state: str
    decision_state: str
    index_state: str
    currentness: float | None
    historical: bool
    contradictions: tuple[str, ...]
    why_matched: tuple[str, ...]
    provenance_locator: str
    content_reference: str
    snippet: str | None


@dataclass(frozen=True, repr=False)
class MainAIRecallHandoff:
    evidence: tuple[RecallEvidenceHandoff, ...]
    coverage: CoverageReport
    warnings: tuple[str, ...]
    contradictions: tuple[tuple[str, str], ...]
    unresolved: tuple[str, ...]


@dataclass(frozen=True, repr=False)
class RecallServiceResponse:
    recall: RecallResponse
    handoff: MainAIRecallHandoff
    authorization_receipt: AuthorizationReceipt


@dataclass(frozen=True, repr=False)
class RecallQueryRequest:
    raw_query: str
    authorization: RecallAuthorizationContext
    hints: RecallContextHints = RecallContextHints()
    mode: RecallMode = RecallMode.BROAD


@dataclass(frozen=True, repr=False)
class RecallOpenSourceRequest:
    item: PersonalKnowledgeItem
    authorization: RecallAuthorizationContext
    receipt: AuthorizationReceipt
    max_snippet_chars: int = 2_000


@dataclass(frozen=True)
class RecallStatusRequest:
    authorization: RecallAuthorizationContext


@dataclass(frozen=True, repr=False)
class InertSourceReference:
    item_id: str
    source_type: SourceType
    source_id: str
    locator: ValidatedLocator
    snippet: str | None
    disclosure_level: DisclosureLevel


class AdapterFactory(Protocol):
    def __call__(self, context: RecallAuthorizationContext) -> list[PersonalSourceAdapter]: ...


class PersonalRecallService:
    MAX_QUERY_CHARS = 4_000
    MAX_OPEN_SNIPPET_CHARS = 8_000

    def __init__(self, *, authority_resolver: RecallAuthorityResolver, adapter_factory: AdapterFactory, source_registry_factory, expected_source_types=()):
        self.authority_resolver = authority_resolver
        self.adapter_factory = adapter_factory
        self.source_registry_factory = source_registry_factory
        self.expected_source_types = frozenset(expected_source_types)

    def query(self, request: RecallQueryRequest, *, now: datetime | None = None) -> RecallServiceResponse:
        checked_at = now or datetime.now(timezone.utc)
        start = require_fresh_authority(request.authorization, resolver=self.authority_resolver, now=checked_at)
        authorize_query_contract(start, request)
        adapters = [_AuthorizedAdapter(adapter, start, request.hints) for adapter in self.adapter_factory(start)]
        response = PersonalRecallEngine(adapters, expected_source_types=self.expected_source_types & start.allowed_source_types).recall(
            owner_id=start.owner_id,
            raw_query=request.raw_query,
            now=checked_at,
            project_id=request.hints.active_project_id,
        )
        if request.mode == RecallMode.CURRENT:
            response.results = [r for r in response.results if r.item.item_id in response.current_items]
        elif request.mode == RecallMode.HISTORICAL:
            if not start.allow_historical:
                raise RecallAuthorizationError("historical recall is not authorized")
            response.results = [r for r in response.results if r.item.item_id in response.historical_items]
        else:
            if not start.allow_historical:
                response.results = [r for r in response.results if r.item.item_id not in response.historical_items]
            if not start.allow_current:
                response.results = [r for r in response.results if r.item.item_id not in response.current_items]
        _prune_response_metadata(response)
        # The resolver is deliberately called again after adapters/DB work. A revoke or
        # narrowing during retrieval must govern disclosure, not the grant seen at start.
        end = require_fresh_authority(request.authorization, resolver=self.authority_resolver, now=now)
        _require_same_or_narrower_result_authority(start, end, response.results)
        registry: SourceRegistry = self.source_registry_factory(end)
        disclosure_evidence = tuple(_check_canonical_disclosure(result.item, owner_id=end.owner_id, registry=registry, checked_at=checked_at) for result in response.results)
        final = require_fresh_authority(request.authorization, resolver=self.authority_resolver, now=now)
        if final != end:
            raise RecallAuthorizationError("authority changed during canonical validation")
        handoff = _handoff(response, final)
        receipt = AuthorizationReceipt(end.authorization_id, end.authorization_version, end.owner_id, end.session_jti, datetime.now(timezone.utc), end.disclosure_level, tuple(r.item.item_id for r in response.results), disclosure_evidence)
        return RecallServiceResponse(response, handoff, receipt)

    def open_source(self, request: RecallOpenSourceRequest) -> InertSourceReference:
        current = require_fresh_authority(request.authorization, resolver=self.authority_resolver)
        authorize_open_contract(current, request)
        if request.receipt.owner_id != current.owner_id or request.receipt.session_jti != current.session_jti:
            raise RecallAuthorizationError("source-open receipt does not match fresh authority")
        if request.item.item_id not in request.receipt.result_item_ids:
            raise RecallAuthorizationError("source was not authorized by the recall result")
        if not current.allow_locator_open or current.disclosure_level == DisclosureLevel.NONE:
            raise RecallAuthorizationError("source-open permission is absent")
        require_item_authority(current, request.item)
        if not 0 < request.max_snippet_chars <= self.MAX_OPEN_SNIPPET_CHARS:
            raise RecallAuthorizationError("requested snippet bound is invalid")
        registry: SourceRegistry = self.source_registry_factory(current)
        checked_at = datetime.now(timezone.utc)
        locator = validate_open_locator(request.item, owner_id=current.owner_id, registry=registry)
        _check_canonical_disclosure(request.item, owner_id=current.owner_id, registry=registry, checked_at=checked_at)
        final = require_fresh_authority(request.authorization, resolver=self.authority_resolver)
        if final != current:
            raise RecallAuthorizationError("authority changed during source-open validation")
        snippet = None
        if current.disclosure_level == DisclosureLevel.SNIPPET:
            snippet = (request.item.text or "")[: request.max_snippet_chars]
        return InertSourceReference(request.item.item_id, request.item.source_type, request.item.source_id, locator, snippet, current.disclosure_level)

    def status(self, request: RecallStatusRequest) -> dict[str, object]:
        current = require_fresh_authority(request.authorization, resolver=self.authority_resolver)
        authorize_status_contract(current, request)
        return {"authorized": True, "owner_id": current.owner_id, "source_types": tuple(sorted(x.value for x in current.allowed_source_types)), "routes_registered": False}


class _AuthorizedAdapter:
    def __init__(self, adapter: PersonalSourceAdapter, context: RecallAuthorizationContext, hints: RecallContextHints):
        self.adapter, self.context, self.hints = adapter, context, hints
        self.name = adapter.name
        self.source_types = frozenset(getattr(adapter, "source_types", ())) & context.allowed_source_types

    def discover(self, *, owner_id: str):
        if owner_id != self.context.owner_id:
            raise RecallAuthorizationError("adapter owner differs from authorization")
        if not self.source_types:
            return
        for item in self.adapter.discover(owner_id=owner_id):
            try:
                require_item_authority(self.context, item)
            except RecallAuthorizationError:
                continue
            if self.hints.current_conversation_id and item.conversation_id != self.hints.current_conversation_id:
                continue
            if self.hints.active_project_id and item.project_id != self.hints.active_project_id:
                continue
            if self.hints.active_intent_id and item.intent_id != self.hints.active_intent_id:
                continue
            if self.hints.workspace_file_id and item.file_id != self.hints.workspace_file_id:
                continue
            yield item


def authorize_query_contract(context: RecallAuthorizationContext, request: RecallQueryRequest) -> None:
    if not request.raw_query.strip() or len(request.raw_query) > PersonalRecallService.MAX_QUERY_CHARS:
        raise RecallAuthorizationError("query is empty or exceeds the safe bound")
    if context.disclosure_level == DisclosureLevel.NONE or not context.allowed_source_types:
        raise RecallAuthorizationError("recall disclosure is not authorized")
    if request.mode == RecallMode.CURRENT and not context.allow_current:
        raise RecallAuthorizationError("current recall is not authorized")
    if request.mode == RecallMode.HISTORICAL and not context.allow_historical:
        raise RecallAuthorizationError("historical recall is not authorized")
    hints = request.hints
    if hints.active_project_id and not context.project_scope.allows(hints.active_project_id):
        raise RecallAuthorizationError("project hint is outside authorization")
    if hints.current_conversation_id and not context.conversation_scope.allows(hints.current_conversation_id):
        raise RecallAuthorizationError("conversation hint is outside authorization")


def authorize_open_contract(context: RecallAuthorizationContext, request: RecallOpenSourceRequest) -> None:
    """Future route policy: validation only, with no HTTP/framework dependency."""
    if request.authorization.owner_id != context.owner_id:
        raise RecallAuthorizationError("open-source owner mismatch")
    if request.receipt.authorization_id != context.authorization_id or request.receipt.authorization_version != context.authorization_version:
        raise RecallAuthorizationError("open-source authorization receipt is stale or forged")


def authorize_status_contract(context: RecallAuthorizationContext, request: RecallStatusRequest) -> None:
    if request.authorization.owner_id != context.owner_id or request.authorization.session_jti != context.session_jti:
        raise RecallAuthorizationError("status authorization mismatch")


def _check_canonical_disclosure(item: PersonalKnowledgeItem, *, owner_id: str, registry: SourceRegistry, checked_at: datetime) -> DisclosureEvidence:
    try:
        validate_open_locator(item, owner_id=owner_id, registry=registry)
    except ValueError as exc:
        raise RecallAuthorizationError("canonical source is missing, revoked, or unavailable") from exc
    record = registry.resolve(source_id=item.source_id, owner_id=owner_id, source_type=item.source_type.value, locator=item.provenance.locator)
    if record is None:
        raise RecallAuthorizationError("canonical source disappeared before disclosure")
    if record.owner_id != owner_id:
        raise RecallAuthorizationError("canonical owner changed before disclosure")
    retrieved_generation = item.metadata.get("canonical_generation")
    result = DisclosureCheckResult.VALID
    if record.lifecycle_state in ("revoked", "purged"):
        result = DisclosureCheckResult.REVOKED
    elif record.lifecycle_state in ("deleted",):
        result = DisclosureCheckResult.DELETED
    elif record.lifecycle_state in ("superseded",):
        result = DisclosureCheckResult.SUPERSEDED
    elif record.canonical_generation is not None and retrieved_generation is not None and record.canonical_generation != retrieved_generation:
        result = DisclosureCheckResult.STALE
    elif record.content_identity is not None and item.content_hash is not None and record.content_identity != item.content_hash:
        result = DisclosureCheckResult.CONTENT_MISMATCH
    if result != DisclosureCheckResult.VALID:
        raise RecallAuthorizationError(f"canonical disclosure check failed: {result.value}")
    return DisclosureEvidence(item.source_id, owner_id, record.canonical_generation, retrieved_generation, record.content_identity, record.lifecycle_state, checked_at, result)


def _require_same_or_narrower_result_authority(start: RecallAuthorizationContext, end: RecallAuthorizationContext, results: list[RetrievalResult]) -> None:
    if (start.authorization_version != end.authorization_version
            or start.allow_current != end.allow_current
            or start.allow_historical != end.allow_historical):
        raise RecallAuthorizationError("authority changed during recall; retry under the fresh grant")
    for result in results:
        require_item_authority(end, result.item)
    if end.disclosure_level == DisclosureLevel.NONE:
        raise RecallAuthorizationError("disclosure was revoked during recall")


def _handoff(response: RecallResponse, context: RecallAuthorizationContext) -> MainAIRecallHandoff:
    historical = set(response.historical_items)
    rows = []
    for result in response.results:
        require_item_authority(context, result.item)
        item = result.item
        snippet = (item.text or "")[:2_000] if context.disclosure_level == DisclosureLevel.SNIPPET else None
        rows.append(RecallEvidenceHandoff(item.item_id, item.source_type, item.source_id, item.source_authority.value, item.verification_state.value, item.decision_state.value, item.index_state.value, item.currentness, item.item_id in historical, result.contradictions, result.why_matched, item.provenance.locator, item.content_reference, snippet))
    return MainAIRecallHandoff(tuple(rows), response.coverage, tuple(response.index_warnings), tuple(response.contradictions), tuple(response.unresolved))


def _prune_response_metadata(response: RecallResponse) -> None:
    """A filtered grant must not leak disallowed item IDs through side-channel fields."""
    allowed = {result.item.item_id for result in response.results}
    response.current_items = [item_id for item_id in response.current_items if item_id in allowed]
    response.historical_items = [item_id for item_id in response.historical_items if item_id in allowed]
    response.clusters = {key: [item_id for item_id in values if item_id in allowed] for key, values in response.clusters.items() if any(item_id in allowed for item_id in values)}
    response.contradictions = [(left, right) for left, right in response.contradictions if left in allowed and right in allowed]
    response.contradiction_candidates = [candidate for candidate in response.contradiction_candidates if candidate.left_item_id in allowed and candidate.right_item_id in allowed]
    response.unresolved = [item_id for item_id in response.unresolved if item_id in allowed]
    response.synthesis = f"Retrieved {len(allowed)} authorized evidence item(s). Coverage: {response.coverage.state.value}."
