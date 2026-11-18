# Founder Alpha — final integration gate (2026-11-18)

Status: **NO-GO for merge/deploy.** `first_cycle_go=false`. This document records the frozen
baseline, the Z.ai transfer blocker, the AST-derived migration DAG, the preserved
Knowledge/Blueprint delivery, and the SFA-1 least-privilege fix prepared on the follow-up
branch. Nothing here merges to the default branch, deploys, changes a shared database, or
activates a paid provider.

## 0. Frozen baseline

| Item | Value |
|---|---|
| Verified baseline branch | `freebuff/founder-alpha-integrated-20261118` |
| Verified baseline SHA | `dd69cb3003f1f963995f843f0f6233596f40d53d` |
| Draft PR | #459 (draft, open, `mergeable: MERGEABLE`) |
| Backend/frontend/Playwright CI | run `44277031011` — success (backend 2523 passed, 1 skipped, 0 failed) |
| Real Ollama + pgvector acceptance | run `44277021975` — success, 12 checks, 0 FAIL, 0 BLOCKED |
| Default branch | `claude/det-kommer-mer-879lcm` @ `69dc5ea5a9cfc750303ea1a9b335c78612bc8f39` — **unchanged** |
| Follow-up integration branch | `freebuff/founder-alpha-final-gate-20261118` (branched from the frozen SHA) |

The frozen branch is not rebased, amended or force-pushed. Every further change happens on the
follow-up branch, so the verified evidence above keeps pointing at the tree that produced it.

## 1. Z.ai delivery — BLOCKED on transfer, not on capability

Required artifact: commit `a1a0e0737edf14e6429f422b39c412e598447892` (the v2 bundle/patch).

What was attempted, verbatim outcomes:

| Attempt | Result |
|---|---|
| `git fetch origin a1a0e0737edf14e6429f422b39c412e598447892` | `fatal: remote error: upload-pack: not our ref a1a0e0737edf14e6429f422b39c412e598447892` |
| `GET /repos/d1n095/LifeAI/commits/a1a0e07…` (GitHub REST) | HTTP **422** — `No commit found for SHA: a1a0e07…` |
| Local object check (`git cat-file -t`) | `could not get object info` |
| Search across all **534** remote heads for a Z.ai/v2 branch tip resolving to that SHA | none |

GitHub resolves a commit by SHA whenever it is reachable from any ref in the repository, and
advertises it in `upload-pack`; both refusals therefore mean the object was never pushed here,
not that it is hidden. This repository has no branch, tag, artifact or doc that mentions the
SHA (`grep -r a1a0e07` over the tree: zero hits).

**This is not substituted.** The previously used GitHub-visible blueprint lane carried only the
`0069_embedding_space_identity` blob and the 0070–0074 blueprint chain; claiming that lane is
"Z.ai's v2 bundle" would assert equivalence nobody has verified. It is therefore not used here
as Z.ai evidence, and no Z.ai revision id, DDL, test or doc is present in this candidate.

**Exact unblocking input** (any one of these, supplied by the founder or the Z.ai environment):

1. a `git bundle` of the delivery (`git bundle create zai-v2.bundle a1a0e07…`), attached to the
   issue/PR or placed in the workspace; or
2. a patch series (`git format-patch -o zai-v2/ <base>..a1a0e07…`), supplied as files; or
3. the producing environment pushes that commit to any branch or tag in `d1n095/LifeAI`
   (`git push origin a1a0e07…:refs/heads/claude/zai-v2-delivery`), after which it is fetchable
   by SHA.

Until one of those arrives, the Z.ai integration step stays open. No further work in this
branch depends on it.

## 2. Migration DAG — AST-derived from the integrated tree, not from the historical plan

Method: every file in `backend/alembic/versions/` is parsed with Python's `ast` module; the
module-level `revision` and `down_revision` assignments are read as literal values, the graph is
rebuilt from those (never from filenames, never from an earlier report), and roots, heads,
dangling parents, duplicate revision ids and numeric-prefix continuity are computed.

Evidence on the frozen baseline (`dd69cb3`) and this branch:

```
migrations=78 duplicate_revision_ids=[]
roots=['0001']
heads=['0078']
dangling_down_revisions=[]
head 0078: depth 78
numeric-prefixed revisions: 78; max=78   (contiguous 1..78, no gaps)
```

