# LifeAI — Living Project Status

**Canonical operational status of the LifeAI / MainAI ("Life") project.** This is the
day-to-day operational journal: what is built, what is reviewed, what is blocked, and what
runs. It does **not** replace:

- `docs/LIFE_MASTER_BLUEPRINT.md` / `docs/LIFE_MASTER_HANDOFF.md` — the architecture and the
  source-linked system entry point.
- `docs/BRANCH_REGISTRY.md` — the authoritative branch/PR dependency map and merge order.

When those documents and this one disagree, the Blueprint governs architecture and the Branch
Registry governs merge order; this file records the *current operational state* on top of them.

## How this file is maintained

- **Update this file whenever development, review, CI, integration, or deployment status
  changes materially.** Prefer a documentation-only commit, independent of implementation.
- **Every claim is tagged with its evidence class** (see legend). Do not present a developer
  report or an expectation as verified evidence.
- **Change tracking lives in Git history**, not inline. Do not silently overwrite a historical
  conclusion: if a prior conclusion is corrected, add a dated correction line under
  "Corrections log" and update the body, so `git log -p docs/LIFEAI_STATUS.md` shows the full
  trail.
- **Never invent PASS results and never commit secrets.** A check is `VERIFIED` only when a
  named, re-runnable job/command actually produced the result at a named SHA.

### Evidence legend

| Tag | Meaning |
|-----|---------|
| **[VERIFIED]** | Reproduced by a named CI job or a command at a named commit SHA, output inspected. |
| **[REPORTED]** | Stated by a developer/agent; not independently re-executed in this record. |
| **[PENDING]** | Work defined and in flight, no result yet. |
| **[DECISION]** | Requires a Founder/product/business decision before it can proceed. |
| **[BLOCKED]** | Cannot proceed until a named dependency or input is available. |

---

## 1. Snapshot

- **Date:** 2026-11-03
- **Default branch:** `claude/det-kommer-mer-879lcm` @ `64c07d5b0c4821bfb175700283021aa6e95cb61c`
- **Integration candidate:** `examiner/integration-firststart` @ `3ce537759298be5eb0570efcb4858d54df940065`
  (tree `ebdced820249b584b69c9d6418e9895cc5c380bf`). **[VERIFIED]** composition (by
  `git merge-base --is-ancestor`): base + **#323 + #327 + #328 + #329**. Does **not** contain
  #326 or #330. Deterministically reconstructible from the pinned commits below.
- **`first_cycle_go`:** `false` — no autonomous activation, no paid providers, no Recall.
- **Operational priority:** get the *current* Life running and verifiable, not expand the
  architecture.

---

## 2. Preserved development branches (exact SHAs) [VERIFIED]

All development is preserved on the GitHub remote (`d1n095/LifeAI`, private). Verified by
`git ls-remote --heads origin`.

| Branch | Remote tip | Contents |
|--------|-----------|----------|
| `claude/mainai-crossplatform-app-shell` | `aef16952ee30bfa1a8ef9fa95d4ab4a99d195fc2` | Cross-platform app lane: Tauri 2 macOS build, Android APK CI, registry (3 commits). |
| `examiner/staging-285-f5d9bf1-2026-10-19-local` | `f1e1a92b899dbf439763aafbf1988a2faa1848df` | Examiner verification artifacts + executable defect reproductions (79 commits). |

