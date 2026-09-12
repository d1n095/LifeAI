"""Deep Investigation Engine -- actor/relationship/money/timeline graph. See
docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the architecture decision.

Pure: no `db`, no I/O -- builds an in-memory graph from caller-supplied, already-evidenced
`Actor`/`Relationship`/`MoneyFlow`/`TimelineEvent` records (each of which already requires
`evidenced_by` evidence-link ids -- this module never invents a tie/flow/event on its own). A
caller wanting this durable stores it as `provenance` on the relevant `mainai_research_
evidence_links`/hypothesis row (`research_ledger.py`), never a second graph table.

RELATIONSHIP != CONTROL. BENEFIT != PROOF OF INTENT. CONTACT != CONSPIRACY -- structurally: no
function or field anywhere in this module represents "control," "intent," or "conspiracy"; only
plain, evidenced ties/flows/events."""

from __future__ import annotations

from dataclasses import dataclass

from app.mainai_research.types import Actor, MoneyFlow, Relationship, TimelineEvent


@dataclass(frozen=True)
class InvestigationGraph:
    actors: tuple[Actor, ...]
    relationships: tuple[Relationship, ...]
    money_flows: tuple[MoneyFlow, ...]
    timeline: tuple[TimelineEvent, ...]

    def actor(self, actor_id: str) -> Actor | None:
        for a in self.actors:
            if a.actor_id == actor_id:
                return a
        return None

    def relationships_for(self, actor_id: str) -> tuple[Relationship, ...]:
        return tuple(r for r in self.relationships if r.from_actor_id == actor_id or r.to_actor_id == actor_id)

    def money_flows_for(self, actor_id: str) -> tuple[MoneyFlow, ...]:
        return tuple(m for m in self.money_flows if m.from_actor_id == actor_id or m.to_actor_id == actor_id)

    def beneficiaries_of(self, actor_id: str) -> tuple[str, ...]:
        """Who benefits FROM `actor_id` -- a plain, evidenced financial fact. BENEFIT != PROOF
        OF INTENT: callers must not read this as a claim of purpose."""

        return tuple(m.to_actor_id for m in self.money_flows if m.from_actor_id == actor_id)

    def funders_of(self, actor_id: str) -> tuple[str, ...]:
        return tuple(m.from_actor_id for m in self.money_flows if m.to_actor_id == actor_id)

    def timeline_ordered(self) -> tuple[TimelineEvent, ...]:
        return tuple(sorted(self.timeline, key=lambda e: e.occurred_at))

    def unevidenced_relationships(self) -> tuple[Relationship, ...]:
        """Surfaces any relationship recorded WITHOUT a real evidence link -- never silently
        treated as equally reliable as an evidenced one."""

        return tuple(r for r in self.relationships if not r.evidenced_by)


def build_investigation_graph(
    *, actors: tuple[Actor, ...] = (), relationships: tuple[Relationship, ...] = (),
    money_flows: tuple[MoneyFlow, ...] = (), timeline: tuple[TimelineEvent, ...] = (),
) -> InvestigationGraph:
    """Pure constructor -- validates every relationship/money-flow endpoint refers to a real
    `Actor` already in `actors` (fails closed on a dangling reference rather than silently
    accepting one)."""

    known_ids = {a.actor_id for a in actors}
    for r in relationships:
        if r.from_actor_id not in known_ids or r.to_actor_id not in known_ids:
            raise ValueError(f"relationship references an actor_id not present in actors: {r.from_actor_id!r} -> {r.to_actor_id!r}")
    for m in money_flows:
        if m.from_actor_id not in known_ids or m.to_actor_id not in known_ids:
            raise ValueError(f"money flow references an actor_id not present in actors: {m.from_actor_id!r} -> {m.to_actor_id!r}")
    return InvestigationGraph(actors=actors, relationships=relationships, money_flows=money_flows, timeline=timeline)
