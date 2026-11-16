"""MainAI V2 -- Privacy Boundary Engine: local, in-memory receipt log.

Foundation-stage implementation: an append-only in-process log, not yet backed by a durable
table. Production wiring would record these the same way app.egress_policy's
ProviderDisclosureEvent does (a real DB row per decision, flushed in the caller's own
transaction) -- deliberately not done here since this package is not wired into any runtime
path yet (see module docstrings across this package). The in-memory log still enforces the
real invariant this stage cares about: a receipt is immutable once recorded (no update/delete
method exists) and never stores the raw content that was removed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.privacy_boundary.types import DataClassification, OutboundPurpose, PrivacyReceipt


@dataclass
class ReceiptLog:
    _receipts: list[PrivacyReceipt] = field(default_factory=list)

    @staticmethod
    def default() -> "ReceiptLog":
        return ReceiptLog()

    def record(
        self,
        *,
        owner_id: uuid.UUID,
        purpose: OutboundPurpose,
        input_classification: DataClassification,
        removed_flag_categories: tuple[str, ...],
        output_schema: str,
        policy_version: int,
        destination_class: str,
        decision: str,
        reason: str,
    ) -> PrivacyReceipt:
        receipt = PrivacyReceipt(
            id=uuid.uuid4(),
            owner_id=owner_id,
            purpose=purpose,
            input_classification=input_classification,
            removed_flag_categories=removed_flag_categories,
            output_schema=output_schema,
            policy_version=policy_version,
            timestamp=PrivacyReceipt.now(),
            destination_class=destination_class,
            decision=decision,
            reason=reason,
        )
        self._receipts.append(receipt)  # append-only -- no method on this class can mutate
        # or remove an existing entry.
        return receipt

    def all(self) -> tuple[PrivacyReceipt, ...]:
        return tuple(self._receipts)

    def for_owner(self, owner_id: uuid.UUID) -> tuple[PrivacyReceipt, ...]:
        return tuple(r for r in self._receipts if r.owner_id == owner_id)