**Remaining local-only:** only reconstructible compositions — `examiner/integration-firststart`
(provably rebuildable from the pinned PR commits) and 8 `claude/mainai-supervisor-goal-*`
branches (merge-commits of already-pushed PRs #263–#267). **No unique source exists only
locally; no uncommitted implementation changes exist.** [VERIFIED]

---

## 3. PR status & dependencies (#323–#330)

Pinned heads **[VERIFIED]** via `gh pr view` on 2026-11-03. "Frozen" = do not modify; awaiting
independent review.

| PR | Head SHA | State | Base | Purpose | Review |
|----|----------|-------|------|---------|--------|
| **#323** | `04b27e9` | open | default | Frontend npm-audit consolidation; pins Playwright E2E jobs to `ubuntu-24.04`. No app logic. | Independent review required. [REPORTED] gate green in its own CI. |
| **#327** | `69cdaf3` | draft | default | DOCX extraction completeness (tables/nested/headers/footers). | Review required. |
| **#328** | `fa840e4` | draft | default | **Test-only** proof that basic startup is model-independent. | Review required. |
| **#329** | `830ab4d` | draft | default | `GET /api/library/:id/original` — authorized founder original-retrieval endpoint + Life service. | Review required. |
| **#330** | `7a44043` | open | **#329** | Frontend "download original" control (the UI for #329's endpoint). | Ready for independent review (see §4). |
| **#326** | `3a07f58` | draft | default | Egress gate: local on-host providers vs. external-AI disclosure (no-egress invariant). | Review required. |

### Dependency chain

- **#330 → depends on #329.** #330 is based on `claude/library-original-retrieval` and **must
  not merge to the default branch ahead of #329**, or the UI would call an endpoint that does
  not exist on default. [VERIFIED] the endpoint exists only in #329 + the candidate, not on
  default.
- **Recommended review/merge order:** `#323 → #327 → #326`, then the Library chain `#329 → #330`;
  #328 is test-only and can land with its target. Authoritative order: `docs/BRANCH_REGISTRY.md`.
- **Do not rebase a dependent branch preemptively** (repo `CLAUDE.md` merge-rule): rebase #330
  onto #323 only once #323 has actually merged.

---

## 4. #330 investigation — CI outcome & verification [VERIFIED]

Verified at `#330` head `7a44043` on 2026-11-03 via `gh pr checks` and GitHub Actions job logs.

**Green (all checks that execute on #330's base):** Frontend TypeScript & ESLint, Frontend
build (docker), Frontend build (vercel), Backend Alembic migration check, Backend RLS &
session-security, Backend account lifecycle & rate-limit. Backend unit/integration passes on
the base and #330 changes no backend code.

**Red — all three are base-inherited, NOT introduced by #330:**

| Failing check | Root cause | Evidence |
|---------------|-----------|----------|
| E2E — Playwright (full stack) | Job ran on `Image: ubuntu-26.04`; `npx playwright install --with-deps chromium` fails `ERROR: Playwright does not support chromium on ubuntu26.04-x64` **before any test executes**. | Exactly the failure #323's `ubuntu-24.04` pin fixes. #330's base (#329) lacks #323. |
| E2E — same-origin proxy | Same `ubuntu-26.04` chromium-install failure. | Same as above. |
| Frontend — npm audit | Base dependency advisories. | #330's `package-lock.json`/`package.json` diff vs. base is **empty** — zero dependencies added; identical result on base #329. |

**Control:** base #329 fails the same three checks and passes everything else.

**Self-review of the download control (no release-blocker found):** backend returns `503`
before serving any corrupted bytes (the client never receives bad bytes as a `200`); the
filename is used only as the anchor `download` attribute (no XSS; backend ASCII-sanitizes);
the object URL is revoked after click (`setTimeout(…,0)` + `a.remove()`, no leak); the request
uses `credentials:"include"` so the HttpOnly session cookie rides along (founder-only, not a
public link).

**Non-blocking future notes:** `fetch`+blob buffers the whole original in JS memory (fine for
the small-file acceptance scope; switch to a streamed download for large originals later); a
download clicked on a long-idle detail page can `401` without the auto-refresh the shared
`request()` path has.

**Verdict:** #330 is **ready for independent review**; its only red checks gate on #323.

---

## 5. Tests actually executed vs. merely discovered

Distinguishing executed results from discovery is a maintenance rule for this file.

| Item | Status | Note |
|------|--------|------|
| #330 `tsc --noEmit`, `eslint`, `next build` | **[VERIFIED]** executed locally, all pass; route `/library/[id]` compiles. |
| #330 `Content-Disposition` filename parser | **[VERIFIED]** executed, 6/6 cases (UTF-8 decode, malformed-encoding fallback, missing header). |
| #330 Playwright spec `library-original-download.spec.ts` (3 cases) | **[REPORTED] discovered only** — `playwright test --list` shows all 3; the full run needs the pgvector + Python backend and executes in CI's `e2e-tests` job (blocked on #323's `ubuntu-24.04` pin), not locally. |
| First-start acceptance (auth / conversation / upload / original retrieval / extraction / restart) | See §8 — executed in the dedicated verification workflow, tied to the candidate SHA. |

---

## 6. STRATO environment & deployment restrictions [REPORTED by Founder / VERIFIED from repo topology]

- An **older Life 0.x** installation is already running on STRATO (Ubuntu 24.04, 2 vCPU,
  ~1.8 GiB RAM, ~78 GB free, Docker/Compose present; containers healthy for ~2–3 months).
  This is **not** the current development version. [REPORTED by Founder]
- Topology (from repo `docker-compose.vps.yml`, `scripts/vps/*`): compose dir `/opt/lifeai`,
  backups `/var/backups/lifeai`, secrets `/etc/lifeai/lifeai.env` (root-owned — never read).
  Caddy (80/443) is the only public process → frontend:3000 → proxies `/api/*` → backend:8000
  over a private network. Postgres is **external Supabase** (no postgres container); Redis is
  ephemeral Valkey. Images are digest-pinned GHCR tags. [VERIFIED from repo]
- `GET /api/health` runs `SELECT 1` + Redis ping → a *healthy* backend already proves live
  Supabase DB connectivity. [VERIFIED from `backend/app/routers/health.py`]
- **Restrictions (current):** no new containers, migrations, deploy, restart, package updates,
  or infrastructure purchases on STRATO; no production credentials, no production Supabase, no
  use of the old STRATO environment for verification. Read-only diagnostics only (Q1–Q6 runbook,
  run by the Founder over SSH). `backup.sh` covers `lifeai_uploads` (the only durable copy of
  original bytes) + Caddy volumes + non-secret compose files; it does **not** cover secrets, the
  Supabase DB (Supabase's own backup/PITR), or Redis.
- Health checks do **not** prove founder login, messaging, or Library integrity — those need
  authenticated (mostly write) flows. Whether to write-test against the live environment or a
  throwaway one is a **[DECISION]**.

---

## 7. Security & architecture decisions (operational) [VERIFIED from repo unless noted]

- **Authorship cannot self-certify.** No CODEOWNERS/branch protection; single collaborator.
  Independent review = a separately assigned examiner (Gemini Pro assigned to #323).
- **Original bytes are immutable & content-addressed** (`storage/local_fs.py`: sha256 key,
  `os.link` atomic publish, hash-verified dedup/self-repair); cross-owner-safe purge via a
  `SECURITY DEFINER` reference check. `GET /api/library/:id/original` serves them founder-only,
  as `attachment` + `application/octet-stream` + `X-Content-Type-Options: nosniff`, with
  integrity verification (503 on failure) — an original that is HTML/SVG can never render
  inline.
- **Basic startup is model-independent** (#328): upload → durable original + sha256 →
  extraction (`content_preview`) → restart survival all succeed with **no** embedding/chat
  provider; a doc with no provider parks at `awaiting_provider` (a pause, not a failure).
- **Local-model retrieval — embedding-dim limitation now has a solution (PR #333, review
  pending).** Root cause: `document_chunks.embedding` is a fixed pgvector `vector(1536)` + HNSW
  `vector_cosine_ops` column, and chunks carried no model/dim identity. Solution (smallest
  safe, no destructive migration): a local model whose native dim ≤ 1536 is **zero-padded** up
  to the storage width — cosine-exact — and every chunk is stamped `(embedding_model,
  embedding_native_dim)` with retrieval filtered strictly to the active space so incompatible
  models are **never silently mixed**. Additive, reversible migration `0069_embedding_space_identity`;
  provider abstraction unchanged (`resolve_active(role=...)` stays the selection point). A
  model *wider* than 1536 still needs a Founder-gated column-dimension migration (not done).
  **[VERIFIED]** 10/10 targeted tests on real pgvector (incl. the migration); 405/405 rag
  regression (2 pre-existing environmental fails). The production local-model *choice* and its
  runtime remain a **[DECISION]**.
- **Egress invariant** (#326, pending review): local on-host providers ledger
  `decision='local'`, distinct from external-AI disclosure; never relaxes a deny.
- Production config guards (`_check_no_placeholder_secrets`, `_check_cookies_secure`) only
  raise when `ENVIRONMENT=production` — dev-inert, so a supervised dev boot is not blocked by
  them.

---

## 8. First-start verification [PENDING — see workflow]

An isolated, manually triggered GitHub Actions workflow verifies the integration candidate's
first-start acceptance criteria in CI, using only synthetic identities and files, with no
production credentials / Supabase / STRATO, and tied to the exact candidate SHA.

- **Workflow:** `.github/workflows/first-start-verification.yml` (manual `workflow_dispatch`;
  defaults the verified ref to the integration candidate `3ce5377`). Reuses the repository's
  proven CI service-container + backend/worker launch infrastructure (`run_e2e_backend.py`,
  `app.worker`) with its deterministic in-process fakes (no paid provider, no network egress).
- **Acceptance criteria exercised:** founder authentication; conversation persistence;
  small-file upload; immutable original retrieval (`/api/library/:id/original`, the #329
  capability #330's UI depends on); document extraction; persistence across a process restart.
- **Evidence:** the workflow emits explicit `PASS` / `FAIL` / `BLOCKED` per criterion, tied to
  the resolved tree, as a job summary + uploaded artifact.
- **Reconstruction:** the candidate is not pushed as a ref, so the workflow reconstructs it
  deterministically from the pinned PR heads (`#323 04b27e9` + `#328 fa840e4` + `#329 830ab4d`
  + `#327 69cdaf3`) and **asserts the resulting tree == `ebdced820…`** before running — a moved
  PR branch fails the run instead of silently testing something else.

- **Status / results — [VERIFIED]:** first-start acceptance **PASSED** at the verified
  candidate tree `ebdced820249b584b69c9d6418e9895cc5c380bf`.
  Run: <https://github.com/d1n095/LifeAI/actions/runs/41548691356> (2026-11-03, conclusion
  success). All 7 criteria, 0 FAIL, 0 BLOCKED:

  | Criterion | Result | Note |
  |-----------|--------|------|
  | Founder authentication | **PASS** | login 200, session established |
  | Conversation persistence | **PASS** | `assistant_status=succeeded` (deterministic fake provider) |
  | Small-file upload | **PASS** | import job accepted |
  | Document extraction | **PASS** | extraction completed; index status `blocked_provider`, i.e. embedding paused for want of a *verified* AI provider — the correct model-independent outcome, not a failure |
  | Immutable original retrieval (`/api/library/:id/original`, the #329 capability #330's UI uses) | **PASS** | returned bytes' sha256 == uploaded bytes |
  | Restart persistence | **PASS** | conversation + source + immutable original all survived a real backend+worker process restart |

  Caveat: this exercises the **model-independent** first-start path with the repo's
  deterministic in-process fakes (no real/paid provider, no egress). Embedding-dependent
  retrieval is out of scope here and remains gated on the `embedding_dim`/local-model
  **[DECISION]** in §7 and §9.

---

## 9. Outstanding blockers & next active implementation

**Blockers**

1. **Independent review** of #323 (then #327 / #326, and the #329 → #330 Library chain).
   [PENDING]
2. **Local-model retrieval**: the `embedding_dim` limitation is **solved** in PR #333
   (zero-pad + strict model/dim identity, additive reversible migration; [VERIFIED] 10/10 on
   real pgvector). Remaining: independent review of #333; the production local-model *choice* +
   runtime [DECISION]; and a live end-to-end run with a real local model (config-start +
   model-backed retrieval) — the one verification item not yet executed.
3. **First supervised runtime boot authorization** and the local embedding-model decision.
   [DECISION]
4. **STRATO upgrade path**: establish the deployed commit (read-only Q1–Q6), confirm backups,
   then build-in-CI → digest-pin bump → `docker compose up -d` → health green, rollback by
   re-pinning. All Founder-authorized, not autonomous. [BLOCKED on 1–3]

**Can the current Life be started and verified?**

- **Supervised dev boot:** no *code* blocker remains (dev compose is self-contained; no model
  required for basic startup; prod guards are dev-inert). [VERIFIED from repo]
- **Automated first-start verification:** **[VERIFIED] yes, for the model-independent path.**
  §8's workflow run passed all 7 acceptance criteria (auth, conversation persistence, small-file
  upload, document extraction, immutable original retrieval, restart persistence) at the
  verified candidate tree `ebdced820…`.
- **Real local-AI retrieval:** **[VERIFIED] yes.** §10's workflow (run 41666998428, tree
  `abeb762322…`) ran Life on a real local model (Ollama `all-minilm` + `qwen2.5:0.5b`) and passed
  all 8 criteria — real embed→index, authorized retrieval citation, real model-backed generation,
  padding+identity on the real chunk, cross-owner isolation, and restart survival. The remaining
  item is now a **DECISION**, not an engineering gap: which local model(s) Life runs in production.

**Next active implementation:** none opened without Founder direction (this task is complete).

---

## 10. Real local-AI verification — Ollama, isolated [VERIFIED — all criteria PASS]

The §8 workflow proves the **model-independent** first start (repo fakes). This section covers
the complementary proof the Founder asked for: that Life runs with a **real local AI model**,
generates model-backed responses, and retrieves from an uploaded document end-to-end.

**Result: Life ran on a real local AI model.** CI run **41666998428** @ `82138cc` (branch
`claude/real-local-ai-verification`) is **green** — all 8 acceptance criteria PASS (pre-phase
6/6, post-phase 3/3), the embedding-space identity assertion PASS, and the cross-owner/no-mixing
pytest PASS — against the verified candidate tree `abeb762322…`.

- **Workflow:** `.github/workflows/real-local-ai-verification.yml` (PR #334, branch
  `claude/real-local-ai-verification`). Manual `workflow_dispatch` + a bootstrap `push:` on its
  own branch for the first evidence run.
- **Integration candidate under test:** `#323 trunk (04b27e9) + #328 (fa840e4) + #329 (830ab4d)
  + #327 (69cdaf3) + #333 (fdf3647)` ⇒ **tree `abeb762322cd67ff95937ec57fd269fb0c4cc674`**
  (asserted before any step; single alembic `0069` head asserted too). [VERIFIED locally:
  reconstruction is conflict-free, `alembic upgrade head` → single head, `test_embedding_space.py`
  10/10 on real pgvector.]
- **Models & versions tested (TEMPORARY — not a production decision):** Ollama **0.40.14**;
  embedding **`all-minilm`** (native 384-dim), chat **`qwen2.5:0.5b`**. Exact pulled digests are
  in the run's job summary (`ollama list`).
- **Real, not faked:** the app's own provider abstraction is pointed at a real Ollama runtime on
  the runner (`DEFAULT_EMBEDDING_PROVIDER=ollama`, `DEFAULT_LLM_PROVIDER=ollama`). Only outbound
  **email** is faked; there is **no paid key** and `CHAT_FALLBACK_ORDER` is empty, so a paid
  provider is structurally unreachable — a dead Ollama FAILs the run, never a silent paid fallback.
- **Acceptance criteria (results tied to the run SHA/tree):**

  | # | Criterion | Result (run 41666998428 @ `82138cc`) |
  |---|---|---|
  | 1 | real embed loads + indexes (`status=indexed`, chunks>0) | **PASS** — `index status=indexed, chunk_count=1` (real `all-minilm` embed) |
  | 2 | vectors padded to 1536 (`vector_dims=1536`) | **PASS** — DB `vector_dims(embedding)=1536` |
  | 3 | chunk stamped (`embedding_model=all-minilm`, `embedding_native_dim=384`) | **PASS** — DB stamp `all-minilm` / `384` |
  | 4 | synthetic doc uploaded, extracted, indexed | **PASS** — import job accepted → `indexed` |
  | 5 | authorized retrieval cites the uploaded doc | **PASS** — `/api/chat` returned the uploaded source (document matched by `source_id`) |
  | 6 | real local chat generation (`assistant_status=succeeded`, `provider=ollama`) | **PASS** — `assistant_status=succeeded, provider=ollama, reply_len=57` |
  | 7 | cross-owner isolation + no-mixing on real pgvector | **PASS** — `test_embedding_space.py` 10/10 on the migrated schema + RLS |
  | 8 | conversation + original + indexed content survive restart | **PASS** — data persistence PASS **and** post-restart live retrieval cited the doc (`provider=ollama`) |

- **Migration-ancestry finding (reviewer action required).** #333's `0069_embedding_space_identity`
  and #326's `0069_provider_disclosure_local_decision` **both** declare `down_revision = "0068"`.
  Different revision *ids* (no id collision), but a **shared parent** ⇒ two alembic heads if both
  are ever present together (`alembic upgrade head` would fail with "Multiple head revisions").
  They do **not** collide in this candidate (#326 is absent → single head, verified). Per the repo
  merge rule (`CLAUDE.md`: the second-merged renumbers; no preemptive rebase "för säkerhets skull"),
  whichever of #326 / #333 merges **second** must re-point its `down_revision` onto the other's
  revision at merge time. Deliberately **not** pre-rebased here. [VERIFIED finding; fix is a
  documented merge-time action, not a code change in this PR.]

---

## 11. Executive business workflow — AI team through the real substrate [VERIFIED — 12/12 tests]

**PR #335** · branch `claude/mainai-executive-business-workflow` @ `aeb0bc3` (base: default
`claude/det-kommer-mer-879lcm` @ `64c07d5`) · OPEN, independent review required.

A Founder-facing executive layer (`app/mainai_business/`) turns a business objective into a
verified, persisted assessment produced by a **team of specialist agents**, driven entirely
through the existing workforce substrate. Composition only — **no new tables, no migration, no
provider invocation**, so it does **not** touch the pending #333/#326 `0069` alembic head.

- **Path:** objective → persistent plan → team of specialists → per-task scoped delegation →
  in-process execution → independent verification → dependency-ordered aggregation →
  Founder-approval gate on consequential actions → one reassembled project view
  (`/api/mainai/business`, founder-only + RLS).
- **Persistence without a migration:** each task is a real, RLS-scoped delegation request +
  assignment stamped with a shared `business_workflow_id`; the whole project is rebuilt from
  the database alone (proves durability + restart survival).
- **Honesty:** orchestration is real (registry, evidence-selector, broker, task-scoped
  authority, disclosure minimisation, independent verification, performance ledger,
  cancel/restart recovery); **per-task workers are deterministic in-process computations, not
  model calls** — same posture as `workforce.vertical_slice` / `safe_internal_run` — pending
  gated provider activation. Wiring #334's verified local model (`all-minilm`/`qwen2.5:0.5b`)
  in as the real worker inference is the honest **next step**, out of scope here, not claimed.
- **Founder-approval boundary (fail-closed):** the consequential `publish_assessment` step is
  never auto-run; without an explicit approval token it is `blocked_awaiting_founder_approval`
  (no authority minted, no worker run). With the token, the approval ref is persisted on the
  verification decision.
- **Reference objective:** synthetic e-commerce opportunity assessment (research + finance +
  operations → synthesis → Founder-gated publish). Generic/reusable — swap the spec, keep the
  engine. No real purchases, payments, publications or external operations.

**Executed against real Postgres (Python 3.12, pgvector/pg16, RLS) — all PASS:**
`test_business_workflow.py` (8) end-to-end + 7 security properties (Founder-approval boundary,
agent privilege isolation, cross-owner RLS isolation, duplicate prevention, prompt-injection
resistance, cancellation + restart recovery, audit/provenance) and `test_mainai_business_api.py`
(4) through the real FastAPI TestClient. Regression: those 12 + `test_workforce_foundation` (7)
+ `test_mainai_execution_api` (23) = **42 passed**; `ruff` clean; app imports with the new
router. See `docs/MAINAI_EXECUTIVE_BUSINESS_WORKFLOW.md`.

---

## 12. Model-backed AI team — real local inference in the specialists [VERIFIED — adapter 25/25 + real Ollama run green]

**PR #336** · branch `claude/mainai-model-backed-workers` @ `a802a47` (stacked on #335). OPEN,
independent review required; merge #335 first.

Connects #334's verified local inference to #335's business workflow so the specialist agents
do real model-backed work. One new seam — a pluggable `WorkerRunner` (`app/mainai_business/
execution.py`) — not a second framework; the workforce registry, broker, scoped authority,
verification pipeline, approval gate and persistence are all reused.

- **`ModelBackedRunner`** — real local inference via `providers.resolve_active(db,"chat")` →
  `OllamaProvider.chat`. Reads inputs from the **assignment's context package only** (context
  isolation); parses + Pydantic-validates the model's JSON (`schemas.py`) — unparseable output
  **fails the task**, never coerced; records an execution receipt (provider/model/latency/
  usage/prompt+output SHA-256/attempts) on provenance and in the view; bounded retry on parse
  failure.
- **Independent verification = a deterministic validator** (`validators.py`) run by a verifier
  agent distinct from the builder — never the builder model reviewing itself, never a second
  model rating the first; the decision records `deterministic_validator`. Output stays
  untrusted (REJECTED) until it passes.
- **Fail-closed, no paid fallback:** refuses any non-local chat provider; never calls
  `chat_with_fallback`. Provider-unreachable / schema-invalid / validator-rejected → task
  failed + dependents blocked, nothing fabricated.
- Founder interface: `POST /api/mainai/business/ecommerce-assessment` gains `mode`
  (`in_process` | `model_backed`); the `GET` view surfaces `runtime`, `provider_invoked` and
  the full `execution_receipt`.

**Verified (`.github/workflows/model-backed-ai-team.yml`):** (1) deterministic business pytest
suite on real Postgres — `test_business_schemas.py` (7) + `test_model_backed_workers.py` (6,
stub local provider) + `test_business_workflow.py` (8) + `test_mainai_business_api.py` (4) =
**25 PASS locally**; (2) a **real Ollama** acceptance run (`scripts/ci/
model_backed_ai_team_acceptance.py`, `qwen2.5:0.5b`) proving actual model-backed execution for
multiple roles, the approval boundary, DB reassembly with receipts, and no paid-provider use.
Real model weights run in CI only (no local Ollama on the dev box — same split as #334); full
local regression incl. existing suites = **55 passed**; `ruff` clean. No new migration (does
not touch the pending `0069` head). See `docs/MAINAI_MODEL_BACKED_AI_TEAM.md`.

**CI result — run 41701226997 @ `43b465d`, conclusion success (after one fix, below):** the
deterministic suite passed (25/25 on real Postgres) and the real Ollama acceptance passed all
15 invariants — all four upstream specialists (research, finance, operations, synthesis) ran
`model_backed` on real `qwen2.5:0.5b` with ollama receipts + hashes and were independently
verified (`recommendation = conditional-go`); the publish step was blocked without approval
and, once approved, the 0.5B model's publication output was **rejected by the deterministic
validator** (`published=false`) — the honest, designed outcome: the independent check bit on a
weak real-model output instead of rubber-stamping it; nothing fabricated. Evidence artifact:
`model-backed-ai-team-evidence`.

---

## Corrections log

Append a dated line here whenever a prior conclusion in this file is corrected. Never delete a
superseded conclusion silently — supersede it here and in the body.

- _2026-11-03_ — Initial version. Baseline established from verified Git/GitHub state and prior
  session evidence.
- _2026-11-03_ — Embedding-dim limitation solved (PR #333, branch
  `claude/embedding-space-local-model` @ `fdf3647`): zero-pad fitting + strict `(model,
  native_dim)` identity, additive reversible migration `0069_embedding_space_identity`.
  [VERIFIED] 10/10 targeted tests on real local pgvector (migration applied via `alembic
  upgrade head`); 405/405 rag regression (same 2 pre-existing environmental fails). §7 and §9
  updated from DECISION-only to solution-pending-review. Not yet executed: a live run with a
  real local model (config-start + model-backed retrieval).
- _2026-11-03_ — §8 filled in with the first real run of the first-start verification workflow
  (run 41548691356): all 7 acceptance criteria PASS at candidate tree `ebdced820…`,
  model-independent path. §9's "can it be verified?" updated from PENDING to VERIFIED-yes for
  that path (embedding-dependent retrieval still blocked). Earlier draft's `document_extraction`
  was conservatively BLOCKED on an earlier run because `blocked_provider` was unclassified; it
  is now correctly PASS (extraction completed; embedding paused by design) — see the workflow
  commit history.
- _2026-11-03_ — §10 added and completed: **real local-AI verification** (PR #334, workflow
  `.github/workflows/real-local-ai-verification.yml`). Candidate `#323+#328+#329+#327+#333` ⇒
  tree `abeb762322…` (single alembic `0069` head; reconstruction conflict-free). After three
  diagnosed-and-fixed workflow defects — (a) the embedding_space pytest step shared `DATABASE_URL`
  with the app so conftest force-dropped the app DB; (b) the post-restart live-chat check lacked
  diagnostics/retry; (c) the citation match used a non-existent `document_id` detail key (pre
  false-positive via a len>0 fallback, post false-negative) — run **41666998428** @ `82138cc` is
  **green**: all 8 criteria PASS on real Ollama `all-minilm` + `qwen2.5:0.5b`. §9 updated: real
  local-AI retrieval is now VERIFIED; production model choice is a DECISION, not an engineering
  gap. Migration-ancestry finding recorded in §10 (#333 and #326 both branch `0069` from `0068`
  → merge-time rechain of whichever merges second).
- _2026-11-03_ — §11 added: **executive business workflow** (PR #335, branch
  `claude/mainai-executive-business-workflow` @ `aeb0bc3`). A Founder-facing AI-team layer
  (`app/mainai_business/`) composed over the existing workforce substrate — no new tables, no
  migration (does not touch the pending `0069` head), no provider invocation. Reference
  synthetic e-commerce opportunity assessment runs end-to-end through real delegation/
  verification; the consequential publish step is fail-closed behind an explicit Founder
  approval token. 12/12 tests PASS on real Postgres (8 engine incl. all 7 required security
  properties + 4 router via the real TestClient); 42 passed with the workforce/execution
  regression. Per-task workers are honestly labelled deterministic in-process (not model
  calls) pending gated provider activation; wiring #334's local model in is the stated next
  step. `first_cycle_go` stays false; independent review required before merge.
- _2026-11-03_ — §12 added: **model-backed AI team** (PR #336, branch
  `claude/mainai-model-backed-workers` @ `a802a47`, stacked on #335). Closes the #335 "next
  step": the specialists now do REAL local-model inference via a pluggable `ModelBackedRunner`
  (reuses #334's provider path), with schema-validated outputs, execution receipts, a
  deterministic independent validator (not the model reviewing itself), fail-closed refusal of
  any non-local provider, and honest failure/dependency blocking. Adapter logic 25/25 PASS on
  real Postgres locally (stub local provider); the real Ollama run is a CI job
  (`model-backed-ai-team.yml`). No new migration. first_cycle_go stays false; review required.
- _2026-11-03_ — §12 real-model run **VERIFIED**: workflow run 41701226997 @ `43b465d` green —
  deterministic suite 25/25 + all 15 real-Ollama invariants PASS on `qwen2.5:0.5b`. First
  attempt (run 41700300925) failed only at the acceptance step: running the script by path left
  `scripts/ci` (not the backend root) on `sys.path` → `import app` ModuleNotFoundError; fixed by
  inserting the backend root in the script itself (as `apply_runtime_privileges.py` does). Honest
  detail: the approved publish step's 0.5B output was rejected by the deterministic validator
  (`published=false`) — the independent check working as designed, not a regression.
