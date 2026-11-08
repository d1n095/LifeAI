# Founder Alpha final gate matrix

Snapshot: 2026-11-08. This document is a review and first-start decision package. It is not release authorization. `first_cycle_go=false`.

## Immutable identities

| Artifact | Exact commit | Tree / role |
| --- | --- | --- |
| Founder Alpha product candidate, PR #352 | `07ce4a1d02d0eaa326acb53c0cb1d0a4708ee765` | `cc454df61bcfd0eccaf98ccb0b057fa823acf96c`; product under review |
| Original-integrity candidate, PR #351 | `a0063d4da6bce871dead53fcae26bee546c92859` | independently reviewed scope dependency |
| Embedding/provenance candidate, PR #349 | `6598559b138e0b7b4b9af0a2da80d25b1df8d6f8` | independently reviewed scope dependency |
| Blueprint candidate, PR #340 | `add0da831c101af7061b7cf6309ce8041ed969aa` | independent verdict still required |
| Full-history preflight evidence, PR #356 | `2adae19288fbf59b91b224ed2ffab927be4ed3b3` | supporting CI evidence only |
| VPS recovery evidence, PR #357 | `ca9f8289390bdc0a8fb7021da00b1a4a26c4b6a2` | supporting CI evidence only; jobs bind to #352 SHA/tree |

## Result vocabulary

- **PASS**: the named executable check completed successfully at the stated immutable identity.
- **FAIL**: the check executed and violated its assertion.
- **SKIPPED**: the workflow created the job but its condition prevented execution.
- **NOT TESTED**: no authoritative executable evidence exists for the scenario.
- **REVIEW REQUIRED**: builder/CI evidence exists, but no independent exact-SHA security verdict exists.

## Mandatory acceptance gates

| Criterion | Product/evidence identity | Executable evidence | Independent verdict | Current gate | First controlled startup impact |
| --- | --- | --- | --- | --- | --- |
| Original bytes are owner-scoped and fail closed without a trustworthy integrity anchor | #351 `a0063d4...`; composed in #352 `07ce4a1...` | #351 run `42381090035`; backend/RLS/migration PASS | Independent review recorded PASS for the changed original-retrieval scope at exact #351 SHA. Reviewer explicitly did not locally rerun the candidate because its object was unavailable in that environment. | **Scope PASS; composition REVIEW REQUIRED** | Blocks startup until #352 composition is independently reviewed |
| Embedding model/dimension identity fails closed; downgrade provenance is not invented or lost | #349 `6598559...`; composed/renumbered in #352 | #349 run `42061641376`; backend `127364637288`, migration `127364636491`, RLS `127364637019` PASS | Independent review recorded PASS for isolated #349 scope, plus a sibling-0069 integration blocker. #352 resolves this structurally with the serial `0068 -> 0069_embedding_space_identity -> 0070... -> 0073...` chain; that resolution is not independently certified. | **Isolated PASS; composition REVIEW REQUIRED** | Blocks startup until migration composition is independently reviewed |
| Runtime approval identity, immutable approved content, serialized approval and bounded version retry | #340 `add0da83...`; composed in #352 | Builder run `41983523917`, backend job `127098844627`; real-local run `41983523983` | Earlier verdicts cover older SHAs only. No independent verdict exists for `add0da83...`. | **REVIEW REQUIRED** | Blocking P1 security gate |
| Complete Founder Alpha composition: auth, RLS, source authority, approval boundaries, restart persistence and fail-closed behavior | #352 `07ce4a1...` / tree `cc454df...` | CI runs `42426032374` and `42426029333`; real-local run `42426029291`, job `128564375005` PASS | No independent exact-SHA verdict | **REVIEW REQUIRED** | Blocking final startup gate |
| Repository-history preflight validates the approved integration object without relaxing fail-closed behavior | #356 `2adae192...` | run `42476047042`: backend 2,376 PASS, 1 SKIPPED; all executed ordinary gates PASS | Supporting evidence, not security certification | **PASS (supporting)** | No remaining functional blocker |
| VPS topology startup, worker crash recovery and provider degradation against frozen product tree | #357 `ca9f828...` checking out #352 SHA/tree | run `42484553449`, compose job `128743550344` PASS | Supporting recovery evidence, not security certification | **PASS (supporting)** | No remaining synthetic-startup blocker |
| Backup integrity and disposable restore against frozen product tree | same | run `42484553449`, backup job `128743550376` PASS | Supporting recovery evidence | **PASS (supporting)** | No remaining synthetic-restore blocker |
| Failed deployment detection and rollback to known-good digests | same | run `42484553449`, deploy/rollback job `128743550504` PASS | Supporting recovery evidence | **PASS (supporting)** | No remaining synthetic-rollback blocker |
| Full ordinary regression battery on the #357 evidence head | #357 `ca9f828...` | run `42484553449`, backend job `128743550489`: 2,373 PASS, 1 SKIPPED; required aggregate job `128751449417` PASS | Supporting regression evidence | **PASS (supporting)** | No regression blocker introduced by the evidence harness |
| VPS bootstrap scripts under current Ubuntu 26 / ShellCheck 0.11 runner | #357 | job `128743551489` SKIPPED. Earlier superseded run showed inherited SC2329 informational findings returning failure. | None | **NOT TESTED / UNCOVERED** | Blocks production deployment readiness; does not invalidate the isolated synthetic startup proof |
| Combined-container topology | #357 | job `128743551635` SKIPPED | None | **SKIPPED** | Later production-release evidence; separate VPS topology executed |
| Real STRATO host, production DNS/TLS, production credentials and real off-host backup restore | none | none | none | **NOT TESTED** | Blocks production deployment, not an isolated Founder-supervised synthetic rehearsal |

