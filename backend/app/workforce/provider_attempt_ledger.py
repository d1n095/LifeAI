"""Independent provider-call counter — MainAI report alone is insufficient."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
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


def _safe_scalar(db: Session, sql: str, params: dict[str, Any] | None = None) -> tuple[int | None, str | None]:
    """Run count query in a SAVEPOINT so missing tables never poison the outer txn."""
    try:
        with db.begin_nested():
            val = db.execute(text(sql), params or {}).scalar()
            return int(val or 0), None
    except Exception as exc:  # noqa: BLE001 — best-effort ledger
        return None, f"{type(exc).__name__}"


def snapshot_provider_attempts(db: Session, *, owner_id: UUID | None = None) -> ProviderAttemptSnapshot:
    """Best-effort cross-check against spend + assignment receipts.

    Does not invent a second provider system. Missing tables → count 0 with note.
    Uses SAVEPOINTs so probe failures cannot abort the caller's transaction.
    """
    notes: list[str] = []
    spend = 0
    receipts = 0

    if owner_id is not None:
        n, err = _safe_scalar(
            db,
            "SELECT count(*) FROM provider_spend_reservations WHERE owner_id = :oid",
            {"oid": str(owner_id)},
        )
        if err:
            n2, err2 = _safe_scalar(db, "SELECT count(*) FROM provider_spend_reservations")
            if err2:
                notes.append(f"spend_unavailable:{err2}")
            else:
                spend = int(n2 or 0)
                notes.append("spend_reservations_not_owner_filtered")
        else:
            spend = int(n or 0)
    else:
        n, err = _safe_scalar(db, "SELECT count(*) FROM provider_spend_reservations")
        if err:
            notes.append(f"spend_unavailable:{err}")
        else:
            spend = int(n or 0)

    if owner_id is not None:
        n, err = _safe_scalar(
            db,
            """
            SELECT count(*) FROM workforce_assignments
            WHERE coalesce((result_payload->>'provider_invoked')::boolean, false) = true
              AND owner_id = :oid
            """,
            {"oid": str(owner_id)},
        )
    else:
        n, err = _safe_scalar(
            db,
            """
            SELECT count(*) FROM workforce_assignments
            WHERE coalesce((result_payload->>'provider_invoked')::boolean, false) = true
            """,
        )
    if err:
        notes.append(f"assignment_receipts_unavailable:{err}")
    else:
        receipts = int(n or 0)

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
