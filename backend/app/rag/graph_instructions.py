"""Round 9 M3: the graph becomes part of what MainAI actually SAYS.

Round 8 gave the claim graph an API surface (writer, evidence-chain, consequences,
overlaps) and proved it through endpoint tests. But an endpoint nobody calls during a
conversation is stored reasoning, not working reasoning. This module is the invocation
layer the chat and workbench prompt assembly now calls with the RETRIEVED sources:

  - second-order consequences of the retrieved documents' supersedes/contradicts edges
    (app/rag/epistemics.second_order_consequences — owner-scoped, cycle-safe, depth-capped);
  - cross-domain overlaps: the same statement surfacing in 2+ projects
    (app/rag/epistemics.cross_domain_claim_overlaps), filtered to statements whose words
    appear in the question so the signal is RELEVANT, not a dump;
  - the topic landscape of the most recently stamped research_topic among the retrieved
    claims (app/rag/evidence_metadata.summarize_topic_landscape — Round 9 M2), including
    its DECLARED disagreements.

Everything is fail-open at the call sites and read-only here. The instruction lines are
Swedish (matching the rest of the prompt layer) and carry their own neutrality rules: a
consequence is a candidate to VERIFY, never an established fact; an overlap is a signal
to check, not corroboration; a disagreement is presented with both sides.

Anti-inflation guarantees that hold here (tested): no consequence line can raise a
verdict (the ladder is untouched — this layer only ADDS prompt text); overlap matches are
token-overlap based and never claimed as agreement; and the graph of ANOTHER owner is
invisible (every query is owner-scoped).
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.rag.evidence_metadata import summarize_topic_landscape
from app.rag.epistemics import cross_domain_claim_overlaps, second_order_consequences


def _content_tokens(text: str) -> set[str]:
    """Deterministic, language-agnostic tokenization for relevance filtering: lowercase,
    split on non-letters, drop 2-char noise tokens. Good enough to decide whether an
    overlap's statement is topically related to the question — and deliberately too dumb
    to manufacture relatedness that isn't there."""
    import re

    return {t for t in re.findall(r"[^\W\d_]+", (text or "").lower(), re.UNICODE) if len(t) > 2}


def format_graph_context_instructions(
    db: Session, owner_id: uuid.UUID, retrieved_source_ids, question_text: str, *, max_depth: int = 2
) -> str:
    """The GRAFKONTEXT block for the retrieved sources. Returns '' when the sources carry
    no graph state — the caller appends nothing rather than an empty section."""
    roots = [s for s in dict.fromkeys(retrieved_source_ids) if s is not None]
    if not roots:
        return ""

    lines: list[str] = ["GRAFKONTEXT (kandidater att VERIFIERA — aldrig etablerade fakta):"]

    consequences = second_order_consequences(db, owner_id, roots, max_depth=max_depth)
    if consequences:
        lines.append(
            f"- andrangordningseffekter: {len(consequences)} beroende källa(or) påverkas av "
            "supersederings-/motsägelsekanter från de hämtade källorna (djup "
            f"1–{max(c.depth for c in consequences)}) — kontrollera dem mot den nya evidensen."
        )
        for c in consequences[:5]:
            lines.append(
                f"  · {c.kind}: beroende {c.dependent_source_id} (djup {c.depth}, väg {len(c.path)} steg)"
            )
    else:
        lines.append("- inga andrangordningseffekter från de hämtade källorna (inga triggerkanter).")

    tokens = _content_tokens(question_text)
    overlaps = cross_domain_claim_overlaps(db, owner_id, limit=100)
    relevant = []
    for o in overlaps:
        overlap_tokens = _content_tokens(o.claim_text)
        if tokens and overlap_tokens and len(tokens & overlap_tokens) >= 2:
            relevant.append(o)
    if relevant:
        lines.append(
            f"- korsdomän-överlapp: {len(relevant)} utsaga(or) förekommer i 2+ projekt och "
            "matchar frågans innehåll — signal att SAMKÖRA, inte bekräftelse:"
        )
        for o in relevant[:5]:
            lines.append(f"  · '{o.claim_text}' i {len(o.project_ids)} projekt")
    else:
        lines.append("- inga frågerelevanta korsdomän-överlapp (matchning kräver 2+ gemensama innehållsord).")

    topics = (
        db.execute(
            text(_TOPIC_SQL),
            {"owner": str(owner_id), "roots": [str(r) for r in roots], "limit": 1},
        )
        .scalars()
        .all()
    )
    for topic in topics[:1]:
        landscape = summarize_topic_landscape(db, owner_id, topic)
        lines.append("- " + _topic_line(landscape))
        # Round 10 M6: the topic's scientific composition + bounded investigation policy
        # ride the same block (fail-open at the caller; deterministic, read-only here).
        from app.rag.scientific_reasoning import analyze_topic_science, format_topic_science_instructions, investigation_policy

        science = analyze_topic_science(db, owner_id, topic)
        policy = investigation_policy(science)
        lines.append("- " + format_topic_science_instructions(science, policy))
        if landscape.disagreements:
            for d in landscape.disagreements[:3]:
                sides = f"'{d.from_claim_text}' vs '{d.to_claim_text}'"
                if d.cross_country:
                    sides += f" ({d.from_setting} vs {d.to_setting})"
                lines.append(f"  · konflikt: {sides} — båda sidor står, ingen vinner här.")

    return "\n".join(lines)


def _topic_line(landscape) -> str:
    from app.rag.evidence_metadata import format_topic_landscape_instructions

    return format_topic_landscape_instructions(landscape)


_TOPIC_SQL = (
    "SELECT m.metadata_json->>'research_topic' AS topic "
    "FROM claim_evidence_metadata m "
    "JOIN knowledge_claims c ON c.id = m.claim_id AND c.owner_id = m.owner_id "
    "WHERE m.owner_id = :owner AND c.source_id = ANY(CAST(:roots AS uuid[])) "
    "AND m.metadata_json->>'research_topic' IS NOT NULL "
    "ORDER BY m.updated_at DESC LIMIT :limit"
)
