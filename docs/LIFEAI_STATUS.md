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

**Publication stage completed — full end-to-end VERIFIED (run 41711441543 @ `019309e`, green,
all 23 invariants PASS).** The rejection above was then closed without weakening validation:
the publication prompt hands the model the exact synthesis recommendation to copy verbatim, and
a **bounded, authorized correction loop** re-prompts the same task with the validator's reason
(budget 2), re-validating from scratch — exhaustion leaves the task REJECTED (no fabricated
publication; proven by `test_unrecoverable_invalid_publication_stays_rejected`). In the latest
real run `qwen2.5:0.5b` produced a valid publication first try (corrections=0): the workflow
reaches `status="completed"` with a persisted `final_deliverable`, the Founder view reassembles
the same completion from the DB, and output-quality failures are distinguished from
authorization (approval) failures (`failure_kind`). The Founder API now exposes the complete
workflow state (per-task status/runtime/receipt/correction_attempts/failure_kind, approval
requirements, workflow status, final deliverable). Added commits on the #336 branch:
`60b48e5` (completion + bounded recovery), `019309e` (no-duplicate-request resume + view dedupe).

---

## 13. Independent certification — coordination PRs #300 / #301 / #303 / #314 [3 PASS, 1 FAIL w/ existing fix]

**PR #337** · `docs/COORDINATION_CERTIFICATION_300_301_303_314.md` + reproducible examiner
probes in `docs/examiner_probes/`. Founder chose to certify the existing coordination
infrastructure first (not build a parallel supervisor). Each candidate examined at its exact
frozen SHA in a detached worktree, Python 3.12, real Postgres + RLS; no frozen candidate
modified.

- **#300** `2c9dd7e0` **PASS** — read-only GitHub intake: GET-only, rate-limit/forbidden →
  BLOCKED with named capability, forged/replay/stale/impersonation rejected, idempotent
  obligations, never claims delivery/publication. 14 candidate tests + 6 examiner probes.
- **#301** `a786dac9` **FAIL (DEF-1/GH-4)** — cold GitHub discovery (no pre-existing
  `AgentWorkAssignment`) is not durable (`_persist_ledger` early-returns at zero rows);
  obligations lost on reload, recreated on refresh. Reproduced executably. 5 own tests pass;
  the defect is the blocker. **Already remediated by #303** → no new repair warranted.
- **#303** `74ebda97` (body) **PASS** — `persist.py` adds a synthetic owner-scoped
  ledger-backing row; cold discovery survives reload, 2nd discovery reuses the same row (no
  duplicate). 10 own tests + fix-verification. ⚠️ branch head drifted to `296de97f` — re-freeze
  before merge.
- **#314** `3aa2bec8` **PASS** — `run_authorized_operating_cycle` = the full dev-management loop;
  SPEND-STOP at the spend-reservation chokepoint + kill-switch make it fail-closed (0
  obligations, no spend under STOP); interactive Founder work never starved. 13 own tests + 2
  probes.

