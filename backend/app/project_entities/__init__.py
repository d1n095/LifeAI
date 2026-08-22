"""Deterministic recording/query API for Life's project entities / interpretation queue --
the boundary between a live signal producer (P3 claim extraction) and structured project
understanding (`project_entities`). See `service.py`'s own module docstring for the full
doctrine."""

from app.project_entities.service import (
    ProjectEntityError,
    dismiss_interpretation_proposal,
    get_interpretation_proposal,
    get_project_entity,
    list_current_project_entities,
    list_entity_relationships,
    list_interpretation_proposals,
    list_unreviewed_interpretation_proposals,
    mark_project_entity_superseded,
    promote_interpretation_proposal,
    record_entity_relationship,
    record_interpretation_proposal,
)

__all__ = [
    "ProjectEntityError",
    "dismiss_interpretation_proposal",
    "get_interpretation_proposal",
    "get_project_entity",
    "list_current_project_entities",
    "list_entity_relationships",
    "list_interpretation_proposals",
    "list_unreviewed_interpretation_proposals",
    "mark_project_entity_superseded",
    "promote_interpretation_proposal",
    "record_entity_relationship",
    "record_interpretation_proposal",
]
