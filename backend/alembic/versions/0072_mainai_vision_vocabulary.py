"""Widen project_entities/project_entity_relationships vocabulary for MainAI Cognitive Control
Plane (Vision Compiler). Additive only -- one small migration, same pattern as migration 0065
(work_candidates.priority) / 0069 (intelligence_ideas.disposition): no new table, no change to
RLS, supersession, or any existing row. See
docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the architecture decision.
"""
from alembic import op

revision = "0072_mainai_vision_vocabulary"
down_revision = "0071"
branch_labels = None
depends_on = None

_NEW_ENTITY_TYPES = (
    "domain", "capability", "system", "subsystem", "requirement", "implied_requirement",
    "invariant", "risk", "acceptance_criterion", "verification_criterion",
)
_OLD_ENTITY_TYPES = ("idea", "decision", "task_reference", "vision_statement", "open_question")

_NEW_RELATIONSHIP_TYPES = ("depends_on", "implies", "verifies", "satisfies", "mitigates")
_OLD_RELATIONSHIP_TYPES = ("relates_to", "supersedes", "contradicts", "blocks", "answers", "duplicates", "derived_from")


def upgrade() -> None:
    all_entity_types = _OLD_ENTITY_TYPES + _NEW_ENTITY_TYPES
    all_relationship_types = _OLD_RELATIONSHIP_TYPES + _NEW_RELATIONSHIP_TYPES
    op.execute("ALTER TABLE project_entities DROP CONSTRAINT ck_project_entities_entity_type")
    op.execute(
        "ALTER TABLE project_entities ADD CONSTRAINT ck_project_entities_entity_type "
        f"CHECK (entity_type IN ({','.join(repr(v) for v in all_entity_types)}))"
    )
    op.execute("ALTER TABLE project_entity_relationships DROP CONSTRAINT ck_project_entity_relationships_type")
    op.execute(
        "ALTER TABLE project_entity_relationships ADD CONSTRAINT ck_project_entity_relationships_type "
        f"CHECK (relationship_type IN ({','.join(repr(v) for v in all_relationship_types)}))"
    )
    # interpretation_proposals.proposed_entity_type carries its own, separate CHECK (migration
    # 0054) with the SAME original vocabulary -- a proposal must be able to name any entity_type
    # its own promotion could later create, so it needs the identical widening.
    op.execute("ALTER TABLE interpretation_proposals DROP CONSTRAINT ck_interpretation_proposals_entity_type")
    op.execute(
        "ALTER TABLE interpretation_proposals ADD CONSTRAINT ck_interpretation_proposals_entity_type "
        f"CHECK (proposed_entity_type IN ({','.join(repr(v) for v in all_entity_types)}))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE interpretation_proposals DROP CONSTRAINT ck_interpretation_proposals_entity_type")
    op.execute(
        "ALTER TABLE interpretation_proposals ADD CONSTRAINT ck_interpretation_proposals_entity_type "
        f"CHECK (proposed_entity_type IN ({','.join(repr(v) for v in _OLD_ENTITY_TYPES)}))"
    )
    op.execute("ALTER TABLE project_entity_relationships DROP CONSTRAINT ck_project_entity_relationships_type")
    op.execute(
        "ALTER TABLE project_entity_relationships ADD CONSTRAINT ck_project_entity_relationships_type "
        f"CHECK (relationship_type IN ({','.join(repr(v) for v in _OLD_RELATIONSHIP_TYPES)}))"
    )
    op.execute("ALTER TABLE project_entities DROP CONSTRAINT ck_project_entities_entity_type")
    op.execute(
        "ALTER TABLE project_entities ADD CONSTRAINT ck_project_entities_entity_type "
        f"CHECK (entity_type IN ({','.join(repr(v) for v in _OLD_ENTITY_TYPES)}))"
    )
