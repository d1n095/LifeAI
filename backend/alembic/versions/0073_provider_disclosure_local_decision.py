"""Composed-lineage compatibility marker for provider disclosure locality — migration 0073.

0062 created the egress disclosure ledger with decision CHECK-constrained to ('allowed',
'denied'). A call to an ON-HOST local model runner (Ollama on loopback / in the same
deployment — see app/providers/ollama_provider.py's is_external_egress) is still fully gated
(incomplete-request and NEVER_EGRESS denies, plus secret redaction, all apply) but never left
the host, so recording it as an external-AI `allowed` disclosure is untrue. This adds a third
truthful decision value, `local`, so a local-first ingestion produces zero external `allowed`
rows — a checkable no-egress invariant.

The actual constraint transition is owned by its historical, already-published revision
``0069``. Frozen Founder Alpha databases may already record this ``0073`` head, so the
identifier must remain in the graph. It is deliberately a no-op: fresh and historical-0069
installs both arrive here with identical locality constraints, while existing #352 databases
remain valid without replaying an already-applied ALTER TABLE.
"""

revision = "0073_provider_disclosure_local"
down_revision = "0072_blueprint_approve_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
