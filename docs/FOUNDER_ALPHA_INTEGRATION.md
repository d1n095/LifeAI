# Founder Alpha integration checkpoint

This branch is a reproducible **composition rehearsal**, not a release candidate. It integrates
the existing Founder Alpha building blocks at immutable SHAs, reconciles their migration lineage,
and adds an end-to-end journey that starts at the real file-upload boundary. It does not change
or certify any frozen source candidate.

## Evidence matrix

| Capability | Evidence | Current classification | First startup |
|---|---|---|---|
| Frontend security + CI | #323 `04b27e9` | Implemented; CI green on source SHA | Required |
| Secure Founder auth/session | Existing base auth + Founder gates | Implemented; inherited security coverage | Required |
| Provider-independent boot | #328 `fa840e4`, #332 `1a67a16` | Implemented; review required | Required |
| Local model/no-egress | #326 `3a07f58`, #334 `82138cc`, #345 `a35a40f` | Implemented; review required | Required |
| Document extraction | Existing Library + #327 `69cdaf3` | Implemented for supported formats; OCR missing | Required for text/DOCX |
| Original integrity/download | #351 `a0063d4` + #330 `7a44043` | Frozen; independent review required | Required |
| Embedding-space identity | #349 `6598559` | Frozen; independent review required | Required |
| Grounded Blueprint + approval | #340 `add0da8` | Builder candidate; independent review required | Required |
| Durable restart acceptance | Founder Alpha real-local workflow | Implemented in CI; exact composed SHA pending | Required |
| Infinite Canvas, OCR, unattended autonomy | Separate future work | Deferred | No |

The machine-readable source of this table is
[`founder_alpha_integration_manifest.json`](founder_alpha_integration_manifest.json). The verifier
`backend/scripts/ci/verify_founder_alpha_integration.py` proves every source SHA is in the commit
ancestry and that Alembic has exactly one head. It deliberately reports `release_ready=false` and
`first_cycle_go=false`; composition evidence is not independent certification or merge authority.

## Executable Founder journey

`test_real_upload_to_original_to_blueprint_to_approval_journey` exercises:

1. authenticated Founder upload through `/api/library/import`;
2. the real durable worker, filesystem storage, parsing, pgvector indexing and source identity;
3. byte-identical, checksum-verified original retrieval;
4. source-grounded Blueprint generation and immutable version 2;
5. explicit Founder approval through the database-enforced approval boundary; and
6. a committed-state reread proving that source identity, storage key and original bytes did not
   change.

Only the model seam is deterministic in the ordinary backend suite. The integration workflow
also runs the existing real-local Ollama acceptance with real PostgreSQL, pgvector and filesystem
storage, then restarts backend and worker before approval.

## Migration composition

The two source branches independently used the 0069 slot. This integration serializes them:

`0068 → 0069_embedding_space_identity → 0070_blueprint_drafts →`
`0071_blueprint_approval_guard → 0072_blueprint_approve_identity →`
`0073_provider_disclosure_local`

No source migration was silently dropped or combined. The provider-locality migration remains
additive and its downgrade guard remains unchanged.

## Release gate

Release remains blocked until independent reviewers approve the exact #349, #340 and #351 SHAs,
this branch's exact-SHA CI and real-local acceptance are green, and the Founder separately
authorizes merge/deployment. A later source-SHA replacement must update the manifest, preserve a
single migration head, and rerun the complete acceptance workflow; moving branch references are
never certification evidence.
