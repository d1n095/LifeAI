"""Independent provider-call counter — MainAI report alone is insufficient."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session


@dataclass
class ProviderAttemptSnapshot:
    spend_reservations: int
    workforce_provider_receipts: int
    notes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "spend_reservations": self.spend_reservations,
            "workforce_provider_receipts": self.workforce_provider_receipts,
            "notes": self.notes,
        }


def snapshot_provider_attempts(db: Session, *, owner_id: UUID | None = None) -> ProviderAttemptSnapshot:
    """Best-effort cross-check against spend + assignment receipts.

    Does not invent a second provider system. Missing tables → count 0 with note.
    """
    notes: list[str] = []
    spend = 0
    receipts = 0
    try:
        q = text("SELECT count(*) FROM provider_spend_reservations")
        if owner_id is not None:
            # table may or may not be owner-scoped depending on migration
            try:
                spend = int(
                    db.execute(
                        text(
                            "SELECT count(*) FROM provider_spend_reservations WHERE owner_id = :oid"
                        ),
                        {"oid": str(owner_id)},
                    ).scalar()
                    or 0
                )
            except Exception:
                spend = int(db.execute(q).scalar() or 0)
                notes.append("spend_reservations_not_owner_filtered")
        else:
            spend = int(db.execute(q).scalar() or 0)
    except Exception as exc:
        notes.append(f"spend_unavailable:{type(exc).__name__}")

    try:
        # Assignments that claim provider was invoked
        stmt = text(
            """
            SELECT count(*) FROM workforce_assignments
            WHERE coalesce((result_payload->>'provider_invoked')::boolean, false) = true
            """
            + (" AND owner_id = :oid" if owner_id else "")
        )
        params = {"oid": str(owner_id)} if owner_id else {}
        receipts = int(db.execute(stmt, params).scalar() or 0)
    except Exception as exc:
        notes.append(f"assignment_receipts_unavailable:{type(exc).__name__}")

    return ProviderAttemptSnapshot(
        spend_reservations=spend,
        workforce_provider_receipts=receipts,
        notes=notes,
    )


def assert_provider_attempts_unchanged(
    before: ProviderAttemptSnapshot, after: ProviderAttemptSnapshot
) -> dict[str, Any]:
    ok = (
        before.spend_reservations == after.spend_reservations
        and before.workforce_provider_receipts == after.workforce_provider_receipts
    )
    return {
        "unchanged": ok,
        "before": before.as_dict(),
        "after": after.as_dict(),
        "mainai_report_alone_insufficient": True,
    }
