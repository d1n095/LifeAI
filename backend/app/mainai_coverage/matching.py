"""Layered Comparison Strategy. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md §B.

Prefer deterministic/local capability over a single fuzzy score: EXACT_IDENTITY ->
NORMALIZED_LEXICAL -> ALIAS -> STRUCTURED_LINK -> GRAPH_RELATIONSHIP -> KEYWORD_OVERLAP ->
SEMANTIC_SIMILARITY. `layered_match()` always reports WHICH layer produced its verdict, so a
caller never mistakes a bare keyword-overlap hit for semantic understanding.

MENTIONED == IMPLEMENTED, SIMILAR WORDING == SAME REQUIREMENT, NO KEYWORD MATCH == NO
RELATIONSHIP, and OLD REQUIREMENT == CURRENT REQUIREMENT are each exactly the false
equivalence this module's layering is built to avoid: a `STRUCTURED_LINK`/`GRAPH_RELATIONSHIP`
hit can find a real relationship where keywords share nothing, and `superseded=True` on a
`NormalizedObservation` (see `types.py`) keeps a supersede-aware caller from treating a stale
match as current.

SEMANTIC_SIMILARITY is NEVER available on this branch -- no local/bounded semantic-embedding
implementation exists in this codebase, and this module does not introduce an external AI/API
dependency to fake one. `semantic_similarity_available()` reports this honestly; a caller that
needs real semantic matching must supply and wire in an actual local model, at which point THIS
layer -- not a silent KEYWORD_OVERLAP substitution -- is where it plugs in."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.mainai_coverage.types import MatchLayer

# A small, bounded, hand-seeded alias table of known synonymous program/concept names this
# project's own history has actually used -- NOT an exhaustive thesaurus. Extensible: a caller
# passes its own `alias_groups` to `layered_match()` to add more without editing this module.
DEFAULT_ALIAS_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"situational awareness", "agent runtime view", "runtime snapshot"}),
    frozenset({"capability mastery", "autonomy stage", "external dependence reduction"}),
    frozenset({"coverage audit", "omission discovery", "gap analysis"}),
    frozenset({"vision compiler", "cognitive control plane", "canonical vision"}),
)

CANONICAL_MATCH_THRESHOLD = 0.3
_WORD_RE = re.compile(r"[a-zA-Z0-9]+")


def normalize_lexical(text: str) -> str:
    words = _WORD_RE.findall(text.lower())
    return " ".join(words)


def _keywords(text: str) -> set[str]:
    """Keeps every token longer than 2 characters, PLUS any shorter token containing a digit
    (e.g. "q1", "v2") -- a short numeric/version-like token is often the ONE thing that
    distinguishes two otherwise similarly-worded claims (a real bug this project's own tests
    caught: "revenue grew 10% in Q1" vs "...40% in Q3" collapsed to identical keyword sets
    once "10"/"40"/"q1"/"q3" were dropped by a bare length filter)."""

    return {w for w in _WORD_RE.findall(text.lower()) if len(w) > 2 or any(c.isdigit() for c in w)}


def keyword_overlap_score(a_text: str, b_text: str) -> float:
    a_kw, b_kw = _keywords(a_text), _keywords(b_text)
    if not a_kw or not b_kw:
        return 0.0
    return len(a_kw & b_kw) / len(a_kw | b_kw)


def _alias_match(a_text: str, b_text: str, alias_groups: tuple[frozenset[str], ...]) -> bool:
    a_norm, b_norm = normalize_lexical(a_text), normalize_lexical(b_text)
    for group in alias_groups:
        a_hit = any(alias in a_norm for alias in group)
        b_hit = any(alias in b_norm for alias in group)
        if a_hit and b_hit:
            return True
    return False


def semantic_similarity_available() -> bool:
    """Always False on this branch -- see this module's own docstring. A real caller-supplied
    local model would flip the check inside `layered_match()`, not this constant alone (kept as
    a named function, not a bare bool, so a future implementation has one obvious place to
    change the behavior AND the disclosure together)."""

    return False


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    layer: MatchLayer | None
    score: float
    semantic_similarity_available: bool = False
    detail: str = ""


def layered_match(
    a_text: str,
    b_text: str,
    *,
    a_entity_ids: frozenset[str] = frozenset(),
    b_entity_ids: frozenset[str] = frozenset(),
    connected_in_graph: bool = False,
    alias_groups: tuple[frozenset[str], ...] = DEFAULT_ALIAS_GROUPS,
    keyword_threshold: float = CANONICAL_MATCH_THRESHOLD,
    include_keyword_overlap: bool = True,
) -> MatchResult:
    """First layer that fires wins -- deterministic, ordered, never blended into one fuzzy
    score. `a_entity_ids`/`b_entity_ids` being non-empty and sharing a member is
    STRUCTURED_LINK (e.g. both claims trace back to the same `KnowledgeClaim`/`VisionNode`
    entity id); `connected_in_graph=True` is the caller's own real
    `compatibility_graph`/`investigation_graph`-style adjacency check, passed in already
    computed (this function does not itself own a graph)."""

    if a_text and a_text == b_text:
        return MatchResult(True, MatchLayer.EXACT_IDENTITY, 1.0, detail="byte-for-byte identical text")

    a_norm, b_norm = normalize_lexical(a_text), normalize_lexical(b_text)
    if a_norm and a_norm == b_norm:
        return MatchResult(True, MatchLayer.NORMALIZED_LEXICAL, 1.0, detail="identical after case/punctuation normalization")

    if _alias_match(a_text, b_text, alias_groups):
        return MatchResult(True, MatchLayer.ALIAS, 0.9, detail="matched via known alias group")

    if a_entity_ids and b_entity_ids and (a_entity_ids & b_entity_ids):
        return MatchResult(True, MatchLayer.STRUCTURED_LINK, 0.85, detail=f"shared entity id(s): {sorted(a_entity_ids & b_entity_ids)}")

    if connected_in_graph:
        return MatchResult(True, MatchLayer.GRAPH_RELATIONSHIP, 0.75, detail="connected in caller-supplied relationship graph")

    score = keyword_overlap_score(a_text, b_text)
    if include_keyword_overlap and score >= keyword_threshold:
        return MatchResult(True, MatchLayer.KEYWORD_OVERLAP, score, detail="keyword overlap only -- NOT semantic understanding")

    if semantic_similarity_available():  # pragma: no cover -- always False on this branch, see docstring
        raise NotImplementedError("semantic similarity layer is not implemented on this branch")

    return MatchResult(False, None, score, semantic_similarity_available=False, detail="no layer matched; semantic similarity unavailable on this branch")
