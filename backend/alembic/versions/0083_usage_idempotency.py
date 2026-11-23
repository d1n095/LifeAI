"""Revenue Engine hardening (migration 0072).

One change: usage_log.idempotency_key — a caller-supplied de-duplication key for
ACCOUNTING events. Financial-correctness requirement: a duplicate/replayed usage event
(the same logical provider call recorded twice) must produce ONE audit row and ONE
rollup increment, never a double charge.

Design notes:
* NULL keys are unlimited (ordinary calls don't dedupe — each retry of a FAILED call is
  a distinct billable event and must count; dedup only applies where a caller explicitly
  keys an event).
* UNIQUE index (PG) / unique constraint (sqlite) makes the guarantee race-safe: two
  concurrent writers with the same key — one insert wins, the loser reads the existing
  row and contributes nothing (app/billing/accounting.py handles the IntegrityError).
* Rollup increments happen ONLY on the winning insert, so rollups can never drift from
  the audit rows.

NOTE FOR MIGRATION RECONCILIATION: the founder-alpha composition lanes number their
migrations 0069..0079 in a DISJOINT history. This revision (0072, down_revision=0071)
belongs to the Z.AI lane lineage and must be renumbered/re-chained by the composition
tooling when the lanes merge — see docs/MIGRATION_RECONCILIATION.md — it adds one
nullable column and one index and conflicts with nothing.
"""

from alembic import op

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE usage_log ADD COLUMN idempotency_key varchar(128) NULL")
    op.execute("CREATE UNIQUE INDEX uq_usage_log_idempotency ON usage_log (idempotency_key) WHERE idempotency_key IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_usage_log_idempotency")
    op.execute("ALTER TABLE usage_log DROP COLUMN IF EXISTS idempotency_key")
