"""Evidence-aware epistemics over the EXISTING claim/source/graph layers — the P2
"cognitive intelligence" increment (Founder Alpha round, 2026-11-18), extending
app/rag/trust.py, app/rag/graph_traverse.py and app/rag/claims.py rather than adding a
second engine.

The mission's five-state ladder: **verified fact / conflicting evidence / hypothesis /
assumption / unknown**. Everything that decides the ladder comes from signals the repo
ALREADY stores — never from a model leg and never from this module's own judgement:

- claim level: `KnowledgeClaim.status` (active/proposed/disputed/historical),
  `assess_claim_confidence()` (certain/likely/uncertain/conflict/no_basis — recomputed
  LIVE from `claim_relationships`, so a `contradicts` edge added after extraction caps the
  verdict at conflicting_evidence immediately), `grounding_score`, `chunk_id`.
- source level: `Document.active_truth_status` (superseded/disputed), `deleted_at`.
- graph level: `source_relationships` edges (supersedes/contradicts/supports/derived_from)
  via the same cycle-safe recursive walks app/rag/graph_traverse.py uses.

Deterministic mapping (documented order — first match wins):

1. contradicts edge on the claim (claim_relationships) OR claim.status == disputed
     -> conflicting_evidence. A dispute is itself a stored fact and outranks everything
        else: a claim can never be presented as verified while a contradiction is on
        record (the same rule assess_claim_confidence() applies to the confidence bucket).
2. no live grounding (chunk purged, or source deleted) -> unknown. The claim text
     survives as revision history but its basis is no longer retrievable — any retrieval
     path must treat it like vector_store.py does (check the source's deleted_at; a NULL
     chunk_id is not automatically safe).
3. claim.status == historical OR source.active_truth_status == superseded
     -> hypothesis (reason names which). Not current, but not contradicted either.
4. claim.status == proposed -> hypothesis (not yet decided).
5. confidence conflict -> conflicting_evidence (defensive — rule 1 already caught the
     contradicts edge; this catches a stored `conflict` bucket with no live edge).
6. confidence in (certain, likely) -> verified_fact. `certain` requires independent
     corroboration or very high grounding (assess_claim_confidence); `likely` means
     grounded in its own active source with no known contradiction — both count as
     established-by-evidence, with the reason field recording WHICH.
7. confidence == uncertain -> hypothesis.
8. confidence == no_basis -> assumption. Stated, but nothing in its own chunk grounds
     it — exactly what an assumption is. (And per assess_claim_confidence, corroboration
     can never rescue it: two ungrounded claims agreeing is not evidence.)

`second_order_consequences()` adds the "interaction / second-order consequence discovery"
half of the mission on top of graph_traverse.py's walks, with the same discipline:
structural, path-evidenced, owner-scoped, no semantic invention. Three rules:

- stale_basis: X supersedes Y, and Z (transitively) depends on Y through
  derived_from/supports edges (inbound closure). Z is downstream of a superseded source.
- contested_ground: X contradicts Y; Z depends on Y (or on X). Z stands on contested
  ground — split per side so the two downstream branches can be compared.
- opposing_support: C supports X and D supports Y while X contradicts Y — C and D are
  committed to opposite sides of a recorded conflict (cross-domain interaction when C
  and D live in different projects; reported via the project ids, never asserted as
  "disagreement in meaning").

Every verdict and consequence carries the exact ids/paths that justify it, so the
founder (or the next reasoning layer) can audit WHY. No new tables, no migration, no
model leg — RLS composes because every query filters owner_id explicitly AND the session
setting applies underneath, exactly like graph_traverse.py.
"""

import enum
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from app.models.claim_relationship import ClaimRelationship, ClaimRelationshipType
from app.models.document import Document
from app.models.knowledge_claim import ClaimConfidence, ClaimStatus, KnowledgeClaim
from app.models.source_relationship import RelationshipType
from app.rag.graph_traverse import traverse_source_graph
from app.rag.trust import assess_claim_confidence


