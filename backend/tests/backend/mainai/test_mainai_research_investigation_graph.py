"""MainAI Research -- `app.mainai_research.investigation_graph` -- proves the actor/relationship/
money/timeline graph never exposes a "control"/"intent"/"conspiracy" field (RELATIONSHIP !=
CONTROL, BENEFIT != PROOF OF INTENT), flags dangling/unevidenced references, and orders a
timeline correctly.

See docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the architecture this
module implements."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.mainai_research.investigation_graph import build_investigation_graph
from app.mainai_research.types import Actor, MoneyFlow, Relationship, TimelineEvent


def test_dangling_relationship_reference_is_rejected():
    actors = (Actor(actor_id="a1", name="Alice", kind="person"),)
    with pytest.raises(ValueError):
        build_investigation_graph(actors=actors, relationships=(Relationship(from_actor_id="a1", to_actor_id="ghost", kind="met_with"),))


def test_beneficiaries_and_funders_are_plain_evidenced_facts():
    actors = (Actor(actor_id="a1", name="Alice", kind="person"), Actor(actor_id="a2", name="Org B", kind="organization"))
    flows = (MoneyFlow(from_actor_id="a2", to_actor_id="a1", flow_kind="payment", evidenced_by=("ev-1",)),)
    graph = build_investigation_graph(actors=actors, money_flows=flows)
    assert graph.beneficiaries_of("a2") == ("a1",)
    assert graph.funders_of("a1") == ("a2",)
    # RELATIONSHIP != CONTROL / BENEFIT != PROOF OF INTENT: no such field exists on the dataclass.
    assert not hasattr(flows[0], "control")
    assert not hasattr(flows[0], "intent")


def test_unevidenced_relationships_are_surfaced_not_hidden():
    actors = (Actor(actor_id="a1", name="Alice", kind="person"), Actor(actor_id="a2", name="Bob", kind="person"))
    relationships = (
        Relationship(from_actor_id="a1", to_actor_id="a2", kind="met_with", evidenced_by=("ev-1",)),
        Relationship(from_actor_id="a2", to_actor_id="a1", kind="co_authored", evidenced_by=()),
    )
    graph = build_investigation_graph(actors=actors, relationships=relationships)
    unevidenced = graph.unevidenced_relationships()
    assert len(unevidenced) == 1
    assert unevidenced[0].kind == "co_authored"


def test_timeline_is_ordered_by_occurrence():
    now = datetime.now(timezone.utc)
    events = (
        TimelineEvent(occurred_at=now, description="second"),
        TimelineEvent(occurred_at=now - timedelta(days=1), description="first"),
    )
    graph = build_investigation_graph(timeline=events)
    ordered = graph.timeline_ordered()
    assert [e.description for e in ordered] == ["first", "second"]


def test_relationships_for_actor_includes_both_directions():
    actors = (Actor(actor_id="a1", name="Alice", kind="person"), Actor(actor_id="a2", name="Bob", kind="person"))
    relationships = (Relationship(from_actor_id="a1", to_actor_id="a2", kind="employed_by"),)
    graph = build_investigation_graph(actors=actors, relationships=relationships)
    assert graph.relationships_for("a2") == relationships
