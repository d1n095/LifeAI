"""Fixed, read-only database observations; no arbitrary SQL or mutation surface."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.claim_action_integrity.adapters.base import MalformedProviderResponse, ingest_observation, provider_failure
from app.claim_action_integrity.adapters.github import _bindings
from app.claim_action_integrity.adapters.types import EvidenceContext, FactMutability, ProviderObservation
from app.models.claim_action_integrity import ClaimActionEvidence, ClaimActionReceipt


class DatabaseEvidenceAdapter:
    def observe_migration_head(self, db: Session, context: EvidenceContext, *, expected_head: str | None = None):
        try:
            revisions = tuple(db.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num")).scalars())
            if len(revisions) != 1:
                raise MalformedProviderResponse("database does not have exactly one migration head")
            revision = revisions[0]
            facts = {"migration_head": revision, "matches_expected": expected_head is not None and revision == expected_head}
            observation = ProviderObservation("database", "database:alembic_version", f"database-snapshot:{uuid.uuid4()}", FactMutability.mutable_snapshot, _bindings(context, database_revision=revision), facts, datetime.now(timezone.utc), 60)
            return ingest_observation(db, context, observation)
        except Exception as exc:
            return provider_failure(exc)

    def observe_integrity_record(self, db: Session, context: EvidenceContext, *, record_kind: str, record_id: uuid.UUID):
        try:
            model = {"evidence": ClaimActionEvidence, "receipt": ClaimActionReceipt}.get(record_kind)
            if model is None:
                raise ValueError("unsupported record kind")
            id_column = inspect(model).primary_key[0]
            row = db.execute(select(model).where(id_column == record_id, model.owner_id == context.owner_id)).scalar_one_or_none()
            facts = {"row_exists": row is not None, "record_kind": record_kind, "record_id": str(record_id)}
            if isinstance(row, ClaimActionReceipt):
                facts.update(
                    effective_state=row.effective_state,
                    action_state=row.action_state,
                    verification_state=row.verification_state,
                )
            observation = ProviderObservation("database", f"database:{record_kind}:{record_id}", f"database-snapshot:{uuid.uuid4()}", FactMutability.mutable_snapshot, _bindings(context, record_kind=record_kind, record_id=str(record_id)), facts, datetime.now(timezone.utc), 60)
            return ingest_observation(db, context, observation)
        except Exception as exc:
            return provider_failure(exc)
