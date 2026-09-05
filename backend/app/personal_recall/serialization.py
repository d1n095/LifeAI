from __future__ import annotations

from typing import Any

from app.personal_recall.types import RecallResponse


class RecallSerializationError(ValueError):
    pass


def serialize_for_local_client(response: RecallResponse, *, owner_id: str, include_content: bool = False) -> dict[str, Any]:
    """Owner-gated local-client projection; raw query is never serialized.

    This is deliberately not a provider-context serializer. Callers cannot select a provider
    audience, and raw content is opt-in for an already-authorized local UI only.
    """
    if any(result.item.owner_id != owner_id for result in response.results):
        raise RecallSerializationError("response contains a foreign-owner result")
    results: list[dict[str, Any]] = []
    for result in response.results:
        item = result.item
        row: dict[str, Any] = {
            "item_id": item.item_id,
            "source_type": item.source_type.value,
            "source_id": item.source_id,
            "content_reference": item.content_reference,
            "decision_state": item.decision_state.value,
            "verification_state": item.verification_state.value,
            "source_authority": item.source_authority.value,
            "index_state": item.index_state.value,
            "relevance_score": result.relevance_score,
            "semantic_score": result.semantic_score,
            "temporal_score": result.temporal_score,
            "authority_score": result.authority_score,
            "truth_score": result.truth_score,
            "why_matched": list(result.why_matched),
            "provenance": {
                "source_type": item.provenance.source_type.value,
                "source_id": item.provenance.source_id,
                "locator": item.provenance.locator,
                "section": item.provenance.section,
                "version": item.provenance.version,
            },
        }
        if include_content:
            row["text"] = item.text
        results.append(row)
    return {
        "audience": "local_owner_client",
        "owner_id": owner_id,
        "query": {"intents": [intent.value for intent in response.query.intents]},
        "coverage": {
            "state": response.coverage.state.value,
            "missing_source_types": [value.value for value in response.coverage.missing_source_types],
            "truncated": response.coverage.truncated,
            "source_states": {source.value: state.value for source, state in response.coverage.source_states},
        },
        "results": results,
    }
