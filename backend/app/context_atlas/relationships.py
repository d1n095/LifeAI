"""Conceptual relationship vocabulary mapped onto the EXISTING schema (Workstream C).

`app/models/source_relationship.py` already defines a closed enum: derived_from, supersedes,
contradicts, supports, duplicates, belongs_to. The program's eleven conceptual relationship
names are mapped here instead of adding a parallel edge table or a new enum value. Where no
existing value fits, the mapping says so explicitly (existing_type=None) and the proposal is
never recordable — the gap stays visible instead of being papered over with the nearest-
sounding value.

No function here writes to the database, and none grants execution authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EvidenceKind(str, Enum):
    exact_byte_identity = "exact_byte_identity"
    recorded_by_founder = "recorded_by_founder"
    derived_artifact = "derived_artifact"
    ai_similarity = "ai_similarity"
    unknown = "unknown"


@dataclass(frozen=True)
class ConceptMapping:
    concept: str
    existing_type: str | None
    note: str


CONCEPT_MAPPINGS: dict[str, ConceptMapping] = {
    "BELONGS_TO_PROJECT": ConceptMapping(
        "BELONGS_TO_PROJECT",
        "belongs_to",
        "Documents/workspaces already carry project_id; the edge is for cross-source ownership.",
    ),
    "DERIVED_FROM": ConceptMapping(
        "DERIVED_FROM",
        "derived_from",
        "A generated artifact (brief, summary, claim) traced back to its source.",
    ),
    "DUPLICATES": ConceptMapping(
        "DUPLICATES",
        "duplicates",
        "Recordable only from exact byte identity, never from a similarity score.",
    ),
    "SUPERSEDES": ConceptMapping(
        "SUPERSEDES",
        "supersedes",
        "Document replacement; app/rag/trust.py already consumes this edge.",
    ),
    "CONTRADICTS": ConceptMapping(
        "CONTRADICTS",
        "contradicts",
        "Must be recorded from explicit content/decision evidence, not inference.",
    ),
    "SUPPORTS": ConceptMapping(
        "SUPPORTS",
        "supports",
        "Existing enum value kept for completeness; not part of the eleven concepts.",
    ),
    "SIMILAR_TO": ConceptMapping(
        "SIMILAR_TO",
        None,
        "No SIMILAR_TO value exists. Recording `duplicates` from similarity would be wrong; "
        "keep as an unrecorded candidate until stronger evidence exists.",
    ),
    "VERSION_OF": ConceptMapping(
        "VERSION_OF",
        None,
        "Modeled by KnowledgeVersion (source_id, version_number), not by SourceRelationship.",
    ),
    "DEPENDS_ON": ConceptMapping(
        "DEPENDS_ON",
        None,
        "No source-level dependency edge exists; task_dependencies covers tasks only.",
    ),
    "IMPLEMENTS_REQUIREMENT": ConceptMapping(
        "IMPLEMENTS_REQUIREMENT",
        None,
        "No requirement entity is linked from sources yet; needs an owner decision.",
    ),
    "VERIFIED_BY": ConceptMapping(
        "VERIFIED_BY",
        None,
        "Verification evidence lives in tests/evidence artifacts, not in the source graph.",
    ),
    "PROPOSED_FOR": ConceptMapping(
        "PROPOSED_FOR",
        None,
        "Proposal/assignment staging has its own approval-gated tables; not an edge here.",
    ),
}

# Evidence kinds that may justify writing an edge without founder review. AI similarity is
# deliberately absent: it is a lead, not proof.
_STRUCTURAL_EVIDENCE = {
    EvidenceKind.recorded_by_founder,
    EvidenceKind.derived_artifact,
}


@dataclass(frozen=True)
class RelationshipProposal:
    concept: str
    existing_type: str | None
    recordable: bool
    requires_review: bool
    reason: str
    execution_authorized: bool = False


def propose_relationship(
    concept: str,
    *,
    evidence: EvidenceKind = EvidenceKind.unknown,
) -> RelationshipProposal:
    """Maps one conceptual relationship to the existing schema, fail-closed.

    Unknown concepts are unresolved (no nearest-match guessing). A known concept with no
    existing type is never recordable. `DUPLICATES` requires exact byte identity; every other
    mapped concept requires structural evidence. `ai_similarity` alone never records.
    """
    mapping = CONCEPT_MAPPINGS.get(concept.strip().upper())
    if mapping is None:
        return RelationshipProposal(
            concept=concept,
            existing_type=None,
            recordable=False,
            requires_review=True,
            reason="unknown relationship concept; left unresolved rather than guessed",
        )

    if mapping.existing_type is None:
        return RelationshipProposal(
            concept=mapping.concept,
            existing_type=None,
            recordable=False,
            requires_review=True,
            reason=mapping.note,
        )

    if mapping.concept == "DUPLICATES":
        recordable = evidence is EvidenceKind.exact_byte_identity
        reason = (
            "exact byte identity recorded as duplicates"
            if recordable
            else "duplicates requires exact byte identity; "
            f"{evidence.value} is not sufficient"
        )
    else:
        recordable = evidence in _STRUCTURAL_EVIDENCE
        reason = (
            f"{mapping.concept} supported by {evidence.value}"
            if recordable
            else f"{mapping.concept} requires structural evidence; "
            f"{evidence.value} is not sufficient"
        )

    return RelationshipProposal(
        concept=mapping.concept,
        existing_type=mapping.existing_type,
        recordable=recordable,
        requires_review=not recordable,
        reason=reason,
    )


def unmapped_concepts() -> tuple[str, ...]:
    """Concepts the existing schema cannot represent — surfaced, not hidden."""
    return tuple(
        name for name, mapping in CONCEPT_MAPPINGS.items() if mapping.existing_type is None
    )
