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
- **Offline retrieval is blocked** by a hardwired `embedding_dim=1536` coupled to an HNSW
  index vs. the only offline embedder's differing dimension — needs a Founder-gated re-embed
  migration + a local-model choice. **[DECISION]**
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
2. **Offline retrieval**: `embedding_dim` vs. local embedder — re-embed migration + model
   choice. [DECISION]
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
  verified candidate tree `ebdced820…`. **Embedding-dependent retrieval is NOT yet verified**
  and remains blocked on the `embedding_dim`/local-model decision.

**Next active implementation:** the §8 first-start verification workflow (in progress). After
that, no new workstream is opened without Founder direction.

---

## Corrections log

Append a dated line here whenever a prior conclusion in this file is corrected. Never delete a
superseded conclusion silently — supersede it here and in the body.

- _2026-11-03_ — Initial version. Baseline established from verified Git/GitHub state and prior
  session evidence.
- _2026-11-03_ — §8 filled in with the first real run of the first-start verification workflow
  (run 41548691356): all 7 acceptance criteria PASS at candidate tree `ebdced820…`,
  model-independent path. §9's "can it be verified?" updated from PENDING to VERIFIED-yes for
  that path (embedding-dependent retrieval still blocked). Earlier draft's `document_extraction`
  was conservatively BLOCKED on an earlier run because `blocked_provider` was unclassified; it
  is now correctly PASS (extraction completed; embedding paused by design) — see the workflow
  commit history.