## Exact VPS evidence binding

The three executed VPS jobs use `actions/checkout` with ref `07ce4a1d02d0eaa326acb53c0cb1d0a4708ee765` and then assert both:

```text
git rev-parse HEAD        == 07ce4a1d02d0eaa326acb53c0cb1d0a4708ee765
git rev-parse HEAD^{tree} == cc454df61bcfd0eccaf98ccb0b057fa823acf96c
```

Downloaded job-log SHA-256 values:

| Job | Job ID | Log SHA-256 |
| --- | ---: | --- |
| Compose topology | `128743550344` | `347c4dbe437f2e05249969be27f57eba571735201a2a237155a866d5b20877cd` |
| Backup/restore | `128743550376` | `ca442021183ddf34fc90183da07302186841a28c11334bc3f1cd73b9b6605111` |
| Deploy/rollback | `128743550504` | `1a8dc4a1b5faa20ab7268d82947c5ec2d2963c05e8ebd5a01e6151a32ef9ce5e` |
| Backend regression | `128743550489` | `3750bdb4c9e99e58af5d128a2fb7e582b6b6b56a385e6f79dc80b7f85f601e93` |

The logs record the exact checkout, tree assertion, injected worker SIGKILL, lease recovery, byte-identical restored upload volume, intentionally broken image, automatic rollback, restored traffic, provider-unreachable degradation and clean teardown.

## Fixture-correction security assessment

The #357 evidence harness changes no product file. Its corrections preserve or strengthen boundaries:

1. Synthetic model names satisfy #352's existing fail-closed requirement; provider traffic remains inside the network-isolated CI stub and paid fallback remains disabled.
2. The fake GHCR presence marker is copied only into the disposable runner's root Docker config because Ubuntu 26 ignores `sudo -E`. It is not a real credential, is never exported to product containers and cannot authenticate to GHCR.
3. The existing local provider stub is copied into the disposable deploy fixture at the relative path already declared by the CI-only Compose override. No external provider or network authority is introduced.
4. The stale no-provider expectation is replaced with the stricter #352 contract: the upload must complete through the local stub; `failed` and `blocked` are rejected. Later provider shutdown must still yield persisted user input and explicit degraded results.

## Required independent handoff

### PR #340 at `add0da831c101af7061b7cf6309ce8041ed969aa`

Re-run the #343 non-unique-integrity probe unmodified and regression items 1–4 and 6. Verify runtime-role identity spoofing remains closed, approval remains atomic, approved rows remain immutable, version retries occur only for `23505` on `ix_blueprint_drafts_owner_title_version`, source ownership is enforced and real-local evidence is bound to this exact SHA.

### PR #352 at `07ce4a1d02d0eaa326acb53c0cb1d0a4708ee765`

Independently verify:

1. exact ancestry and single migration head `0073_provider_disclosure_local`;
2. fresh install and populated `0068 -> 0069 -> 0070 -> 0071 -> 0072 -> 0073` upgrade/round trip;
3. #349 identity/provenance invariants after migration renumbering;
4. #351 original-integrity trust anchor and owner isolation;
5. #340 runtime approval identity, concurrency, immutability and version retry;
6. Founder authentication, RLS and source ownership across the composed API/UI;
7. restart persistence and provider-failure rollback;
8. no implicit paid fallback, merge, deployment, Recall or external-file authority.

The reviewer must publish an exact-SHA PASS/FAIL/BLOCKED verdict. Green CI alone is not certification.

## Decision

`first_cycle_go=false`. Synthetic functionality and recovery evidence are strong, but first controlled startup remains blocked by the missing independent verdicts for #340 `add0da83...` and #352 `07ce4a1...`. Production deployment additionally remains blocked by the uncovered bootstrap-script runner gate and all real-host requirements.
