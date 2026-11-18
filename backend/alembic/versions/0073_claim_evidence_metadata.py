"""Claim-level study-metadata storage — the PRODUCER side of the evidence-quality layer.

`app/rag/evidence_quality.grade_evidence()` has existed since its founding pass as a pure
function over a metadata dict, with one documented integration point:
`epistemics.classify_claim(quality_cap=...)`. What never existed was any production caller:
nothing in the app produced the metadata, so the grader ran only in its own test file, and
every live `classify_claim` call site ran UNCAPPED. This migration adds the missing half of
that edge: a per-claim row holding exactly what a founder (or an import pipeline; never an
ungated model leg) DECLARED, plus the grader's MATERIALIZED verdict over it — grade, ladder
cap, caveats, per-axis findings.

Write-time materialization is the design: `epistemic_status_counts_sql()` must agree with
`classify_claim(quality_cap=...)` from ONE query, and re-implementing the grading rubric in
SQL would put two implementations of one rule into the codebase. The grader runs once, at
stamp time; the rollup LEFT JOINs this table and substitutes over the closed vocabulary the
grader already emits.

Fail-closed: unknown metadata keys are rejected by the producer with a named reason and
never stored (see app/rag/evidence_metadata.normalize_declared_metadata) — declared absence
is honest, invented metadata is not.

Owner anchoring mirrors migrations 0019/0056: the composite FK
(claim_id, owner_id) -> knowledge_claims(id, owner_id) (the constraint 0056 added) makes a
cross-owner reference a foreign-key violation at the database, not merely a row RLS hides.
One row per claim; re-stamping updates in place so the materialized verdict can never go
stale against a corrected declaration.
"""

from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE claim_evidence_metadata (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            claim_id uuid NOT NULL,
            metadata_json jsonb NOT NULL,
            quality_grade varchar(20) NOT NULL,
            quality_cap varchar(40) NOT NULL,
            quality_caveats jsonb NOT NULL DEFAULT '[]'::jsonb,
            axis_findings jsonb NOT NULL DEFAULT '[]'::jsonb,
            rejected_keys jsonb NOT NULL DEFAULT '{}'::jsonb,
            declared_via varchar(60) NOT NULL,
            recorded_at timestamp NOT NULL DEFAULT now(),
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT fk_claim_evidence_metadata_claim_owner FOREIGN KEY (claim_id, owner_id)
                REFERENCES knowledge_claims (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT uq_claim_evidence_metadata_claim_owner UNIQUE (claim_id, owner_id),
            CONSTRAINT ck_claim_evidence_metadata_metadata_object CHECK (jsonb_typeof(metadata_json) = 'object'),
            CONSTRAINT ck_claim_evidence_metadata_caveats_array CHECK (jsonb_typeof(quality_caveats) = 'array'),
            CONSTRAINT ck_claim_evidence_metadata_axes_array CHECK (jsonb_typeof(axis_findings) = 'array'),
            CONSTRAINT ck_claim_evidence_metadata_rejected_object CHECK (jsonb_typeof(rejected_keys) = 'object'),
            CONSTRAINT ck_claim_evidence_metadata_grade CHECK (quality_grade IN (
                'high', 'moderate', 'low', 'very_low', 'unassessable'
            )),
            CONSTRAINT ck_claim_evidence_metadata_cap CHECK (quality_cap IN (
                'verified_fact', 'conflicting_evidence', 'hypothesis', 'assumption', 'unknown'
            )),
            -- The grader's own contract: a cap can only ever LOWER a ladder verdict
            -- (evidence_quality grade_evidence: epistemic_cap is the HIGHEST status the
            -- metadata supports), and conflicting_evidence is a contradiction verdict no
            -- metadata quality can grant or revoke. declared_via names the provenance of
            -- the declaration itself — a founder or pipeline channel, never an ungated
            -- model leg.
            CONSTRAINT ck_claim_evidence_metadata_cap_range CHECK (quality_cap IN ('verified_fact', 'hypothesis', 'unknown')),
            CONSTRAINT ck_claim_evidence_metadata_declared_via CHECK (declared_via <> '')
        );
        CREATE INDEX ix_claim_evidence_metadata_claim ON claim_evidence_metadata(claim_id);
        CREATE INDEX ix_claim_evidence_metadata_owner ON claim_evidence_metadata(owner_id);

        ALTER TABLE claim_evidence_metadata ENABLE ROW LEVEL SECURITY;
        ALTER TABLE claim_evidence_metadata FORCE ROW LEVEL SECURITY;
        CREATE POLICY claim_evidence_metadata_isolation ON claim_evidence_metadata
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    # No explicit GRANT/REVOKE, matching every migration since 0004: mainai_app's baseline
    # access comes from ALTER DEFAULT PRIVILEGES, and app/rls.py verifies the append-only
    # narrowing on every boot. This table is intentionally UPDATE-able (re-stamping a
    # corrected declaration must move the materialized verdict with it), so it does NOT get
    # the SELECT/INSERT-only narrowing observation tables get.

    # Account erasure: ON DELETE CASCADE from users (owner_id) and from knowledge_claims
    # (the composite FK) — both paths are already walked by the existing erasure flows, so
    # no separate erasure function is needed here.


def downgrade() -> None:
    op.execute("DROP TABLE claim_evidence_metadata;")
