# Life (MainAI) — MASTER HANDOFF

**Read this first in any new conversation or agent session.** It is a *pointer index*, not a second
knowledge store. When this disagrees with an authoritative doc, the authoritative doc wins — fix this file.
Keep it short. `first_cycle_go=false` everywhere.

_Rev. 7 — 2026-11-02 — Claude Opus 4.8 (independent). Base: `claude/det-kommer-mer-879lcm` @ `64c07d5b0`._

## 0. Standing invariants (never violate without explicit Founder say-so)
No merge · no deploy · no DNS/STRATO · no paid/live provider calls · **no paid-provider data egress** · no
Recall · no autonomous GO · don't modify frozen candidates · don't overwrite another agent's branch ·
independent reviewer required (no self-cert) · `UNKNOWN != VERIFIED` · `REPO-WRITE ≠ PROVIDER-SPEND ≠ EGRESS`.

## 1. Founder scope (authoritative — not open questions)
Life = the Founder's **persistent digital partner & overarching intelligence**; coordinates specialist
agents and **future companies**, acts within delegated authority, communicates naturally, gains progressive
**local-first** coding/operational independence. **Business, finance, communication, multiple companies and
other Founder-defined domains are already in scope.** The full vision is **broader than the repo docs** and
must be **discovered + reconciled from authorized historical sources**, not invented from a summary.
**Absence from Git ≠ nonexistence** — history may live in uploads/Library/Vault/DB/external Founder files.
**Priority: whole-project comprehension before extensive new development.**

## 2. Authoritative documents (read these; don't re-summarize here)
**START HERE → `docs/LIFE_MASTER_BLUEPRINT.md`** — the shared, source-linked entry point to the whole
intended system (vision, maturity, dependency graph, roadmap, open decisions; marked INCOMPLETE where the
historical corpus is not yet reachable). **For getting Life running: `docs/LIFE_FIRST_STARTUP_READINESS.md`**
— the operational runbook (readiness verdict, offline-vs-model split, branch integration order, first
blocker, Founder-supervised startup checklist). Then:
Vision/memory: `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` · Readiness: `docs/MAINAI_STARTUP_READINESS.md`
· Autonomy: `docs/MAINAI_V1_GOAL_TO_AUTONOMY.md` + `docs/FIRST_AUTONOMOUS_TASK_BLOCKER_MAP.md` · Local-first:
`docs/MAINAI_LOCAL_INTELLIGENCE_SCHOOL.md` · Vault/egress: `docs/LIFE_VAULT_EGRESS_CONTROL.md` · Branch map:
`docs/BRANCH_REGISTRY.md` · **Full recon (A–I):** `docs/LIFE_TAKEOVER_READINESS_RECON.md` · Local-first
ingestion demo + first blocker: `docs/LIFE_LOCAL_FIRST_INGESTION_DEMO.md` · Governance: repo `CLAUDE.md`.

## 3. Verified facts (this session; corrected Rev. 2)
- **Runtime status (Founder fact):** last Founder-used version = **v0.1** (messaging worked, **upload did
  not**, **no historical corpus ever provided**). Current ~40-PR system **never started end-to-end with the
  Founder** → not a Founder-tested runtime. First supervised startup + verified small-file upload must
  precede real historical comprehension. Missing corpus is **expected**, not lost. See startup runbook.
