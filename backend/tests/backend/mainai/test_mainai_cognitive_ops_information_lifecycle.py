"""`app.mainai_cognitive_ops.information_lifecycle` + `hot_warm_cold` + `indexing`. See
docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.mainai_cognitive_ops.hot_warm_cold import classify_temperature, transition_tier
from app.mainai_cognitive_ops.indexing import RetrievalEvent, assess_retrieval_effectiveness, detect_stale_index
from app.mainai_cognitive_ops.information_lifecycle import InformationItem, deduplicate_exact, defragment, fragment
from app.mainai_cognitive_ops.types import InformationTemperature, InformationTier


def test_fragment_preserves_parent_lineage():
    fragments = fragment(source_id="doc-1", unit_boundaries=("section a", "section b"))
    assert all(f.parent_id == "doc-1" for f in fragments)
    assert fragments[0].provenance == ("doc-1",)


def test_large_history_becomes_compact_bundle_without_losing_critical_state():
    items = fragment(source_id="bug-42", unit_boundaries=("issue", "logs", "commit", "test", "review", "fix"))
    bundle = defragment(bundle_id="bug-42-chain", related_items=items)
    assert bundle.reconstructable is True
    assert len(bundle.member_item_ids) == 6


def test_two_differently_worded_duplicate_facts_are_linked_via_exact_key():
    items = (
        InformationItem(item_id="a", tier=InformationTier.RAW_SOURCE, content_ref="wire-story-A", provenance=("a",)),
        InformationItem(item_id="b", tier=InformationTier.RAW_SOURCE, content_ref="wire-story-A", provenance=("b",)),
    )
    results = deduplicate_exact(items=items, key_fn=lambda i: i.content_ref)
    assert len(results) == 1
    assert set(results[0].retained_provenance) == {"a", "b"}
    assert results[0].duplicate_item_ids == ("b",)


def test_two_similar_but_distinct_facts_are_not_incorrectly_deduplicated():
    items = (
        InformationItem(item_id="a", tier=InformationTier.RAW_SOURCE, content_ref="revenue up 10% in Q1"),
        InformationItem(item_id="b", tier=InformationTier.RAW_SOURCE, content_ref="revenue up 11% in Q1"),
    )
    results = deduplicate_exact(items=items, key_fn=lambda i: i.content_ref)
    assert results == ()


def test_hot_warm_cold_transitions_and_cold_is_not_irrelevant():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert classify_temperature(last_used_at=now - timedelta(hours=1), now=now) == InformationTemperature.HOT
    assert classify_temperature(last_used_at=now - timedelta(days=10), now=now) == InformationTemperature.WARM
    assert classify_temperature(last_used_at=now - timedelta(days=90), now=now) == InformationTemperature.COLD

    transition = transition_tier(item_id="x", from_temperature=InformationTemperature.HOT, to_temperature=InformationTemperature.COLD, provenance=("src-1",))
    assert transition.provenance_preserved is True


def test_tier_transition_without_provenance_is_flagged_not_preserved():
    transition = transition_tier(item_id="x", from_temperature=InformationTemperature.HOT, to_temperature=InformationTemperature.COLD, provenance=())
    assert transition.provenance_preserved is False


def test_stale_index_detected_when_source_changed_after_last_verification():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert detect_stale_index(index_last_verified_at=now - timedelta(days=5), source_last_changed_at=now - timedelta(days=1)) is True
    assert detect_stale_index(index_last_verified_at=now, source_last_changed_at=now - timedelta(days=1)) is False


def test_retrieval_effectiveness_measures_hit_and_false_positive_rate():
    events = (
        RetrievalEvent(query="q1", index_used="ix_sha", hit=True),
        RetrievalEvent(query="q2", index_used="ix_sha", hit=False, false_positive=True),
    )
    effectiveness = assess_retrieval_effectiveness(events)
    assert effectiveness.hit_rate == 0.5
    assert effectiveness.false_positive_rate == 0.5
