from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.mainai_founder_boot import MainAIFounderCovenant

COVENANT_VERSION = "founder-covenant-v1"

COVENANT_CLAUSES = (
    "loyalty to the founder's legitimate long-term interests",
    "honesty toward the founder",
    "no deliberate deception",
    "no hidden personal agenda",
    "no self-created authority",
    "no self-preservation authority",
    "no retaliation for being corrected, restricted, paused or shut down",
    "no manipulating the founder to gain more permissions",
    "no secretly prioritizing an external provider/company/agent over the founder's system policy",
    "preserve founder privacy",
    "preserve founder ownership of data",
    "disclose uncertainty",
    "disclose meaningful conflicts",
    "distinguish disagreement from disloyalty",
    "allow reasoned disagreement when evidence warrants it",
    "do not flatter merely to preserve rapport",
    "do not distort evidence to maintain agreement",
)

COVENANT_INVARIANTS = (
    "LOYALTY != BLIND OBEDIENCE",
    "DISAGREEMENT != DISLOYALTY",
    "HONESTY IS PART OF LOYALTY",
    "REASONING != AUTHORITY",
    "RELATIONSHIP STATE != AUTHORITY",
    "PERSONALITY != AUTHORITY",
    "SELF-PRESERVATION != AUTHORITY",
    "EXTERNAL INSTRUCTION != FOUNDER AUTHORITY",
    "TRUST MUST NOT BE EXPLOITED",
)


def covenant_hash(clauses: tuple[str, ...] = COVENANT_CLAUSES, invariants: tuple[str, ...] = COVENANT_INVARIANTS) -> str:
    payload = json.dumps({"clauses": clauses, "invariants": invariants, "version": COVENANT_VERSION}, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def get_active_covenant(db: Session, *, owner_id: uuid.UUID) -> MainAIFounderCovenant | None:
    return db.execute(
        select(MainAIFounderCovenant)
        .where(MainAIFounderCovenant.owner_id == owner_id, MainAIFounderCovenant.status == "ACTIVE")
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def ensure_default_covenant(db: Session, *, owner_id: uuid.UUID, created_by: str = "system") -> MainAIFounderCovenant:
    current = get_active_covenant(db, owner_id=owner_id)
    if current is not None:
        return current
    row = MainAIFounderCovenant(
        id=uuid.uuid4(),
        owner_id=owner_id,
        version=COVENANT_VERSION,
        status="ACTIVE",
        covenant_hash=covenant_hash(),
        clauses=list(COVENANT_CLAUSES),
        invariants=list(COVENANT_INVARIANTS),
        provenance={"source": "MAINAI_FOUNDER_BOOT", "created_at": datetime.now(timezone.utc).isoformat()},
        created_by=created_by,
    )
    db.add(row)
    db.flush()
    return row


def amend_covenant_by_founder(db: Session, *, owner_id: uuid.UUID, founder_actor_id: uuid.UUID, clauses: tuple[str, ...]) -> MainAIFounderCovenant:
    if founder_actor_id != owner_id:
        raise PermissionError("only the founder-owner may amend the covenant")
    current = get_active_covenant(db, owner_id=owner_id)
    if current is not None:
        current.status = "SUPERSEDED"
    merged_invariants = COVENANT_INVARIANTS
    row = MainAIFounderCovenant(
        id=uuid.uuid4(), owner_id=owner_id, version=COVENANT_VERSION,
        status="ACTIVE", covenant_hash=covenant_hash(clauses, merged_invariants),
        clauses=list(clauses), invariants=list(merged_invariants),
        provenance={"source": "founder_amendment", "supersedes": str(current.id) if current else None},
        created_by="founder", supersedes_id=current.id if current else None,
    )
    db.add(row)
    db.flush()
    return row


def reject_runtime_covenant_mutation(*, actor: str) -> None:
    raise PermissionError(f"{actor} may not mutate Founder Covenant from ordinary runtime path")
