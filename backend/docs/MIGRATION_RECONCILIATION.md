# Migration Reconciliation — Z.AI lane ↔ Founder-Alpha composition lane

Status: **read-only upstream inspection** (shallow fetch of
`codex/founder-alpha-approved-precomposition` and
`codex/founder-alpha-0069-migration-compat` / `-recovery` on 2026-11-18). Nothing
upstream was modified; no merge has been performed.

## 1. What upstream actually has

The founder-alpha lane branches from the shared base **0068_workforce_ops** and numbers
its revisions with FULL NAMES:

| Upstream revision id | Content | Tables touched |
|---|---|---|
| `0069_embedding_space_identity` | document_chunks embedding-model columns + new embedding-space table | `document_chunks`, new table |
| `0070_blueprint_drafts` | new owner-scoped `blueprint_drafts` (RLS in-migration) | new table only |
| `0071_blueprint_approval_integrity` | blueprint approval integrity | new/own tables |
| `0072_blueprint_approve_identity` | blueprint approve identity | own tables |
| `0073_provider_disclosure_local` | ALTERs `provider_disclosure_events` | own table |

Their repo merge-rule (quoted from 0070's docstring): *"this migration's down_revision
must end up chained onto the resulting single head at merge time (standard per the repo
merge-rule). No data/logic conflict exists."*

## 2. The conflict (lineage, NOT schema)

The Z.AI lane (this repo) also branched from **0068** and used BARE-NUMBER ids:

| Z.AI revision id | Content | Tables touched |
|---|---|---|
| `0069` | founder_email_change_requests | new table only |
| `0070` | billing foundation | `billing_tenants`, `users.tenant_id`, `usage_log` ADD COLUMNs, `usage_rollups`, `revenue_ledger` |
| `0071` | advertising tables | `ad_consent_events`, `ad_events` |
| `0072` | usage idempotency | `usage_log` ADD COLUMN + index |

Merging both lanes today produces **two heads** (`0072` and
`0073_provider_disclosure_local`) and a confusing numbering overlap. **Schema-level
conflict: NONE.** The two lanes touch disjoint objects — upstream never touches `users`
columns, `usage_log`, `usage_rollups`, `revenue_ledger`, `billing_tenants` or `ad_*`;
the Z.AI lane never touches `document_chunks`, `blueprint_*` or
`provider_disclosure_events`. Every Z.AI migration is additive (CREATE TABLE / ADD
COLUMN), so **re-changing order cannot lose data** — DDL is order-independent among
disjoint objects.

## 3. Reconciliation strategy (preserves all data)

At composition time — and ONLY then, by whoever performs the approved merge — renumber
the Z.AI lane onto the upstream head, preserving chain order WITHIN the lane:

| Current (Z.AI) | Becomes | down_revision |
|---|---|---|
| `0069` founder_email_recovery | `0074_founder_email_recovery` | `0073_provider_disclosure_local` |
| `0070` billing_foundation | `0075_billing_foundation` | `0074_founder_email_recovery` |
| `0071` advertising_tables | `0076_advertising_tables` | `0075_billing_foundation` |
| `0072` usage_idempotency | `0077_usage_idempotency` | `0076_advertising_tables` |

Procedure (mechanical, data-preserving):
1. `git mv` each Z.AI migration file to its new name; edit `revision`/`down_revision`
   per the table above (each file's `upgrade()`/`downgrade()` bodies are untouched).
2. Re-point the in-file "NOTE FOR MIGRATION RECONCILIATION" blocks — they already
   describe exactly this operation, so they can be deleted once applied.
3. Verify: `alembic heads` shows ONE head (`0077_usage_idempotency`);
   `alembic history` is linear `0068 → (upstream 0069..0073) → 0074 → 0075 → 0076 → 0077`.
4. Verify on a DISPOSABLE PostgreSQL (see ci/verify.yml): `alembic upgrade head` from an
   empty database, then from a database upgraded to `0073_provider_disclosure_local`
   first (proves the chain composes on top of an existing founder-alpha deployment
   without touching its data).
5. The parity tests (tests/backend/test_billing_engine.py, test_founder_recovery_edge.py)
   assert lineage/DDL invariants and must be updated in the SAME commit if ids change.

Do NOT squash, rewrite history, or drop any migration: both lanes' deployments may have
`alembic_version` rows pointing at their respective heads; renumbering only affects the
pre-merge repo state.

## 4. Freebuff

The Freebuff candidate remains **unavailable** (no `freebuff*` branch upstream; the
ingestion gate from `e69e4d9` stays as the integration path). Nothing here touches any
Freebuff development branch.
