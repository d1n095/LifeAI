"""Pydantic schemas for the inspectable-memory founder API.

Kept local to this package (not in app/schemas.py) to avoid colliding with Claude PR #197's
shared schemas surface.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class InspectableMemoryItemOut(BaseModel):
    id: uuid.UUID
    kind: str
    raw_statement: str | None = None
    normalized_interpretation: str
    related_entities: list[uuid.UUID] = Field(default_factory=list)
    confidence: float | None = None
    factual_status: str
    truth_state: str
    plan_references: list[uuid.UUID] = Field(default_factory=list)
    task_references: list[uuid.UUID] = Field(default_factory=list)
    dependencies: list[uuid.UUID] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    provenance: dict = Field(default_factory=dict)
    created_at: datetime | None = None
    superseded_by: uuid.UUID | None = None
    corrections: list[uuid.UUID] = Field(default_factory=list)
    implementation_status: str | None = None
    verification_status: str | None = None


class MemoryTruthClaimOut(BaseModel):
    id: uuid.UUID
    claim_text: str
    claimed_state: str
    target_kind: str
    target_id: uuid.UUID | None = None
    verified_at: datetime | None = None
    verified_result: bool | None = None
    provenance: dict = Field(default_factory=dict)
    created_at: datetime
    idempotency_key: str


class FounderAddMemoryIn(BaseModel):
    content: str
    note_type: str = "observation"
    idempotency_key: str
    source: str | None = None
    provenance: dict = Field(default_factory=dict)


class FounderCorrectMemoryIn(BaseModel):
    content: str
    idempotency_key: str
    note_type: str | None = None


class FounderDisputeMemoryIn(BaseModel):
    kind: str
    reason: str | None = None


class RecordTruthClaimIn(BaseModel):
    claim_text: str
    claimed_state: str
    target_kind: str
    idempotency_key: str
    target_id: uuid.UUID | None = None
    provenance: dict = Field(default_factory=dict)
    verify_now: bool = True