Current single head: **`0078_repository_file_source_units`**, whose `down_revision` is
`0074_blueprint_generation_lifecycle_guard`. The chain composition is:

```
… 0068 → 0069_embedding_space_identity            (PR #457 chain root after 0068)
      → 0070_blueprint_drafts
      → 0071_blueprint_approval_guard
      → 0072_blueprint_approve_identity
      → 0073_blueprint_draft_idempotency
      → 0074_blueprint_lifecycle_guard
      → 0075_system_activation_authorization       (composition chain, renumbered)
      → 0076 …
      → 0077 …
      → 0078_repository_file_source_units          (head)
```

### Why the historical `0089–0097` numbering plan is rejected

That plan was derived from an earlier tree whose composition chain was believed to sit at
`0081–0088`. The integrated tree's real head is `0078`, so applying `0089–0097` verbatim would
(a) leave gaps that encourage a second, parallel chain later, and (b) bake one branch's
historical numbering into a repository that no longer has that shape. Numbering is derived from
repository content only.

### Derivation rule that will be applied when Z.ai's bundle arrives

1. Re-run the AST parse on the tree **including** Z.ai's revisions; if any revision id collides
   with an existing id (including `0075`–`0078`), renumber Z.ai's chain — keeping each
   migration's DDL semantics byte-identical, changing only `revision` and `down_revision`.
2. Chain the first Z.ai revision's `down_revision` to whatever the parse reports as the single
   head at merge time (currently `0078_repository_file_source_units`), never to a hardcoded
   number.
3. Re-verify: exactly one root, exactly one head, no dangling `down_revision`, no duplicate
   revision id, contiguous numbering, and `alembic heads` → one value in CI.
4. Roundtrip (`upgrade → downgrade -1 → upgrade`) on real PostgreSQL before any further
   acceptance run.

No `alembic stamp`, no destructive migration, no silent schema rewrite is used at any point.

## 3. Knowledge/Blueprint delivery — preserved, with provenance

The delivery is kept whole; this branch changes no knowledge, blueprint, retrieval, embedding,
ingestion or authorization code.

| Property | Where it lives | How it is proven |
|---|---|---|
| Draft generation grounded in retrieved sources | `app/blueprint/service.py::generate_blueprint_draft` | `source_refs` must match retrieved `document_id`s; an ungrounded draft is refused |
| Originals never mutated by generation | `provenance.originals_mutated == False` on every draft | acceptance check `blueprint_generate_real_model` asserts it with a real model |
| Original bytes immutable across restart | `GET /api/library/{id}/original` | SHA-256 of the uploaded bytes re-checked after a backend+worker restart |
| Versioned, never overwritten | `blueprint_drafts.version` (unique per title/owner) | `blueprint_new_version` (v1→v2) in the real-Ollama driver; versioning test in `test_blueprint_draft.py` |
| Founder-only approval, no self-approval | `blueprint_approve_draft` SECURITY DEFINER (migration 0071) | approval + second-approve-rejected (HTTP 409) checks in the acceptance driver |
| Owner isolation | `blueprint_drafts_isolation` in `app/rls.py` | cross-owner isolation checks in acceptance and RLS suites |
| Memory-source integrity | `memory_source_units`, `document_source_units`, `message_source_units`, `repository_file_source_units` | `_PROTECTED_TABLES` narrowing + write-once guard triggers; `test_runtime_table_privileges.py` measures the live ACL |
| Runtime role cannot forge provenance rows | `s1a_privilege_policy.py` (SELECT-only or SELECT+INSERT per table) | `has_table_privilege` verification in the same policy, both mutating and `--verify-only` paths |
| Migration-state integrity (this branch) | `_MIGRATION_STATE_TABLES` | `test_migration_state_privileges.py` — see §4 |

Founder acceptance tests preserved unchanged:

- `backend/tests/backend/test_founder_alpha_blueprint_e2e.py` — the full chain through the real
  HTTP API against real PostgreSQL/pgvector + RLS (provider is the repo's sanctioned
  deterministic fake, as documented in its own docstring).
- `.github/workflows/real-local-ai-verification.yml` — the real-model driver: auth, real chat,
  upload, real `all-minilm` embed + index, cited retrieval, `qwen2.5:0.5b` grounded answer,
  conversation persistence, embedding-space identity (`all-minilm`/384/1536), single head,
  cross-owner isolation, restart persistence, post-restart live retrieval (12 checks).