**Integration base (Phase 2):** lineage A (durable coordination / GitHub intake / delivery /
handover → #303, supersedes #301) + lineage B (provider integration / operating cycle → #314).
**Single missing integration:** #314's base lacks #303's intake + `persist.py`, so the operating
cycle's task selection is not yet fed by real GitHub state in one durable ledger. Smallest
coherent integration = compose #303's intake+persist onto #314's base and wire
`run_authorized_operating_cycle`'s task selection to it — specified, not performed, because both
are frozen cursor-owned candidates (no silent modification / no duplicate work). `first_cycle_go`
stays false.


### Phase 3 — GitHub-fed operating cycle implemented [PR #338, executed 3/3 + 24 regression]

Certification found #314 **descends from #303** (`merge-base(#314,#303)=74ebda97`, #303 0 ahead)
— so #314 already contains the GitHub intake + `persist.py` AND the operating cycle; **no
cross-lineage merge is needed**. The only missing wire was that `autonomous_cycle` did not
consume `github_intake`. PR **#338** (`claude/coordination-operating-cycle-github-fed`, stacked
on #314) adds `agent_coordination/github_fed_cycle.py::run_github_fed_operating_cycle` — a
composition of existing PUBLIC primitives (no new framework): `load_owner_ledger →
GitHubDevelopmentIntake.refresh (read-only) → persist_owner_ledger → load_owner_ledger →
run_authorized_operating_cycle → founder_coordination_snapshot`. Life now selects its next task
from real observed PR/CI/review state instead of the Founder hand-copying it. Executed on real
Postgres: `test_github_fed_operating_cycle.py` 3 passed (end-to-end GitHub-fed selection;
restart recovery + no duplicate assignment; provider-unavailable pauses only GitHub while the
cycle continues), 24 on-branch regression, ruff clean. Read-only, `first_cycle_go=false`.
**Independent review required — author does not self-certify.**
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
- _2026-11-03_ — §12 **publication stage completed**: the earlier validator rejection of the
  0.5B publication is resolved without weakening validation — verbatim-recommendation prompt +
  a bounded, authorized correction loop (budget 2; exhaustion stays REJECTED, never fabricated).
  Full end-to-end real-model run 41711441543 @ `019309e` green (all 23 invariants): the workflow
  reaches `completed` with a persisted `final_deliverable`, reassembled identically from the DB.
  Fixed a view bug where a blocked + later-approved request for the same consequential task made
  the reassembled view read `awaiting_approval` (engine now reuses the open request; view dedupes
  per task_key). Commits `60b48e5`, `019309e` on the #336 branch. first_cycle_go stays false.
- _2026-11-04_ — §13 added: independent certification of coordination PRs (PR #337). Founder
  chose to certify existing infra first (not build a parallel supervisor). Verdicts from real
  tests at each frozen SHA: #300 PASS, #301 FAIL (DEF-1/GH-4 reproduced; fixed by #303), #303
  PASS (DEF-1/GH-4 repair; head drifted past body SHA — re-freeze), #314 PASS (operating cycle +
  SPEND-STOP fail-closed). Integration base = #303 + #314; single missing integration (compose
  #303 intake/persist onto #314 operating-cycle base) specified, not performed — both are frozen
  cursor-owned candidates. first_cycle_go=false.
- _2026-11-04_ — Phase 3 implemented (PR #338): certification showed #314 descends from #303
  (no cross-lineage merge needed); added `github_fed_cycle.run_github_fed_operating_cycle`
  wiring the operating cycle's task selection to read-only GitHub intake (composition of
  existing public primitives). 3 integration tests + 24 regression pass on real Postgres.
  Read-only; first_cycle_go=false; independent review required (no self-cert).
- _2026-11-04_ — Priority 1/2 (PR #339, `claude/coordination-authorized-dispatch`, stacked on
  #338): added `agent_coordination/authorized_dispatch_step.py::dispatch_or_wait` — the dispatch
  step after task selection, composing existing public primitives (evaluate_dispatch_readiness +
  get_real_adapter + dispatch_assignment) into: real dispatch when a provider is founder-enabled,
  else durable WAITING_FOR_PROVIDER (ready→blocked, named capability) + resume. HONEST: no
  supported agent (claude-code/cursor-agent/codex) is enabled/installed here, so a real external
  round-trip is unavailable and never simulated; even an "enabled" provider without an argv
  template fails closed to REAL_PROVIDER_NOT_CONFIGURED via NotConfiguredAdapter. 4 tests + 14
  regression pass on real Postgres (no injected adapter). Blocker: real round-trip needs a
  separately-reviewed Agent Runtime + founder-enabled installed CLI. Independent review required;
  first_cycle_go=false.

## 14. Founder Alpha — Blueprint draft (items 8/9) [PR #340, 3/3 on real Postgres]

Founder decision: make Life usable by the Founder. Items 1–7 of the Founder Alpha workflow are
already verified end-to-end by #334's real-Ollama run (41666998428 @ 82138cc): sign-in,
model-backed chat, conversation persistence across restart, upload + immutable-original
preservation, index + retrieval with source refs, cross-owner isolation. Founder-only original
download = #329/#330. The remaining gap — items 8/9 (Blueprint draft) — is delivered by PR #340
(`claude/founder-alpha-blueprint-draft`, stacked on #333 @ fdf3647).

`app/blueprint/service.py::generate_blueprint_draft` composes RAG retrieval (source-grounding) +
the active local model (synthesis) + a new owner-scoped `blueprint_drafts` store — no new
retrieval/model/archive system. The draft is versioned (immutable version per regeneration),
source-traceable (`source_document_ids`/`source_refs` → immutable originals; ungrounded refused),
an AI interpretation separated from originals (`provenance.originals_mutated=False`), and always
`status="draft"` — only an explicit `approve_blueprint_draft` (Founder action) promotes it; Life
never self-approves. Migration `0070_blueprint_drafts` is additive/reversible, single head off
`0069` (merge-note for #326's own 0069 included); RLS owner-isolation in app/rls.py.

Executed on real Postgres (`test_blueprint_draft.py`, 3 passed; migration 0070 applied + RLS):
versioning, source-traceability, draft-pending-approval + explicit approval, originals-not-mutated,
ungrounded-refused, cross-owner RLS; migration single-head + reversible (upgrade→downgrade→upgrade).
Real-local-model synthesis runs in the #334-style Ollama CI; a stub synthesizer is used for the
local composition tests (same split as #336), never claimed as the real-model PASS.

**Founder Alpha status:** items 1–7 PASS (#334; download #329/#330); items 8/9 PASS (PR #340, real
Postgres). Remaining: assemble ONE Founder Alpha candidate (this + #334 lineage + #329/#330) and
run the whole workflow once under real Ollama end-to-end. Independent review required (no
self-cert); first_cycle_go=false.

### 14a. Blueprint API + UI wired into ONE integrated candidate [PR #340 @ e3f4e3c — 6/6 e2e on real Postgres; review required]

Per the Founder directive to stop accumulating disconnected PRs and assemble one executable
candidate, PR #340 was EXTENDED in place (no new PR) with the missing Blueprint API + UI and an
end-to-end verification. Why this branch already is the candidate: its Alembic chain is linear
(`0069_embedding_space_identity` #333 → `0070_blueprint_drafts`) and it already carries
`ollama_provider`, the RAG ingest/retrieve/vector_store, and the library router incl. Founder-only
original retrieval (`get_source_media`) — so no cross-lineage merge and no Alembic-conflict
resolution were required (single head confirmed).

- **Added:** `backend/app/routers/blueprint.py` (5 routes, registered in `app/main.py`) reusing
  `app.rag.retrieve.retrieve_context` + `resolve_active(db,"chat")`; `frontend/app/(shell)/blueprint/page.tsx`
  + `lib/api.ts` types/methods + Sidebar link.
- **Trust boundary (router-enforced):** whole router `require_founder`-gated; source identity never
  client-supplied (derived from retrieval over the founder's own docs); approval identity =
  authenticated founder, never the body; ungrounded → 422; provider failure → 503 with no partial row.
- **Verified** `tests/backend/test_founder_alpha_blueprint_e2e.py` (6) on REAL Postgres/pgvector +
  RLS + real storage records: full chain (auth→chat→indexed sources→retrieval→generate→inspect
  refs→new version→durable re-read/restart→retrieve draft+original→explicit approve; v1 immutable;
  originals' checksums unchanged), ungrounded-refused, cross-owner isolation + founder-only 403,
  forge-protection, synthesis-failure rollback, DB-enforced `(owner,title,version)` uniqueness.
  Full local run 15 passed (+#340's 3, openapi-schema, smoke); ruff clean.
- **Honest model-leg scope:** chat/embedding provider is the repo's sanctioned deterministic fake
  (monkeypatched `OpenAIProvider`, never a real key), identical to `test_chat_source_grounding.py`
  and the #334/#336 split; real local model (Ollama) proven in the model-backed CI, not on this box
  (no Ollama). No real-model PASS is claimed here.
- **Incidental finding:** the #333/0069 active-embedding-space guard excludes chunks lacking an
  `(embedding_model, native_dim)` stamp, which silently breaks the pre-#333
  `tests/backend/chat/test_chat_source_grounding.py` — to be updated separately; this e2e suite
  stamps chunks as the real ingest path does.
- **Frontend note:** no local `node_modules`, so tsc/eslint run in CI; page follows repo conventions.
- **Remaining:** run this candidate once under real Ollama end-to-end (extend #334's workflow to
  invoke the Blueprint step). Boundaries unchanged: no merge/STRATO/paid/destructive-migration/prod
  data; first_cycle_go=false; independent review required (not self-certified).

### 14b. Founder Alpha final integration verification [candidate @ 1b38597 — real-Ollama CI + full static checks; review required]

The complete, launch-candidate work (extends PR #340 in place; no new PR). All on the candidate
branch `claude/founder-alpha-blueprint-draft`.

- **Complete candidate (deterministic reconstruction):** `#323 trunk (04b27e9) + #328 (fa840e4) +
  #329 (830ab4d) + #327 (69cdaf3) + live #340 head`. Merges cleanly (verified locally), single
  0069 + single 0070 head (no #326 multi-head), blueprint router + #329 `/original` both present.
- **Migration chain (item 5):** `alembic upgrade head` clean on real Postgres (0066→…→0069→0070);
  0070 reversible (downgrade→upgrade restores table + unique index); additive, non-destructive,
  single head. Fixed a real gap: 0070 now applies `blueprint_drafts` RLS **in the migration**
  (ENABLE+FORCE+policy, matching the 0060 precedent and the exact `app/rls.py` expression), which
  repairs `test_migration_roundtrip` (previously the policy was only in `app/rls.py`, so a
  downgrade→upgrade round-trip dropped and did not restore it).
- **#333 source-grounding regression (item 4):** root cause = the active-embedding-space guard
  (`app/rag/vector_store.py`) scopes retrieval to the active `(embedding_model, native_dim)`; the
  real ingest path stamps every chunk (proven by the real-Ollama chunk-identity assertion), but
  three pre-#333 test fixtures inserted unstamped chunks and then exercised retrieval
  (`test_chat_source_grounding`, `test_chat_context_status::test_successful_retrieval_has_no_context_status`,
  `test_workbench` analyze). Fix = stamp those fixtures with the active space as production does —
  the guard is unchanged; added an explicit test that a legacy NULL-identity chunk is NOT mixed
  into an active-space query (isolation stated, not hidden; remedy for real legacy data =
  re-index). Other unstamped-chunk tests that never retrieve (erasure/purge/claims/RLS/memory) are
  intentionally left unchanged.
- **New CI (items 1–2):** `.github/workflows/founder-alpha-verification.yml` — reconstructs the
  complete candidate, stands up **real Ollama (all-minilm + qwen2.5:0.5b) + pgvector + filesystem**,
  and runs the full chain (founder login → real-model chat → upload → real embed/index → grounded
  retrieval → **generate Blueprint with the real model** → inspect refs → save v2 → restart →
  retrieve original + draft → **Founder approves**), plus the chunk embedding-space identity
  assertion and pre-serve pytest (embedding-space + grounding + blueprint unit + e2e). No paid
  provider reachable (empty fallback chain).
- **Local evidence:** 123+ passed on real Postgres/pgvector (migration round-trip, grounding,
  context_status, workbench, embedding-space isolation, blueprint unit + e2e, openapi).
- **Frontend static checks (item 3):** `tsc --noEmit` clean, `eslint .` 0 problems, `next build`
  succeeds (`/blueprint` route prerendered). Fixed one real eslint error in the Blueprint page
  (`react-hooks/set-state-in-effect`) by loading await-first in the mount effect.
- **CI results — candidate head `8fabade` (PASS on all in-scope checks):**
  - `Founder Alpha verification (real local)` run **41849139859 = SUCCESS** — the complete chain
    under REAL Ollama + pgvector + filesystem, every criterion PASS: authentication; real_model_chat
    (provider=ollama); upload_original; real_embed_and_index (chunks=1); grounded_retrieval (cited
    the uploaded doc); **blueprint_generate_real_model (v1 draft, refs matched, provider=ollama,
    model=qwen2.5:0.5b)**; blueprint_inspect_refs; blueprint_new_version (v2); immutable_original
    (SHA-256 match); restart_persistence (original+draft survived); **founder_approval (approved_by
    set)**; approval_is_idempotent_guarded (409); plus the embedding-space identity assertion
    (all-minilm / native 384 / stored 1536). (Prior run 41844888898 failed only because the
    pre-serve step wrongly ran fake-provider API tests in the Ollama env — fixed in 8fabade.)
  - `CI` run **41844888832 @ 1b38597** — all in-scope jobs SUCCESS: Backend unit/integration,
    Backend account-lifecycle, Alembic migration check, RLS & session-security, Frontend
    TypeScript & ESLint, Frontend build (vercel + docker). The only failing jobs are the two
    PRE-EXISTING out-of-scope ones: `npm audit` (the `next`-pinning issue, own branch per CLAUDE.md)
    and the Playwright E2E jobs (chromium does not install on the runner image). Neither is caused
    by this candidate.
- **Item 6:** independent review requested at the frozen SHA (no self-cert) — PR #340 review-request
  comment lists the auth/RLS/concurrency/provenance/source-refs/model-failure/approval dimensions.
- **Item 7:** all in-scope checks pass → executable startup + deployment proposal delivered in
  `docs/FOUNDER_ALPHA_STARTUP_AND_DEPLOYMENT.md` (exact SHA, commands, host requirements, storage
  persistence, backups, restore test, URL strategy, and the honest remaining deployment blockers —
  chiefly: Ollama is not yet in the VPS topology, and a production model decision is still open).
- **Known pre-existing (out of scope, flagged):** `npm audit` CI job (the `next`-pinning issue, per
  CLAUDE.md handled on its own branch); Playwright chromium-install fails on the runner image
  (infra); one pre-existing F841 (unused `founder_id`) in an unrelated `test_workbench` function
  (backend ruff is not a CI gate). None are introduced by this candidate.

---

## 15. Anti-coordination-failure requirements — acceptance-criteria mapping [code-grounded; reuse, not rebuild]

Founder directive (2026-11-04): Life must structurally prevent the coordination failures that force
the Founder to re-remind/re-correct/re-coordinate — system-wide, all capabilities. Per the
directive's own items 5–6 this is handled by **reusing the established architecture** (security
kernel, persistent memory, owner ledger, scheduling, `agent_coordination`, `mainai_execution`
lessons/recovery, `rag/trust`) — **no new orchestration framework, no duplication, ownership
preserved**. These requirements are recorded here as **acceptance criteria for existing work**.
Each row was verified against the actual code (not inferred from conversation), with the owner of
any gap named rather than reassigned.

**Fresh executable re-verification (candidate `8fabade`, real Postgres): 87 passed** across
`test_mainai_lesson_from_founder_correction`, `test_lesson_from_verification`,
`test_lesson_guard_observations`, `test_mainai_execution_recovery[_dedup|_takeover]`,
`test_provider_spend_api`, `test_founder_alpha_blueprint_e2e`, `test_chat_source_grounding`. The
pure coordination-ledger properties were certified earlier (§13 + PRs #337/#339) on Codex #324's
lineage and are cited, not re-run here (not this candidate's tree; not my lane).

### Item 7 — the nine required properties
| Required property | Existing implementation (verified) | Evidence | Owner / status |
|---|---|---|---|
| Does **not reassign** an active task | `agent_coordination` DB-fenced leases + `DuplicateWorkError`/`canonical_work_key` | §13 (#303/#314 PASS); examiner probes | Codex #324 — COVERED |
| Does **not repeat** a completed task | owner-ledger obligation idempotency; `create_work_assignment` dup-guard. Founder-Alpha analog: Blueprint regenerate = new immutable version, approve idempotent-guarded (409) | §13; `test_founder_alpha_blueprint_e2e` | Codex #324 + #340 — COVERED |
| **Preserves state across restart** | execution reconciliation/recovery; Blueprint + original durable | real-Ollama run 41849139859 (restart_persistence PASS); `test_mainai_execution_recovery*` | #340 + Codex — COVERED |
| **Detects stale / contradictory** info | `rag/trust.py` active_truth_status capping + `detect_conflicts` (`contradicts`) | `test_chat_source_grounding` (historical/disputed capping, conflicts_detected) | #340 lane — COVERED |
| Handles **unavailable providers** without losing assignments | `authorized_dispatch_step` → durable `WAITING_FOR_PROVIDER` + `resume_waiting_for_provider`; `provider_spend` SPEND-STOP | PR #339 (4 tests); `test_provider_spend_api` | Codex #324 — COVERED |
| **Schedules** authorized work using real availability | `adapter_config.adapter_availability` + `evaluate_dispatch_readiness(require_adapter_enabled=True)` + `github_fed_cycle` | PRs #338/#339 | Codex #324 — COVERED (see PARTIAL note below) |
| **Remembers Founder corrections** | `mainai_execution/lessons.record_lesson_from_founder_correction` (durable verbatim quote + root_cause/fix/general_rule/applies_to/regression_test) | `test_mainai_lesson_from_founder_correction` | #340 lineage — COVERED |
| **Prevents recurrence** of fixed errors | `lessons.apply_lessons_to_verification_plan` injects a lesson's `regression_test` into the matching task_type's verification plan | `test_lesson_from_verification`, `test_lesson_guard_observations` | #340 lineage — COVERED |
| **Requires approval** for sensitive actions | Blueprint approve-only draft→approved (`approved_by` = authenticated founder); `provider_spend` authorization; `recovery_approval` | `test_founder_alpha_blueprint_e2e`, `test_provider_spend_api`, `test_mainai_execution_recovery*` | #340 + Codex — COVERED |

### Items 1–4 (structural) — status
- **Persistent awareness/continuity (1):** owner ledger + obligations + `founder_coordination_snapshot`
  + execution state machine + GitHub-fed status — COVERED (Codex #324; §13).
- **Prevent repeated mistakes (2):** the 7 pre-assignment checks map to lease/dup-guard/ledger +
  the lesson loop above — COVERED; "durable corrective record + regression test" = `EngineeringLesson`.
- **Time-aware scheduling (3):** dispatch-readiness + adapter availability COVERED; **PARTIAL —**
  explicit provider *quota / reset-window* modeling ("known reset times", "unknown stays unknown")
  is not a first-class persisted concept yet. **Gap for the owner of `provider_spend` / coordination
  (Codex #324)** — recorded here, NOT reassigned, NOT implemented by me (outside my scope; would
  duplicate active work).
- **Continuous improvement (4):** lessons + versioned migrations + rollback + reproducible CI —
  COVERED; no uncontrolled changes (gates intact).

### Conclusion for my authorized scope (#340 Founder Alpha + security remediation)
Every acceptance criterion that applies to Founder Alpha is already met and freshly re-proven
(approval, restart persistence, no-silent-repeat via versioning, RLS isolation, stale/contradiction
detection). **No genuinely-uncovered behavior exists within my scope**, so — per the directive —
no new code was written here; this is the required evidence record. The one PARTIAL (provider
quota/reset-window) is flagged for its existing owner (Codex #324) without creating a duplicate
assignment. Security/deployment gates intact; `first_cycle_go=false`.

---

## 16. Blueprint approval security remediation — two P1 findings closed (DB-enforced) [candidate e4f7c6e; exact-SHA CI verified; independent review required]

Founder correction (2026-11-04): the Founder Alpha #340 report had not demonstrated closure of two
previously identified P1 security findings. Both are now closed with **database-enforced**
guarantees (not the Python service alone), reusing the repo's established pattern (guard trigger +
owner-scoped SECURITY DEFINER function + s1a_privilege_policy grant narrowing). Corrected frozen
SHA for independent security review: **`e4f7c6e`** (branch `claude/founder-alpha-blueprint-draft`).

**P1-1 — the runtime role (`mainai_app`) could forge approval state / modify approved content /
delete a protected record.** Closed by migration `0071_blueprint_approval_guard`:
  * `trg_blueprint_drafts_approval_guard` (BEFORE UPDATE/DELETE, fires for every writer): an
    approved draft is immutable and undeletable; a draft→approved transition must set
    approved_at+approved_by and must not alter content; status may only move draft→approved.
  * approval runs only through `blueprint_approve_draft(uuid,text)` SECURITY DEFINER
    (search_path=pg_catalog, schema-qualified); `scripts/security/s1a_privilege_policy.py` narrows
    `blueprint_drafts` to `SELECT,INSERT` for `mainai_app` (no direct UPDATE/DELETE) + EXECUTE on
    the function — so in production the runtime role cannot write approval state by raw SQL at all.

**P1-2 — concurrent Founder approvals were not transaction-safe.** Closed: the approval function
takes `SELECT ... FOR UPDATE` then re-checks status inside the lock, so concurrent approvals of the
same draft serialize and exactly one wins; the loser sees `approved` and is rejected.

**Reproducible security evidence** (`backend/tests/backend/test_blueprint_approval_security.py`):
approved-row immutable + undeletable (trigger); forgery without audit fields rejected;
content-tamper-on-approve rejected; concurrent approvals serialized on the row lock (lock_timeout)
+ second rejected; happy-path approval; and — with the real s1a policy applied — `mainai_app` loses
UPDATE/DELETE and a direct UPDATE/DELETE raises `InsufficientPrivilege` while EXECUTE on the function
remains. Local: 11 security+unit passed; 20 passed across privilege-floor + migration-roundtrip +
openapi (no regression); migration 0071 single-head + reversible.

**Exact-SHA GitHub Actions verification @ `e4f7c6e`:** `CI` run **41888768194** — **Backend
unit/integration = success** (runs the security suite + migration roundtrip + privilege floor at
this SHA), plus RLS & session-security, Alembic migration check, account-lifecycle, Frontend
TypeScript & ESLint, Frontend build (vercel + docker) all success. The security pytest also passed
in the `Founder Alpha verification (real local)` run **41888768153** pre-serve step. (Only the two
pre-existing out-of-scope CI jobs fail: `npm audit` next-pinning; Playwright chromium-install infra.)

**Non-security follow-up (flagged, scope-disciplined — NOT fixed here):** in run 41888768153 the
live real-Ollama `blueprint_generate_real_model` step flaked with a 503 (ProviderError: the
qwen2.5:0.5b synthesis call did not return within the 120s provider timeout on the CPU runner). The
SAME generate code passed at `8fabade` (run 41849139859), and this commit changed only the approval
path — so this is a transient real-model robustness characteristic, not a security regression and
not caused by this change. A bounded-synthesis-prompt / longer-timeout hardening is a reasonable
follow-up for the Blueprint feature, deliberately deferred here per the directive's "complete the
security remediation before expanding scope." Security approval does not depend on a real-Ollama
PASS ("a real-Ollama PASS alone is not security approval").

**Boundaries:** no merge, no deploy, no STRATO, no Codex #324 lane touched, no paid provider.
`first_cycle_go=false`. Not self-certified — independent security review requested at `e4f7c6e`.

---

## 17. Founder Alpha #340 completion — generate fix + boot directive; frozen SHA `d13efd7`

Closes the remaining Founder Alpha #340 items on top of the §16 security remediation.

**1. Real-Ollama Blueprint generate 503 — fixed (smallest change, architecture preserved).** Cause:
the qwen2.5:0.5b synthesis call exceeded the 120s provider default on a CPU runner because the
prompt could carry up to 8 sources × 800 chars (the smaller chat prompt had succeeded in the same
run). Fix in `app/routers/blueprint.py`: the synthesis PROMPT now uses only the most-relevant few
sources (`_SYNTH_PROMPT_MAX_SOURCES=6`), each trimmed (`_SYNTH_EXCERPT_CHARS=400`), with a generous
model timeout (`_SYNTH_TIMEOUT_SECONDS=240`). `source_refs` still records ALL grounded sources —
traceability unchanged; only the model INPUT is bounded. Still real-model, still grounded, still
ungrounded-refused — no new system.

**2. Re-verified at an immutable SHA via GitHub Actions.** `Founder Alpha verification (real local)`
run **41903171671 @ `d13efd7` = SUCCESS** — every criterion PASS, including
`blueprint_generate_real_model` (v1 draft, refs matched, provider=ollama, model=qwen2.5:0.5b),
`blueprint_new_version`, restart persistence, and `founder_approval` (approved_by set; re-approve
409). `CI` run **41903171882 @ `d13efd7`**: in-scope jobs green (Backend account-lifecycle, Alembic
migration check, RLS & session-security, Frontend TypeScript & ESLint, Frontend build vercel +
docker); backend unit/integration carries the §16 security suite (green at `e4f7c6e`). Only the two
pre-existing out-of-scope jobs fail (`npm audit` next-pinning; Playwright chromium-install infra).

**3. Founder Alpha boot directive recorded** in the authoritative architecture doc
(`docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`): Life's first objective is to become a functioning,
secure, persistent MainAI — not to finish all of LifeOS — plus what Life must do on/after startup
(understand the project from authorized sources; distinguish planned/implemented/tested/approved/
deployed; one unified overview + propose priorities; cooperate while challenging wrong assumptions;
eventually coordinate agents without duplicate assignments via the existing `agent_coordination`;
respect the security kernel, Founder authority and approval boundaries; help build the rest
incrementally). Reuses existing architecture; no new orchestration system.

**4. Frozen corrected #340 SHA = `d13efd7`** — evidence prepared for independent security review
(PR #340 review-request comment lists the dimensions; §16 for the P1 remediation, this section for
the generate fix + boot directive).

**5. Status before a controlled Founder Alpha startup.**
COMPLETE (verified): Blueprint API + UI; the full real-model chain end-to-end
(auth→chat→upload→index→retrieve→generate→inspect→version→restart→retrieve→approve) at `d13efd7`;
the #333 source-grounding regression; migration chain single-head + reversible; frontend
tsc/eslint/build; the two P1 security findings (DB-enforced, CI-verified); the boot directive.
REMAINS before a controlled startup: independent security review of `d13efd7` (no self-cert, no
merge); Ollama added to the VPS topology + a production model decision (see
`FOUNDER_ALPHA_STARTUP_AND_DEPLOYMENT.md`); the original-archive backup→restore→integrity cycle
before any local-file deletion. Boundaries: no merge/STRATO/deletion/paid; Codex #341 untouched;
`first_cycle_go=false`.

---

## 18. Independent security review — PR #341 / #324 coordination integration [PASS, SHA-bound]

Independent examination (not self-certification for the #324 content — I did not author it; I did
author #339's authorized_dispatch_step, noted below) of **PR #341** "Integrate #324/#339 with the
frontend CI baseline", frozen SHA **`95c32168860b2b8a1ccd4126d4a59ea410a1f4ed`**, head
`codex/coordination-324-339-ci-integration`, base `claude/frontend-npm-audit-baseline-consolidation`.

Method (GitHub-first; no clone/worktree/broad local suite): git blob-SHA equality vs the already-
certified #303/#314/#339; targeted reads of changed + new files at the frozen SHA; reproducible CI
evidence from #341's own passing CI run.

**VERDICT: PASS** at `95c32168`.

1. **Preserved by construction (byte-identical blob SHAs vs certified #339/#314/#303):**
   `agent_coordination/{authorized_dispatch_step,dispatch,lifecycle,github_intake,__init__}.py` —
   fail-closed dispatch gate + durable WAITING_FOR_PROVIDER, recovery/reconciliation, read-only
   GitHub intake — carried unchanged. (authorized_dispatch_step/dispatch/github_fed were my #339
   work; byte-identical, examined earlier.)
2. **Changed (integrated from #324): `durable.py` `persist.py` `autonomous_cycle.py` `rls.py`** —
   owner-scoping intact (17/21/14 owner_id references; owner-ledger + ledger-backing semantics
   retained); no cross-owner path introduced; rls.py keeps every existing isolation policy.
3. **New owner-scoped tables (migration 0100 `authorized_operating_cycles`, `provider_webhook_events`)
   — DB-enforced isolation correct:** ENABLE + FORCE RLS + owner policies (USING + WITH CHECK on
   `owner_id = current_setting('app.current_user_id')`); composite-FK owner integrity on every FK;
   `provider_webhook_events` is append-only (BEFORE UPDATE/DELETE deny trigger; `mainai_app` gets
   only SELECT,INSERT); REVOKE ALL FROM PUBLIC + minimal grants.
4. **New external-input boundary (Cursor webhook: `provider_webhook_ingress.py` +
   `external_connectivity/cursor_cloud.py` + router `/cursor/{owner_id}`) — authenticated,
   replay-safe, binding-scoped, untrusted-until-verified:** HMAC-SHA256 over raw bytes with
   `hmac.compare_digest` (constant-time) + user-agent + `sha256=` format; rejects missing-secret/
   mismatch. Path `owner_id` is "lookup scope only — HMAC + durable assignment binding remains
   authority". Row-locked (`with_for_update`) owned-cycle lookup (concurrency-safe); replay/forgery
   detection (delivery_id + body-hash mismatch rejected); status allowlist + timestamp window
   (stale/future rejected); result ingested as UNTRUSTED via the broker + independent verification.
5. **Adversarial evidence present + GREEN at the frozen SHA:** `test_rt3_persistent_cycle_webhook.py`
   (single-fenced worker claim + restart-reclaim; concurrent-claim serialization;
   authenticated-event-never-bypasses-independent-verification; replay_ignored) passed in #341's own
   **CI run 41893411066** (Backend unit/integration + RLS & session-security + Alembic migration
   check + account-lifecycle all success).

**Residual boundaries flagged for the owner (not blockers):**
- Single global `cursor_cloud_webhook_secret` (not per-owner); authority rests on HMAC + the durable
  owned-cycle binding + untrusted-until-verified. Adequate for a single-founder deployment; revisit
  per-owner secrets if multi-owner webhooks are ever enabled.
- `operating_cycle_runtime.py`, `external_connectivity/`, `transports/`: the webhook path and the
  cycle-authority assert were examined; an exhaustive line-by-line of all runtime/transport
  internals was bounded by the GitHub-first/targeted mandate. No defect found on the examined paths.

Boundaries: #341 unmodified; no merge/deploy/STRATO; Codex's #340 review untouched; `first_cycle_go=false`.

---