class EpistemicStatus(str, enum.Enum):
    """The mission's five-state evidence ladder — coarse, deterministic, auditable."""

    verified_fact = "verified_fact"
    conflicting_evidence = "conflicting_evidence"
    hypothesis = "hypothesis"
    assumption = "assumption"
    unknown = "unknown"


@dataclass
class EpistemicVerdict:
    """One claim's position on the evidence ladder, with the reasons and ids that justify
    it. `confidence` is the LIVE recomputation (assess_claim_confidence), never the stored
    extraction-time bucket."""

    claim_id: uuid.UUID
    claim_text: str
    status: EpistemicStatus
    confidence: ClaimConfidence
    claim_status: ClaimStatus
    reasons: list[str] = field(default_factory=list)
    source_id: uuid.UUID | None = None
    source_title: str | None = None
    project_id: uuid.UUID | None = None
    source_active_truth_status: str | None = None
    chunk_id: uuid.UUID | None = None
    has_live_grounding: bool = False


# Precompiled whitespace normalizer for the cross-domain claim-overlap discovery.
_WS_RUN = re.compile(r"\s+")


def _visible_source(db: Session, owner_id: uuid.UUID, source_id: uuid.UUID) -> Document | None:
    return db.query(Document).filter(Document.id == source_id, Document.uploaded_by == owner_id).first()


def classify_claim(
    db: Session,
    owner_id: uuid.UUID,
    claim: KnowledgeClaim,
    *,
    quality_cap: "EpistemicStatus | None" = None,
) -> EpistemicVerdict:
    """The deterministic evidence ladder for one claim (mapping order documented in the
    module docstring). Owner-scoped: the source is loaded through the owner's visibility
    filter, and the claim_relationships lookup filters owner_id explicitly.

    `quality_cap` (optional) comes from app/rag/evidence_quality.grade_evidence(): the
    highest EpistemicStatus the claim's study metadata can support. When the ladder's
    verdict EXCEEDS the cap it is downgraded to the cap with a named reason — a dispute
    (conflicting_evidence) is never affected (a contradiction is a contradiction whatever
    the metadata quality), and a cap can only ever lower a verdict, never raise one."""
    reasons: list[str] = []

    contradicted = (
        db.query(ClaimRelationship)
        .filter(
            ClaimRelationship.owner_id == owner_id,
            ClaimRelationship.relationship_type == ClaimRelationshipType.contradicts,
            or_(ClaimRelationship.from_claim_id == claim.id, ClaimRelationship.to_claim_id == claim.id),
        )
        .first()
        is not None
    )
    if contradicted:
        reasons.append("a claim_relationships contradicts edge is on record")
    if claim.status == ClaimStatus.disputed:
        reasons.append("claim status is disputed")

    source = _visible_source(db, owner_id, claim.source_id)
    has_live_grounding = claim.chunk_id is not None and source is not None and source.deleted_at is None
    live_confidence = assess_claim_confidence(db, claim)

    if contradicted or claim.status == ClaimStatus.disputed:
        status = EpistemicStatus.conflicting_evidence
    elif not has_live_grounding:
        status = EpistemicStatus.unknown
        reasons.append("grounding no longer retrievable (chunk purged or source deleted)")
    elif claim.status == ClaimStatus.historical:
        status = EpistemicStatus.hypothesis
        reasons.append("claim is historical — superseded or explicitly marked historical")
    elif source.active_truth_status is not None and source.active_truth_status == "superseded":
        status = EpistemicStatus.hypothesis
        reasons.append("source is superseded by newer evidence")
    elif claim.status == ClaimStatus.proposed:
        status = EpistemicStatus.hypothesis
        reasons.append("claim is proposed — not yet decided")
    elif live_confidence == ClaimConfidence.conflict:
        status = EpistemicStatus.conflicting_evidence
        reasons.append("live confidence computes to conflict")
    elif live_confidence in (ClaimConfidence.certain, ClaimConfidence.likely):
        status = EpistemicStatus.verified_fact
        reasons.append(
            "independently corroborated" if live_confidence == ClaimConfidence.certain else "grounded in its active source, no known contradiction"
        )
    elif live_confidence == ClaimConfidence.uncertain:
        status = EpistemicStatus.hypothesis
        reasons.append("grounding is weak (uncertain)")
    else:  # no_basis — the only bucket left
        status = EpistemicStatus.assumption
        reasons.append("nothing in the source chunk grounds this statement (no_basis)")

    verdict_status = status
    if quality_cap is not None and status == EpistemicStatus.verified_fact and quality_cap != EpistemicStatus.verified_fact:
        verdict_status = quality_cap
        reasons.append(f"evidence-quality cap: study metadata supports at most {quality_cap.value}")

    return EpistemicVerdict(
        claim_id=claim.id,
        claim_text=claim.claim_text,
        status=verdict_status,
        confidence=live_confidence,
        claim_status=claim.status,
        reasons=reasons,
        source_id=claim.source_id,
        source_title=source.title if source is not None else None,
        project_id=source.project_id if source is not None else None,
        source_active_truth_status=source.active_truth_status.value if source is not None and source.active_truth_status is not None else None,
        chunk_id=claim.chunk_id,
        has_live_grounding=has_live_grounding,
    )