- `.github/workflows/founder-alpha-verification.yml` — the older pinned-reconstruction Blueprint
  acceptance (generate → inspect refs → new version → restart → approve).

### Requirement traceability

The 27 locked requirements are the founder's own numbered list from the directive that
commissioned the integrated candidate; that list is not duplicated in this repository, and this
document deliberately does **not** renumber or re-word it. The mapping delivered with PR #459
stands unchanged: each of the 27 was classified as either a **first-start blocker** or a
**post-start enhancement**, and every "delivered" claim was tied to a specific test or CI job.

What this document adds is the evidence anchors, so a reviewer can re-check any row without
re-reading chat history. Each of the 27 maps into exactly one of these capability groups, and
the group's classification governs its members:

| Group | Requirement family (as classified in the PR #459 report) | Evidence anchor | Classification |
|---|---|---|---|
| G1 | Locally executable AI, no mandatory paid provider | `app/providers/ollama_provider.py`, `docs/FOUNDER_ALPHA_LOCAL_AI.md`, real-Ollama acceptance run | first-start blocker — **PASS** (test models only, see §6) |
| G2 | Local embeddings without destructive re-embedding | `app/rag/embedding_space.py`, `0069_embedding_space_identity`, embedding-identity acceptance check | first-start blocker — **PASS** |
| G3 | Retrieval-grounded answers with cited sources | `app/rag/retrieve.py`, `grounded_retrieval` acceptance check | first-start blocker — **PASS** |
| G4 | Curated knowledge without mutating originals | `/original` endpoint, SHA-256 across restart | first-start blocker — **PASS** |
| G5 | Blueprint draft generation, provenance, versioning | `app/blueprint/service.py`, migrations 0070–0074 | first-start blocker — **PASS** |
| G6 | Explicit Founder approval; never self-approved | `blueprint_approve_draft` (0071), approval + 409 checks | first-start blocker — **PASS** |
| G7 | Owner isolation / no cross-owner leakage | `app/rls.py`, RLS + cross-owner tests | first-start blocker — **PASS** |
| G8 | Continuity across restart | restart persistence + post-restart retrieval checks | first-start blocker — **PASS** |
| G9 | Provenance/memory-source integrity at the privilege layer | `s1a_privilege_policy.py`, `test_runtime_table_privileges.py` | first-start blocker — **PASS** |
| G10 | Migration integrity (single head, reversible, untamperable) | AST DAG §2, `test_migration_roundtrip.py`, `test_migration_state_privileges.py` | first-start blocker — **PASS** on this branch |
| G11 | Production model pair proven on the real VPS | not exercised (test models only) | **post-start enhancement** |
| G12 | VPS backup/restore, compose topology, deploy/rollback scripts | no infrastructure authorization | **post-start**, BLOCKED |
| G13 | Billing/commercial tiers | out of scope by standing rule | future product |

No requirement was dropped, renumbered, merged away or silently reclassified in this pass.

## 4. SFA-1 — runtime role could DELETE migration state

Finding: `mainai_app` held SELECT/INSERT/UPDATE/DELETE on `alembic_version`. The schema-wide
floor deliberately permits those four on user-data tables (they are genuinely used), so nothing
narrowed this one.

Why it matters: `alembic_version` holds one row naming the revision the schema is at. Deleting
it does not undo anything — it *lies*. The next `alembic upgrade head` would no longer consider
the head applied and would attempt its DDL a second time against a database that already has it;
a forged UPDATE claims "already at head" so a restart skips work it believes is done. RLS does
not help: it is row-scoped, and this table has no policy. No code under `app/` or `scripts/`
reads `alembic_version`; Alembic itself only ever runs on the admin/migration connection.

Fix (least privilege, boot-persistent, fail-closed):

- `_MIGRATION_STATE_TABLES = ["alembic_version"]` in
  `backend/scripts/security/s1a_privilege_policy.py`; the policy now `REVOKE ALL`s it from
  `mainai_app` and **verifies** zero privileges on every boot, in both the mutating path and the
  worker's read-only `--verify-only` path.
- Kept separate from `_PROTECTED_TABLES` because those entries assert S1A provenance semantics
  plus table ownership, and `alembic_version` is Alembic's own bookkeeping object that a managed
  provider may own under a different identity. The invariant that matters here is only "the
  runtime role holds nothing on it".
- Because it is part of the existing boot policy, it also fixes **existing** databases (a GRANT
  never removes a privilege, so a migration-only fix would only ever help new ones), and it
  cannot be silently undone by a later restart.

Why legitimate migrations still work: `backend/alembic/env.py` sets the URL from
`settings.database_url` (the admin/migration role), and `backend/docker-entrypoint.sh` runs
`alembic upgrade head` before `apply_runtime_privileges.py`. The revocation targets
`mainai_app` only.

Executable PostgreSQL regression tests — `backend/tests/backend/test_migration_state_privileges.py`:

| Test | What it proves |
|---|---|
| `test_policy_revokes_every_privilege_on_alembic_version_from_the_runtime_role` | all seven privileges measured absent via `has_table_privilege` |
| `test_policy_is_idempotent_on_a_second_boot` | a restart against already-narrowed state is a clean no-op, not a boot failure |
| `test_runtime_role_is_refused_by_postgres_itself[delete/update/insert/select]` | Postgres actually raises `InsufficientPrivilege` for the real `mainai_app` credential — not merely an absent catalog bit |
| `test_the_recorded_revision_is_still_intact_after_those_attempts` | `alembic_version` still holds exactly the single AST-derived head |
| `test_legacy_wide_grant_is_narrowed_for_migration_state` | reproduces the historical `GRANT ALL PRIVILEGES ON ALL TABLES` state and proves the boot policy reverses it |
| `test_the_migration_role_keeps_full_write_access_to_its_own_bookkeeping` | the migration role performs a real `UPDATE` — least privilege was not over-applied |
| `test_alembic_upgrade_head_still_runs_after_the_revocation` | the exact `docker-entrypoint.sh` command exits 0 after the revocation |

## 5. Verification matrix for this branch

| Check | Where | Status |
|---|---|---|
| Backend suite (includes the SFA-1 tests) | `ci.yml` → `pytest tests/backend/` | see PR body for the run id on this SHA |
| Security suite (RLS/privilege) | `ci.yml` → `pytest tests/security/` | see PR body |
| Account suite | `ci.yml` → `pytest tests/account/` | see PR body |
| Alembic single head + roundtrip | `ci.yml` (roundtrip test) | see PR body |
| TypeScript + ESLint + frontend build | `ci.yml` | see PR body |
| Playwright E2E (both suites) | `ci.yml` | see PR body |
| Real Ollama + pgvector acceptance (12 checks) | `real-local-ai-verification.yml` with `candidate_sha=<this SHA>` | see PR body |
| Founder Acceptance Suite (Blueprint E2E + pinned reconstruction) | `test_founder_alpha_blueprint_e2e.py`, `founder-alpha-verification.yml` | see PR body |

Exact run ids and their conclusions are recorded in PR #460's body and in the founder report for
this candidate; they are intentionally not hardcoded here, so this file can never disagree with
the runs it points at.

## 6. Remaining blockers

1. **Z.ai v2 delivery** — the required SHA is not fetchable; see §1 for the exact transfer
   inputs. Z.ai's own work remains unverified until then.
2. **Production model pair** — only test models (`all-minilm`, `qwen2.5:0.5b`) are proven.
   A production pair with a native dimension > 1536 needs a Founder-gated `EMBEDDING_DIM`
   migration; that is not performed here.
3. **Infrastructure** — VPS backup/restore, compose topology, `deploy.sh`/`rollback.sh` and the
   combined-container verification are gated to specific branches and lack infrastructure
   authorization.
4. **Pre-existing, unrelated** — PI-1b/1c/1d and the #447 backend flakiness remain unverified;
   they are not touched by this candidate.
5. **Default-branch ordering** — merging needs the founder's decision against the other open
   `0069` lanes (#326, #403, #411, #437). No merge is performed here.

## 7. Recommendation

**NO-GO for merge, deployment or production activation; GO for founder review of this
candidate.** The integrated candidate is technically green on real infrastructure (PostgreSQL,
Redis, pgvector, Ollama) and its migration graph is provably a single, contiguous, reversible
chain. It cannot be declared complete because one commissioned delivery (Z.ai) is unobtainable,
one requirement family (production model pair on the real VPS) is unproven by design, and the
merge ordering is the founder's decision.

`first_cycle_go=false`.