- **Startup audit (default `64c07d5b0`, Docker unavailable in review env):** minimal run (login, navigate,
  message, upload one small file, durable store + sha256, extract) = **READY-pending-boot**; offline
  **retrieval is BLOCKED** (`index_document` needs embedding before any chunk; only offline embedder = Ollama,
  whose dim ≠ hardwired `embedding_dim=1536`); large-file/resumable UI not on default (#311–#315). Messaging
  send/receive stores offline; assistant replies need a model.
- Platform mature: 44 backend pkgs, 55 models, 68 migrations, 198 test files, 62 RLS policies.
- **Ingestion→memory (default branch @ 64c07d5b0):** immutable content-addressed originals + durable
  restart-safe worker **built & verified**; extraction text+A/V built (image-OCR likely gap); chunking +
  provenance built; claim extraction + objective grounding + concept-reconciliation built **for documents**;
  retrieval-with-provenance built. **`memory_source_unit` unifies provenance across documents AND messages
  (S1C built)** — BUT **message→claim extraction (S4) is NOT wired** and there is **no ChatGPT/export
  importer**. **Large-file lane is NOT on default** (cursor PRs #311–#315/#320). Memory layers 4–8 +
  automatic relationship discovery partial/manual.
- **Egress (corrected this session):** default-deny gate **is** wired into Safe Planner **+ RAG embed**
  (`embed_with_policy`: ingest/retrieve/media/library) **+ chat** (`chat_with_fallback`), all via `owner_id`.
  The real gap — on-host local calls ledgered as external disclosures — is **fixed in PR #326**
  (`decision='local'`, migration 0069, checkable no-egress invariant).
- **Extraction fidelity:** text/md/html/PDF-text-layer/A+V ✅; **DOCX tables/nested/headers/footers now
  extracted (PR #327, draft)**; **remaining GAPS: scanned/image PDF → empty text (no OCR); standalone
  images; DOCX text boxes/footnotes (detected+reported, not extracted).**
- **`embedding_dim=1536` hardwired** → a real local embedder needs a schema migration + Founder decision (blocker).
  **Why it is coupled (verified Rev. 3):** an HNSW index (`document_chunks_embedding_hnsw_idx`,
  `vector_cosine_ops`, migration 0004) sits on the `vector(1536)` column; pgvector ANN indexes require a fixed
  dimension, so one column can't serve two model sizes with ANN. All good local embedders are 768/1024-dim.
  *Model identity+dim* can be made configurable **non-destructively** (additive columns + fail-closed guard —
  Blueprint §14.3); the *width change* (→1024) is a one-time Founder-gated re-embed migration (vectors must be
  recomputed). Recommended local models (validate on Founder data via ScandEval/MTEB-sv): embedding **bge-m3**
  or **multilingual-e5-large** (both 1024) / KBLab Swedish (768); chat **Qwen2.5-7B** / Gemma-2-9B / Llama-3.1-8B
  / GPT-SW3 (Swedish-native). See Blueprint §14.
- **CI reality (verified Rev. 3, 2026-11-01):** **#323 (`04b27e9`) is fully green and ready (not draft),
  MERGEABLE/CLEAN.** #326/#327 fail **only** the 3 checks #323 fixes (E2E Playwright, E2E same-origin proxy,
  npm audit); their backend/RLS/alembic checks pass → their red is base-inherited, not a regression. Fresh
  local regression (Postgres 5433 + pgvector 0.8.6): #326 egress+locality+restart-demo = **26 passed**; #327
  `tests/backend/rag/` = **407 passed / 1 skip / 2 env-only fails** (both reproduce on untouched base — a
  host-UTC+1 timezone artifact + a Docker-only test; zero #327 regressions).
- **Provider-key pre-verification missing** (invalid-but-set key → silent `failed`).
- **Background non-blocking:** two durable lease job systems (`knowledge_import_jobs`/worker.py;
  `mainai_jobs`/jobs service) + `InteractiveIsolation` = built.
- Autonomy proven on a disposable mirror (#205), NOT the real checkout.
- **Source-to-memory architecture (Rev. 4, verified read-only @ `0a265ba` — Blueprint §16):** the storage +
  provenance substrate is genuinely strong and tested (content-addressed write-once originals, hash-verified
  dedup/self-repair, cross-owner-safe purge via SECURITY DEFINER, immutable MemorySourceUnit lifecycle,
  decision-vs-proposal gate with human-review-only promotion). Lifecycle test run: **278 passed, 1 fail**
  (the one fail is a Docker-entrypoint infra test, not a lifecycle regression). The verified gaps are all
  *missing connections, not broken foundations*: (a) **no original-download route for non-media sources and no
  Life-facing original-retrieval tool** (Founder+Life cannot fetch a whole text/PDF/DOCX original); (b)
  message→claim (S4) unwired + `ClaimRelationship` never populated (trust.py reads edges nothing creates); (c)
  the multi-agent workforce verifier (`BUILDER_CANNOT_SELF_VERIFY`) is a built framework driven by the gated
  executive loop, not wired into document analysis. Smallest next milestone + acceptance criteria: Blueprint §16.6.
- **Shared-original retrieval (Rev. 5) — IMPLEMENTED, PR #329 `830ab4d`, draft/review-pending.** Closes §16's
  sharpest gap: `GET /api/library/{source_id}/original` (founder) + `load_original_as_life()` (Life), one shared
  enforcement point `app/library/originals.py::load_original` — owner-scoped, sha256 integrity gate, octet-stream
  +attachment+nosniff, audited, no storage-key leak, **no provider call / no egress** (reading ≠ disclosure). Off
  base `64c07d5`, no migration, independent of #323/#326/#327/#328. Full matrix `tests/backend/rag/
  test_original_retrieval.py` = **16 passed**; regression green except the one pre-existing host-TZ `ops_status`
  test (fails identically on untouched base). Not self-certified, not merged. Contract: Blueprint §16.6.
- **#329 pre-review audit (Rev. 6, verified read-only @ `830ab4d`) — two honest reviewer concerns, no code
  changed this round (review-gated; author cannot self-certify):**
  1. **Whole-original buffering, not streamed (performance/memory).** `load_original` does
     `handle.read()` into one `bytes` (`originals.py:153`), then the endpoint returns `Response(content=…)`
     (`routers/library.py`, a second full copy) — so one `/original` request holds the original ~twice in
     memory transiently, ×concurrency. **Bounded today** by the hard `MAX_UPLOAD_BYTES = 60 MB` upload cap
     (`routers/library.py:69`), and it matches the existing `/media` endpoint's pattern, so it is not *worse*
     than the established convention — but it does not scale and is the one thing the long-term design must
     change. `LocalFsStorage.open_read` already returns a seekable `BinaryIO`, so a `StreamingResponse` is
     architecturally available. **Design tradeoff to resolve in the streamed version, not paper over:** the
     current full-buffer re-hash (`originals.py:162-164`) closes the verify→open TOCTOU by hashing *all* bytes
     before returning any; a streamed version must hash incrementally and can only *abort the connection*
     mid-stream on a late mismatch (bytes already sent cannot be recalled) instead of returning a clean 503.
     Recommend: keep buffered ≤ a small threshold for the clean-error guarantee, stream above it, documented.
  2. **Life path is a helper, NOT yet an executable tool (authority/delegation).** `load_original_as_life`
     has **no caller anywhere** except its own module + test — grep-verified. The Founder HTTP endpoint is the
     only live caller of `load_original`. So "Life can retrieve originals" is true only as an *authorized
     capability that exists*; it is **not** registered in any executive/workforce tool surface, has no
     delegation/authority wiring, and no worker invokes it. A backend helper is not an operational Life tool —
     tool-registration + bounded-delegation is a separate, later, reviewed step (do not claim Life operationally
     retrieves originals yet).
  Otherwise the required trust properties verified present in code: owner-scope (`uploaded_by == owner_id`
  + `deleted_at IS NULL`, RLS backstop), integrity gate before any byte, revoked/deleted/not-found all 404
  (indistinguishable), one audit row per access with no storage_key in the detail, no provider call/egress.
- **Rev. 7 (2026-11-02) — disposable integration candidate + STRATO sizing + streaming remediation design (no
  production change, nothing merged):**
  - **Integration candidate (local, disposable, NEVER pushed/merged):** worktree `examiner/wtintegration`,
    branch `examiner/integration-firststart`, = base `64c07d5` + #323 + #328 + #329 + #327, **stack-merged with
    ZERO conflicts** (tip `3ce5377`; #326 deliberately kept out — its egress lane stays in independent review).
    This is the no-model first-start footprint. **Integration-test PASS (NOT a container-runtime PASS):**
    `test_basic_startup_no_model` + `test_original_retrieval` + `test_openapi_schema` = **23 passed**; full
    `tests/backend/rag` on the combined tree = **423 passed, 1 skipped, 2 failed** where both fails are the
    SAME two pre-existing environmental artifacts (`test_ops_status…` host-TZ; `…_via_docker_entrypoint`
    Docker-less) that fail identically on untouched base; the `-k "storage or library or document"` subset =
    **274 passed, 1 failed** (only the same `test_ops_status…` host-TZ artifact) — **zero integration-introduced
    regressions** across both runs. A full
    `docker compose up` runtime PASS remains **unverified — Docker is unavailable in this clone**; no E2E boot
    was run, none is claimed.
  - **STRATO VPS feasibility (verified from `docker-compose.vps.yml` + `docs/STRATO_VPS_DEPLOY.md:51`):**
    decisive fact — **Postgres does NOT run on the VPS** (external Supabase Session Pooler); images are built in
    CI and only pulled by digest (no on-box build). So the VPS runs only caddy+frontend+backend+worker+redis
    (Valkey, tmpfs, no disk). **Configured `mem_limit` ceilings (measured from the compose, NOT runtime RSS):**
    caddy 128m · frontend 512m · backend 512m · worker 384m · redis 128m = **~1664 MB of limits** (+ a one-shot
    `uploads-init` 64m that exits); cpus ceilings sum 3.25 (oversubscribed on 2 cores, acceptable for one light
    founder). **Disk 90 GB = ample** (no local DB; only images ~1–2 GB + the `lifeai_uploads` volume + capped
    logs). **RAM is the binding constraint:** 1.66 GB of limits + host OS/Docker/page-cache (~0.3–0.5 GB) sits
    right at the 2 GB ceiling — **Option A (current 2 GB) can boot a supervised single-founder no-model start
    only with swap and/or trimmed limits, with no headroom** for concurrent large downloads, backups, or image
    pulls. **Recommended: Option B (STRATO VPS L, 8 GB / 4 vCore)** — comfortable headroom + operational
    reliability for an actually-usable durable install, still no local AI. **Option C (16 GB) is premature** —
    only justified once local embedding+chat inference (Blueprint §14; a 7B Q4 model alone wants ~6–8 GB) runs
    on the box, which is explicitly a separate later concern. *These are estimates from configured limits, not
    measured usage — the Founder (or an authorized operator) should confirm with `docker stats --no-stream`,
    `free -h`, `df -h`, `docker system df` on the running stack (read-only; see the §7 checklist).*
  - **#329 streaming remediation — smallest safe design (NOT implemented; conflicts with #329's active review
    lane, so proposed only, as its own later reviewed PR):** the integrity guarantee does NOT have to weaken to
    bound memory, because the store is **content-addressed + write-once**: a blob's bytes never change in place
    (the only mutation is purge/delete), and `open_read` holds an fd (`O_NOFOLLOW`) so an in-flight download
    keeps serving the correct inode even if the blob is purged mid-stream (POSIX unlink-after-open). Therefore:
    (1) keep the existing **pre-stream `verify()`** (a full streaming hash of the stored blob, holds nothing in
    RAM) as the authoritative fail-closed gate — storage corruption → 503, zero bytes sent; (2) **small files
    (≤ a threshold, e.g. 1–4 MB): keep today's buffered path** for the clean-503 TOCTOU re-hash guarantee;
    (3) **large files (threshold…60 MB cap): `StreamingResponse`** yielding bounded chunks (e.g. 64–256 KB),
    dropping per-request memory from O(file)≈2× to O(chunk) — the real DoS/resource-exhaustion win for
    concurrent downloads; hash incrementally and, on the by-construction-impossible content mismatch, abort the
    connection (no clean EOF) rather than trust wrong bytes. **Auth/revocation/owner checks unchanged** (run
    before verify/open; revoked/deleted → 404 before any byte). **Audit:** write the row at stream start (access
    authorized + verify passed), so a client disconnect still records the access. Not a correctness bug in #329
    (it is already bounded by the 60 MB cap + fail-closed); this is a scale/DoS-hardening optimization.

## 4. Exact PR / commit state (2026-11-01)
- Default `claude/det-kommer-mer-879lcm` @ `64c07d5b0`; no open PR merged into it yet.
- **Exam verdicts (posted on each PR): #319 `397672d84` PASS · #320 `6c1240d8` PASS · #321 `e1f649a9` PASS
  · #322 `a7c1ffa3` PASS** (single mig heads 0100/0101; full suites 3545/3530/3555/3561, 0 genuine fails).
- **#324 `306b980f4`** `codex/coordination-ledger-dedup-takeover` (parent #322; mig 0102) = **active Codex
  CC3/CC4/AR6**. Inspect only; do not duplicate/modify; sequence after #322.
- Claude: **#327** (DOCX extraction completeness — tables/nested/headers/footers, **draft @ `69cdaf342`**,
  review required; rag regression 409 pass/1 skip), **#326** (egress local/external distinction + local-first
  ingestion demo, **draft @ `3a07f58b0`**, review required; migration 0069 single head; 1193+1-skip regression
  green), **#325** (recon + handoff +
  `LIFE_MASTER_BLUEPRINT.md`, draft, docs-only, @ `0aaa419`), **#323** (npm-audit baseline + CI Playwright
  pin, @ `04b27e9`, **now fully green & ready/not-draft**), **#328** (`claude/basic-startup-no-model-proof`,
  **draft @ `fa840e4`**, test-only off base, review required — executable proof that upload/extract/persist +
  restart survival work with NO embedding/chat model; 2 passed locally), #316/#317/#318 (passkeys/PWA). Deep
  cursor/* stack #285–#315 unmerged. (BRANCH_REGISTRY predates #323–#328 — reconciling it is the tracked M1
  zero-risk docs task, §7.)

## 5. Genuinely open Founder decisions (block M2; recon §H)
1. **Designate ONE authorized source** for the first ingestion proof (existing Library/Vault upload, or a
   new export e.g. ChatGPT data export) — I will not assume access.
2. Confirm **local-first/offline embedding (no egress)** for the first proof.
3. Confirm the **ingestion/comprehension proof (M2)** as the first supervised demonstration.

## 6. Ownership
Claude (this lane): exams #319–#322 (done, PASS); recon + handoff (#325). Codex: #319/#321/#322 builder +
**#324 active**. Cursor: autonomy/first-cycle/large-file/messaging lanes (#285–#315). Don't overlap.

## 7. Next safe action
**Rev. 7 lead — practical path to the first REAL supervised startup:** (a) assign the independent review below
and **merge #323 first** (it is the only non-draft/green PR and unblocks #326/#327); (b) the no-model
integration candidate is proven at the integration-test level (worktree `examiner/wtintegration` @ `3ce5377`,
base+#323+#328+#329+#327, 23+423+274 passing, 0 new regressions) — the remaining gap to a *real* start is a
**`docker compose -f docker-compose.vps.yml up` boot in an authorized Docker environment** (unavailable here);
(c) the first boot needs the external Supabase Postgres creds + a domain + GHCR pull token already documented
in `docs/STRATO_VPS_DEPLOY.md`. **Founder read-only STRATO sizing checklist** (run on the existing 2 GB VPS to
replace the estimate with measured numbers — none of these change anything): `free -h` (total/used/available
RAM + swap), `nproc` (cores), `df -h /` + `docker system df` (disk + image/volume/log usage), and, once the
stack is up, `docker stats --no-stream` (per-container real RSS vs the mem_limit ceilings). If RAM `available`
under load is thin or swap is heavy → size up to **VPS L (8 GB)**; if comfortable → 2 GB can serve a single
founder's no-model start. **Independent-review queue** — confirmed again 2026-11-02 (all PRs draft except #323,
**zero reviews on every PR**, nothing merged, SHAs unchanged):
(base `64c07d5`): **#323** `04b27e9` (green/ready — review+merge FIRST → turns #326/#327's three red checks
green), **#327** `69cdaf3`, **#326** `3a07f58`, **#328** `fa840e4`, **#329** `830ab4d` (shared-original
retrieval — independent of the other four, review in parallel; see the §3 Rev. 6 pre-review audit for its two
reviewer concerns). **First usable Library = a no-model integration candidate** (base + #323 + #328 + #329,
#327 optional) demonstrating login → conversation persistence → small-file upload → immutable original storage
→ Library browse → **original download (#329)** → extraction → restart recovery. No AI model needed (#328
proves the model-independent path at the service-test level, 2 passed). A throwaway integration *can* be built
without merging to the default branch (merge the candidates into a disposable integration branch/worktree off
base), but a full **`docker compose up` runtime PASS is still environment-blocked — Docker is unavailable in
this review clone**, so no end-to-end boot has been run and none is claimed. **Frontend "Download original"
spec (minimal, review-ready, not yet implemented — would be its own small reviewed PR):** (a) `lib/api.ts` add
`getLibraryOriginalUrl: (id) => \`${API_URL}/api/library/${id}/original\`` (mirror of `getLibraryMediaUrl`);
(b) `app/(shell)/library/[id]/page.tsx` header block render `<a href={api.getLibraryOriginalUrl(source.id)}
download>` gated on the original being present (status past the pre-storage phase). Auth needs nothing new:
the endpoint is GET + HttpOnly same-origin session cookie, exactly like the existing `<audio src>` media URL
(no CSRF header needed for GET). Backend already fails closed (founder-only, owner-scoped, 404 otherwise).
Then: (2) Independent review of **#327 then #326** (package + fresh evidence in `LIFE_FIRST_STARTUP_READINESS.md`
§7). (3) Founder picks the **local embedding +
chat models** (Blueprint §14) → unblocks the configurable-embedding PR (§14.3, additive/non-destructive,
sequenced *after* #326's `0069`) and then the one-time re-embed migration. (4) Supervised `docker compose up`
dry-run of the startup checklist (runbook §5) in an authorized env. No 4th PR was stacked this round, by
design (don't dilute review; don't collide with `0069`; the offline blocker is a model decision, not a bug).

**Priority (per the Founder's refinement): verified sources + Blueprint first, then build.** Blueprint
(`LIFE_MASTER_BLUEPRINT.md`, incl. §11 source audit, §12 comprehension gap analysis, §13 next-blocker +
acceptance criteria) and the local-first ingestion demo (#326) are delivered. **Source-access audit (§11):
no authorized historical source is reachable in the review environment** — only disposable `examiner_*` DBs
and `.env.example` templates. **Immediate path = first supervised startup** (runbook `LIFE_FIRST_STARTUP_READINESS.md`): assign
independent review in order **#323 → #327 → #326**, then run a supervised `docker compose up` dry-run of the
startup checklist in an authorized env (Docker unavailable in the review clone). Decisions that gate further
comprehension work: **(i)** that independent review; **(ii)** choose a **local embedding model** → turns the
`embedding_dim` blocker into a bounded migration (unblocks offline retrieval); **(iii)** designate ONE
authorized source (Blueprint §11 A or B); **(iv)** choose a local-first *chat* model (claim extraction needs
one) or authorize a classified egress. The historical-ingestion gate remains
**Blueprint §10 Q1 — designate ONE authorized historical source** so the ⟨access⟩-marked corpus can actually be
ingested and the vision verified (today it is not reachable from Git). Extraction-fidelity build units,
each its own reviewed PR: **✅ DOCX tables/headers/footers done (#327)**; remaining, in order —
**(1)** scanned-PDF/image OCR *(needs a local OCR-engine decision — local-first)*, **(2)** standalone image
ingestion, **(3)** full extraction of detected-but-unsupported DOCX constructs (text boxes/footnotes),
**(4)** configurable `embedding_dim` (needs Founder model decision), **(5)** S4 message→claim + export
importer. No implementation without established scope + authorization.
M1 (reconcile `BRANCH_REGISTRY` ↔ real PR graph) remains a zero-risk docs step that can proceed now.