def epistemic_ledger(
    db: Session,
    owner_id: uuid.UUID,
    *,
    source_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> list[EpistemicVerdict]:
    """Classify every claim the owner can see (optionally narrowed to one source or one
    project), oldest first. Owner-scoped end to end; claims on invisible sources are
    unreachable by construction (the source join is the owner filter)."""
    query = db.query(KnowledgeClaim).filter(KnowledgeClaim.owner_id == owner_id)
    if source_id is not None:
        query = query.filter(KnowledgeClaim.source_id == source_id)
    if project_id is not None:
        query = query.join(Document, Document.id == KnowledgeClaim.source_id).filter(
            Document.uploaded_by == owner_id, Document.project_id == project_id
        )
    claims = query.order_by(KnowledgeClaim.created_at.asc(), KnowledgeClaim.id.asc()).all()
    return [classify_claim(db, owner_id, c) for c in claims]


def epistemic_counts(verdicts) -> dict[str, int]:
    """Coarse ladder counts for a verdict list — the summary a prompt-building layer can
    cite instead of a single overconfident number ("3 verified, 1 conflicting, 2 assumed,
    1 unknown"), never replacing the per-claim verdicts themselves."""
    counts = {s.value: 0 for s in EpistemicStatus}
    for v in verdicts:
        counts[v.status.value] += 1
    return counts


# --------------------------------------------------------------------------------------
# Second-order consequences — structural discovery over the recorded graph
# --------------------------------------------------------------------------------------


@dataclass
class SecondOrderConsequence:
    """One downstream consequence discovered by propagating a recorded source-level edge
    through derived_from/supports dependencies. `path` is the dependency chain from the
    trigger's head to the dependent — the audit trail for WHY."""

    kind: str  # stale_basis | contested_ground | opposing_support
    root_source_id: uuid.UUID
    trigger_edge: tuple[uuid.UUID, uuid.UUID, str]  # (from, to, relationship_type)
    dependent_source_id: uuid.UUID
    dependent_project_id: uuid.UUID | None
    depth: int
    path: list[uuid.UUID] = field(default_factory=list)


_DEPENDENT_TYPES = (RelationshipType.derived_from, RelationshipType.supports)


def _project_map(db: Session, owner_id: uuid.UUID, source_ids) -> dict[uuid.UUID, uuid.UUID | None]:
    ids = list({s for s in source_ids})
    if not ids:
        return {}
    rows = db.execute(
        text("SELECT id::text, project_id::text FROM documents WHERE uploaded_by = :owner AND id = ANY(CAST(:ids AS uuid[]))"),
        {"owner": str(owner_id), "ids": [str(s) for s in ids]},
    ).all()
    return {uuid.UUID(r[0]): (uuid.UUID(r[1]) if r[1] else None) for r in rows}


def _trigger_edges(db: Session, owner_id: uuid.UUID, root_ids, types) -> list[tuple[uuid.UUID, uuid.UUID, str]]:
    """Recorded edges of the given types incident on the roots (either direction),
    owner-scoped, deterministic order."""
    if not root_ids:
        return []
    rows = db.execute(
        text(
            "SELECT from_source_id::text, to_source_id::text, relationship_type::text "
            "FROM source_relationships WHERE owner_id = :owner "
            "AND relationship_type::text = ANY(CAST(:types AS text[])) "
            "AND (from_source_id = ANY(CAST(:roots AS uuid[])) OR to_source_id = ANY(CAST(:roots AS uuid[]))) "
            "ORDER BY from_source_id::text, to_source_id::text, relationship_type"
        ),
        {"owner": str(owner_id), "types": [t.value for t in types], "roots": [str(r) for r in set(root_ids)]},
    ).all()
    return [(uuid.UUID(r[0]), uuid.UUID(r[1]), r[2]) for r in rows]


def _dependents(db: Session, owner_id: uuid.UUID, head_ids, max_depth: int) -> list:
    """Everything that transitively depends on `head_ids` through derived_from/supports
    edges — i.e. the inbound closure ("Z derived_from Y" / "Z supports Y" make Z a
    dependent of Y). graph_traverse's walk is reused unchanged: owner-scoped, cycle-safe,
    depth-capped."""
    return traverse_source_graph(
        db, owner_id, head_ids, direction="inbound", relationship_types=_DEPENDENT_TYPES, max_depth=max_depth
    )


def _downstream_consequences(
    db: Session,
    owner_id: uuid.UUID,
    kind: str,
    root_source_id: uuid.UUID,
    trigger: tuple[uuid.UUID, uuid.UUID, str],
    head_ids,
    max_depth: int,
    projects: dict[uuid.UUID, uuid.UUID | None],
) -> list[SecondOrderConsequence]:
    hops = _dependents(db, owner_id, head_ids, max_depth)
    out = []
    for h in hops:
        terminal = h.from_source_id  # inbound walk: the dependent is the hop's FROM end
        if terminal == root_source_id:
            continue  # a direct edge on the root itself is recorded knowledge, not a discovery
        out.append(
            SecondOrderConsequence(
                kind=kind,
                root_source_id=root_source_id,
                trigger_edge=trigger,
                dependent_source_id=terminal,
                dependent_project_id=projects.get(terminal),
                depth=h.depth,
                path=list(h.source_path),
            )
        )
    out.sort(key=lambda c: (c.depth, str(c.dependent_source_id), [str(p) for p in c.path]))
    return out


def second_order_consequences(
    db: Session,
    owner_id: uuid.UUID,
    root_source_ids,
    *,
    max_depth: int = 3,
) -> list[SecondOrderConsequence]:
    """Propagate every supersedes/contradicts edge incident on the roots through the
    derived_from/supports dependency closure (mission: "interaction and second-order
    consequence discovery across domains"). Deduplicated per
    (kind, trigger_edge, dependent); shallowest path wins. Roots with no incident
    trigger edges return [] — never an error."""
    roots = list(dict.fromkeys(root_source_ids))
    if not roots:
        return []
    depth = min(max(1, int(max_depth)), 12)

    triggers = _trigger_edges(db, owner_id, roots, (RelationshipType.supersedes, RelationshipType.contradicts))
    if not triggers:
        return []

    touched = set(roots)
    for f, t, _k in triggers:
        touched.add(f)
        touched.add(t)
    projects = _project_map(db, owner_id, touched)

    results: dict[tuple[str, tuple[uuid.UUID, uuid.UUID, str], uuid.UUID], SecondOrderConsequence] = {}

    for trigger_from, trigger_to, rel in triggers:
        # Supersession: everything depending on the superseded side (the trigger's TO end)
        # has a stale basis. Contradiction: both sides' dependents stand on contested
        # ground — reported per side, so downstream branches can be compared.
        if rel == RelationshipType.supersedes.value:
            for c in _downstream_consequences(
                db, owner_id, "stale_basis", trigger_from, (trigger_from, trigger_to, rel), [trigger_to], depth, projects
            ):
                key = (c.kind, c.trigger_edge, c.dependent_source_id)
                if key not in results or c.depth < results[key].depth:
                    results[key] = c
        else:
            for head in (trigger_from, trigger_to):
                for c in _downstream_consequences(
                    db, owner_id, "contested_ground", trigger_to, (trigger_from, trigger_to, rel), [head], depth, projects
                ):
                    key = (c.kind, c.trigger_edge, c.dependent_source_id)
                    if key not in results or c.depth < results[key].depth:
                        results[key] = c

    # opposing_support: two support edges feeding opposite sides of a contradicts pair.
    # Each unordered pair is reported ONCE (the reversed-side iteration must not duplicate).
    seen_opposing: set[frozenset] = set()
    support_edges = _trigger_edges(db, owner_id, roots, (RelationshipType.supports,))
    contradict_edges = [(f, t) for f, t, r in triggers if r == RelationshipType.contradicts.value]
    support_by_head: dict[uuid.UUID, list[uuid.UUID]] = {}
    supporters: set[uuid.UUID] = set()
    for f, t, r in support_edges:
        support_by_head.setdefault(t, []).append(f)  # edge C supports X: head X, supporter C
        supporters.add(f)
        supporters.add(t)
    # Supporters are usually NOT on the trigger paths — look their projects up too, or
    # every cross-domain pair would be silently dropped as "unplaceable".
    missing = supporters - set(projects)
    projects.update(_project_map(db, owner_id, missing))
    for xf, xt in contradict_edges:
        for side_a, side_b in ((xf, xt), (xt, xf)):
            for supporter_a in support_by_head.get(side_a, []):
                for supporter_b in support_by_head.get(side_b, []):
                    if supporter_a == supporter_b:
                        continue  # one source supporting both sides of a conflict records tension, not an interaction
                    pair = frozenset((supporter_a, supporter_b))
                    if pair in seen_opposing:
                        continue
                    pa = projects.get(supporter_a)
                    pb = projects.get(supporter_b)
                    if pa is None and pb is None:
                        continue
                    if pa == pb:
                        continue  # same-domain opposing support is visible in the Library UI already
                    seen_opposing.add(pair)
                    results.setdefault(
                        ("opposing_support", (supporter_a, supporter_b, RelationshipType.contradicts.value), supporter_a),
                        SecondOrderConsequence(
                            kind="opposing_support",
                            root_source_id=xt,
                            trigger_edge=(xf, xt, RelationshipType.contradicts.value),
                            dependent_source_id=supporter_a,
                            dependent_project_id=pa,
                            depth=1,
                            path=[supporter_a],
                        ),
                    )

    ordered = sorted(results.values(), key=lambda c: (c.kind, c.depth, str(c.root_source_id), str(c.dependent_source_id)))
    return ordered


@dataclass
class CrossDomainClaimOverlap:
    """The same claim text recorded in sources of different projects — the interaction
    discovery that is honest without semantics: identical TEXT, evidenced by both claim
    ids and both project ids. No claim that they "agree in meaning"."""

    claim_text: str
    claim_ids: list[uuid.UUID] = field(default_factory=list)
    source_ids: list[uuid.UUID] = field(default_factory=list)
    project_ids: list[uuid.UUID | None] = field(default_factory=list)


def cross_domain_claim_overlaps(db: Session, owner_id: uuid.UUID, *, limit: int = 100) -> list[CrossDomainClaimOverlap]:
    """Claims whose normalized text (lower-cased, whitespace-collapsed) appears in sources
    of DIFFERENT projects (IS DISTINCT FROM semantics: unfiled counts as its own bucket).
    Owner-scoped, deleted sources excluded, deterministic order, capped for safety."""
    rows = db.execute(
        text(
            "WITH norm AS ("
            "  SELECT c.id::text AS claim_id, c.source_id::text AS source_id, "
            "         lower(btrim(regexp_replace(c.claim_text, '\\s+', ' ', 'g'))) AS norm_text, "
            "         coalesce(d.project_id::text, '~unfiled') AS project_bucket, "
            "         d.project_id::text AS project_id "
            "  FROM knowledge_claims c JOIN documents d ON d.id = c.source_id AND d.uploaded_by = c.owner_id "
            "  WHERE c.owner_id = :owner AND d.deleted_at IS NULL"
            "), grouped AS ("
            "  SELECT norm_text, "
            "         array_agg(claim_id ORDER BY claim_id) AS claim_ids, "
            "         array_agg(source_id ORDER BY source_id) AS source_ids, "
            "         array_agg(DISTINCT project_id ORDER BY project_id) AS project_ids "
            "  FROM norm GROUP BY norm_text"
            ") "
            "SELECT norm_text, claim_ids, source_ids, project_ids FROM grouped "
            "WHERE (SELECT count(DISTINCT n.project_bucket) FROM norm n WHERE n.norm_text = grouped.norm_text) >= 2 "
            "ORDER BY norm_text LIMIT :limit"
        ),
        {"owner": str(owner_id), "limit": max(1, int(limit))},
    ).all()

    overlaps = []
    for norm_text, claim_ids, source_ids, project_ids in rows:
        overlaps.append(
            CrossDomainClaimOverlap(
                claim_text=norm_text,
                claim_ids=[uuid.UUID(c) for c in claim_ids],
                source_ids=[uuid.UUID(s) for s in source_ids],
                project_ids=[uuid.UUID(p) if p else None for p in project_ids],
            )
        )
    return overlaps


# --------------------------------------------------------------------------------------
# Execution-path integration (additive, fail-open): the reduced single-SQL rollup and
# the instruction line the chat/workbench context builders append next to
# build_trust_instructions' source-trust text. Per-claim verdicts above stay
# authoritative; this rollup mirrors the same ladder in one bounded query so a
# per-request call site never needs N per-claim subqueries.
# --------------------------------------------------------------------------------------

_ROLLUP_SQL = """
WITH live AS (
    SELECT c.id, c.status::text AS claim_status, c.confidence::text AS stored_confidence,
           (c.chunk_id IS NOT NULL AND s.deleted_at IS NULL AND s.id IS NOT NULL) AS has_live_grounding,
           s.active_truth_status::text AS source_status,
           em.quality_cap::text AS quality_cap
    FROM knowledge_claims c
    JOIN documents s ON s.id = c.source_id AND s.uploaded_by = c.owner_id
    LEFT JOIN claim_evidence_metadata em ON em.claim_id = c.id AND em.owner_id = c.owner_id
    WHERE c.owner_id = :owner
      {project_filter}
), contradicted AS (
    SELECT DISTINCT r.from_claim_id AS cid FROM claim_relationships r
    WHERE r.owner_id = :owner AND r.relationship_type = 'contradicts'
    UNION
    SELECT DISTINCT r.to_claim_id AS cid FROM claim_relationships r
    WHERE r.owner_id = :owner AND r.relationship_type = 'contradicts'
),
resolved AS (
    -- The verified_fact bucket's conditions WITHOUT the cap clause, computed once: a
    -- claim that would verify but carries a stricter materialized cap lands in the
    -- CAP's bucket instead (classify_claim's exact downgrade rule — cap applies only
    -- to a would-be verified_fact, never touches conflicting_evidence/assumption, and
    -- a cap can only lower, never raise). cap values come from the grader's closed
    -- vocabulary stored at stamp time (app/rag/evidence_metadata.py): 'verified_fact',
    -- 'hypothesis' or 'unknown'.
    SELECT l.*, (
        l.has_live_grounding
        AND l.claim_status = 'active' AND l.source_status IS DISTINCT FROM 'superseded'
        AND l.stored_confidence IN ('certain', 'likely')
    ) AS would_verify
    FROM live l
)
SELECT
    count(*) FILTER (WHERE c.cid IS NOT NULL OR l.claim_status = 'disputed')  AS conflicting_evidence,
    count(*) FILTER (WHERE c.cid IS NULL AND l.claim_status <> 'disputed' AND l.would_verify
                      AND (l.quality_cap IS NULL OR l.quality_cap = 'verified_fact'))
                                                                              AS verified_fact,
    count(*) FILTER (WHERE c.cid IS NULL AND l.claim_status <> 'disputed'
                      AND l.stored_confidence = 'no_basis')                  AS assumption,
    count(*) FILTER (WHERE
                        (c.cid IS NULL AND l.claim_status <> 'disputed' AND NOT l.has_live_grounding
                         AND l.stored_confidence <> 'no_basis')
                      OR (c.cid IS NULL AND l.claim_status <> 'disputed' AND l.would_verify
                          AND l.quality_cap = 'unknown'))                    AS unknown,
    count(*) FILTER (WHERE
                        (c.cid IS NULL AND l.claim_status <> 'disputed' AND l.stored_confidence <> 'no_basis'
                         AND l.has_live_grounding
                         AND NOT (l.claim_status = 'active' AND l.source_status IS DISTINCT FROM 'superseded'
                                  AND l.stored_confidence IN ('certain', 'likely')))
                      OR (c.cid IS NULL AND l.claim_status <> 'disputed' AND l.would_verify
                          AND l.quality_cap = 'hypothesis'))                 AS hypothesis,
    count(*)                                                                 AS total
FROM resolved l
LEFT JOIN contradicted c ON c.cid = l.id
"""


def epistemic_status_counts_sql(
    db: Session,
    owner_id: uuid.UUID,
    *,
    project_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """One-query reduced rollup of the ladder for the owner (optionally one project).
    Mirrors classify_claim's mapping in SQL: contradicted-or-disputed wins first, then
    no_basis → assumption, then grounded-active-source-with-confident-bucket →
    verified_fact, everything else → hypothesis. The stored confidence column is the
    extraction-time bucket (assess_claim_confidence recomputes LIVE per claim); the rollup
    therefore reads 'certain|likely' as the stored signal that no contradiction has been
    recorded since — the authoritative per-claim verdicts remain classify_claim's.

    Also mirrors classify_claim's quality-cap downgrade: a would-be verified_fact whose
    stamped study metadata caps the ladder lower (claim_evidence_metadata.quality_cap —
    materialized by app/rag/evidence_metadata.py at stamp time) is counted in the CAP's
    bucket instead, so the chat/workbench instruction rollup agrees with the per-claim
    detail view that passes quality_cap into classify_claim. The cap never moves a claim
    INTO verified_fact and never touches conflicting_evidence/assumption, exactly like
    classify_claim's rule."""
    sql = _ROLLUP_SQL.format(
        project_filter="AND s.project_id = :project_id" if project_id is not None else ""
    )
    params: dict = {"owner": str(owner_id)}
    if project_id is not None:
        params["project_id"] = str(project_id)
    row = db.execute(text(sql), params).first()
    if row is None:
        return {s.value: 0 for s in EpistemicStatus} | {"total": 0}
    return {
        "conflicting_evidence": int(row[0]),
        "verified_fact": int(row[1]),
        "assumption": int(row[2]),
        "unknown": int(row[3]),
        "hypothesis": int(row[4]),
        "total": int(row[5]),
    }


def format_epistemic_instructions(counts: dict[str, int]) -> str:
    """The prompt-building layer's instruction line (Swedish, matching
    build_trust_instructions' language): what the evidence ladder currently holds and how
    to treat non-verified buckets. Deterministic; never names claim content."""
    if not counts or counts.get("total", 0) == 0:
        return "EPISTEMISK STATUS: inga lagrade påståenden att grading — verka utan påståenden om ej bevisade fakta."
    return (
        f"EPISTEMISK STATUS ({counts.get('total', 0)} påståenden): "
        f"{counts.get('verified_fact', 0)} verifierade, "
        f"{counts.get('conflicting_evidence', 0)} motstridiga, "
        f"{counts.get('hypothesis', 0)} hypoteser, "
        f"{counts.get('assumption', 0)} antaganden, "
        f"{counts.get('unknown', 0)} okända. "
        "Behandla hypoteser och antaganden som EJ bevisade fakta; presentera motstridiga "
        "påståenden som en konflikt, aldrig som en sanning."
    )
