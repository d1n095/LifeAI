"""Stage O — memory repack / health loop (safe checks; no canonical rewrite)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.continuous_simplification import SimplificationKind, propose_simplifications
from app.contradiction_engine import list_claims
from app.models.project_entities import ProjectEntity, ProjectEntityRelationship
from app.models.work_candidate import WorkCandidate


@dataclass
class HealthFinding:
    code: str
    severity: str  # info|warn|error
    title: str
    evidence_refs: list[dict] = field(default_factory=list)
    safe_repack_hint: str = ""
    changes_canonical_meaning: bool = False  # always False for Stage O actions


@dataclass
class HealthReport:
    owner_id: uuid.UUID
    findings: list[HealthFinding] = field(default_factory=list)

    @property
    def ok_to_repack(self) -> bool:
        return all(not f.changes_canonical_meaning for f in self.findings)


def run_memory_health_checks(db: Session, *, owner_id: uuid.UUID) -> HealthReport:
    findings: list[HealthFinding] = []

    # Reuse Stage F detectors
    simp = propose_simplifications(db, owner_id=owner_id)
    for p in simp.proposals:
        findings.append(
            HealthFinding(
                code=p.kind.value,
                severity="warn",
                title=p.title,
                evidence_refs=p.evidence_refs,
                safe_repack_hint="propose_only",
            )
        )

    # Broken relations: edges pointing at missing/non-owned entities
    edges = db.execute(
        select(ProjectEntityRelationship).where(ProjectEntityRelationship.owner_id == owner_id)
    ).scalars().all()
    entity_ids = {
        e.id
        for e in db.execute(select(ProjectEntity).where(ProjectEntity.owner_id == owner_id)).scalars().all()
    }
    for edge in edges:
        if edge.from_entity_id not in entity_ids or edge.to_entity_id not in entity_ids:
            findings.append(
                HealthFinding(
                    code="broken_relation",
                    severity="error",
                    title=f"Broken entity relationship {edge.id}",
                    evidence_refs=[{"kind": "project_entity_relationship", "id": str(edge.id)}],
                    safe_repack_hint="quarantine_edge_row_only",
                )
            )

    # Dead plan references on unreviewed candidates with empty/missing provenance goal
    for cand in db.execute(
        select(WorkCandidate).where(WorkCandidate.owner_id == owner_id, WorkCandidate.status == "unreviewed")
    ).scalars().all():
        if (cand.title or "").startswith("[memory]") and not (cand.provenance or {}).get("memory_note_id"):
            findings.append(
                HealthFinding(
                    code="dead_plan_reference",
                    severity="warn",
                    title=f"Memory-parked candidate missing note provenance: {cand.id}",
                    evidence_refs=[{"kind": "work_candidate", "id": str(cand.id)}],
                    safe_repack_hint="attach_missing_provenance_if_recoverable",
                )
            )

    # Old invalidated assumptions still listed as active dependents noise
    for claim in list_claims(db, owner_id=owner_id, status="invalidated"):
        if claim.dependent_refs:
            findings.append(
                HealthFinding(
                    code="old_assumption",
                    severity="info",
                    title=f"Invalidated assumption still has dependents: {claim.id}",
                    evidence_refs=[{"kind": "structured_claim", "id": str(claim.id)}],
                    safe_repack_hint="revalidate_dependents_without_deleting_claim",
                )
            )

    return HealthReport(owner_id=owner_id, findings=findings)
