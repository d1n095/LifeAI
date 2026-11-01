# Life — First supervised startup readiness (operational runbook)

Operational companion to [`LIFE_MASTER_BLUEPRINT.md`](LIFE_MASTER_BLUEPRINT.md). Goal: get Life
running again safely with the Founder, with working messaging and reliable small-file upload, then
source-grounded historical understanding. **This is a runbook, not a vision doc.** Evidence is from
a read-only audit of the default branch `claude/det-kommer-mer-879lcm` @ `64c07d5b0`. `first_cycle_go=false`.

_Rev. 1 — 2026-11-01 — Claude Opus 4.8 (independent). No merge/deploy/egress/spend/Recall/autonomy._

## 0. Historical fact (per Founder, 2026-11-01)
Life **v0.1** was the last version the Founder actually used: **basic messaging worked; file upload
did NOT; no historical corpus was ever successfully provided.** The current ~40-PR system has had
**substantial GitHub development but has never been started/tested end-to-end with the Founder.** So
the absence of historical files is expected — the task now is a first supervised startup, not a search
for files that were never uploaded. Current candidates are **not** equivalent to a Founder-tested runtime.

## 1. Startup readiness — levelled, not one boolean (Deliverable #1)
| Capability | Verdict | Evidence |
|---|---|---|
| Boot (postgres + redis + backend + frontend) | **READY-pending-boot** | `docker-compose.yml` services + healthchecks; `main.py` on_startup applies RLS/privileges and **auto-bootstraps the Founder** (`bootstrap_founder_user` from `FOUNDER_EMAIL/PASSWORD`); single migration head `0068`. Not container-verified here (Docker unavailable in the review env). |
| Authentication / Founder access | **READY-pending-boot** | bootstrap founder + `routers/auth.py`; passkeys are a separate opt-in (#316–#318). |
| Frontend load + navigation | **READY-pending-boot** | Next.js `app/(shell)/*` (chat, library, documents, …); browser → same-origin `/api/*` proxy (`INTERNAL_API_URL`). |
| Messaging — send/receive (storage) | **READY offline** | `routers/chat.py` + `conversations.py`; `chat/page.tsx` stores user msg + assistant reply as two messages. Storing the Founder's message needs no model. |
| Messaging — **assistant reply** | **NEEDS A MODEL** | `chat_with_fallback` → a chat provider (local Ollama or paid). Offline only if Ollama runs. |
| Upload one small file → durable store + integrity hash | **READY offline** | `POST /api/documents/upload` (chunked read, content-addressed `{sha256}`, atomic write→fsync→rename, idempotent); frontend `lib/api.ts` FormData + `UploadQueueProvider`. |
| Extract content accurately | **READY offline** | `rag/extract.py` (text/md/html/PDF-text-layer) + DOCX completeness (#327) + A/V (`media_import`). |
| **Retrieve content with provenance** | **BLOCKED offline** | `index_document` **requires embedding before any chunk is written** → no embedding ⇒ no `DocumentChunk` ⇒ no retrieval (semantic *or* text). Embedding needs a model. |
| Preserve state across restart | **READY (DB-backed)** | durable content-addressed store + append-only provenance + lease-fenced workers; retrieval reads durable rows (proven in PR #326's restart demo with a stub embedder). |
| Large-file / resumable upload UI | **BLOCKED / not on default** | the large-file lane is PRs #311–#315 (unmerged); default upload is single-POST (server-side chunked read + a client queue), fine for *small* files. |
| Run with **no paid AI** (local-first) | **BLOCKED (decision needed)** | Ollama infra exists (compose `--profile local-llm`, `OllamaProvider`), but the only offline embedder emits a vector whose dimension ≠ the hardwired `embedding_dim=1536` → see §4. |

**Overall:** a **minimal supervised run — login, navigate, exchange messages, upload one small file,
preserve it durably with its hash, and extract it — is READY pending a real boot.** A **source-grounded,
fully-offline run (retrieval + assistant replies with no paid AI) is BLOCKED** on the local-embedding
decision (§4). "READY-pending-boot" is honest: green unit tests ≠ a verified deployed app, and Docker
was unavailable here to prove the container path.

## 2. Messaging & upload end-to-end status (Deliverable #3)
**Code-wired, not yet runtime-verified with the Founder.** Backend APIs, frontend UI, and the same-origin
proxy for both messaging and multipart upload are all present and consistent (see table above). The repo's
Playwright **E2E harness exists** (`frontend/e2e`, `playwright.*.config.ts`) but currently **fails in CI for
an environment reason unrelated to the app** — base `ci.yml` runs E2E on `ubuntu-latest` (now Ubuntu 26.04),
where Playwright has no Chromium build; **PR #323 fixes this** (pin to `ubuntu-24.04`). Backend unit/
integration tests pass. A true end-to-end pass therefore needs either #323 on the base or a local
`docker compose up` run (Founder-side; see §5).

**Confirmed 2026-11-01 (Rev. 2):** PR #323 (`claude/frontend-npm-audit-baseline-consolidation` @ `04b27e9`)
is now **fully green in CI** — `All required checks passed`, including *E2E Playwright (full stack)*, *E2E
same-origin proxy*, and *npm audit* — and is **ready (not draft), MERGEABLE, mergeStateStatus CLEAN**. On
#326 and #327 the **only** failing checks are exactly those three (E2E Playwright, E2E same-origin proxy, npm
audit); their *Backend unit/integration*, *RLS & session-security*, and *Alembic migration* checks all
**pass**. Direct comparison therefore confirms #326/#327's red checks are **base-inherited pre-#323
failures**, not regressions — they go green once #323 is in the base.

## 3. Minimum branch/PR integration sequence (Deliverables #2) — dependency-ordered
> Do not merge/rebase blindly; preserve frozen SHAs; each needs independent review. Migrations: base head
> `0068`; #326 adds `0069`. No rebase ahead of a dependency actually merging (repo `CLAUDE.md`).

1. **#323** — frontend npm-audit baseline + CI Playwright `ubuntu-24.04` pin (`04b27e9`). *First*: turns
   E2E/npm CI green so every later PR is trustworthy; no migration; no app-logic change. **Now fully green in
   CI, ready (not draft), MERGEABLE/CLEAN** — the one PR that is review-ready for integration today.
2. **#327** — DOCX extraction completeness (no migration; independent of #326). *Review → merge.*
3. **#326** — egress local/external distinction + migration `0069` (local-first no-egress invariant). *Review
   → merge after 0068.* Low risk, backward-compatible.
4. **Local-embedding decision + `embedding_dim` migration** (§4) — *required for offline retrieval*; needs a
   Founder model choice, so it is a decision+PR, not yet written.
5. *(optional for MVP)* **#316 → #318** passkeys/PWA (#318 depends on #316); installable/app polish.
6. **Deferred past the MVP:** large-file lane **#311–#315** (resumable large uploads; adds migrations),
   coordination **#319–#322/#324** (autonomy/coordination — not needed for a supervised small-file startup).

## 4. First genuine startup blocker (Deliverable #4)
**Fully-offline (no-paid-AI) indexing/retrieval is blocked by a vector-dimension mismatch.** `index_document`
must embed chunks before it writes any `DocumentChunk`, and the embedding column is hardwired to
`settings.embedding_dim = 1536` (OpenAI's size). The only on-host embedder, Ollama, serves models whose
dimension differs (e.g. `nomic-embed-text` = 768), so a real local embed cannot be stored without a schema
migration — and **choosing the local model is a Founder decision** (model → dimension → one-way re-embed).
Root cause: a single global pgvector dimension + a paid default provider. (If a paid embedding key is
acceptable for the *first* supervised run, this is **not** a boot blocker — OpenAI's 1536-dim embeddings work
as-is; but that contradicts the local-first / no-paid-AI goal, so it needs an explicit Founder call.)
Everything up to and including extraction + durable preservation works offline regardless.

## 5. Founder-supervised startup checklist (Deliverable #6)
Run in an authorized environment (not this review clone). Nothing below enables autonomy, egress, or spend.
1. Create `.env` from `.env.example`: set `SECRET_KEY`, `FOUNDER_EMAIL`, `FOUNDER_PASSWORD`,
   `FRONTEND_ORIGINS`/`PUBLIC_APP_URL`; leave paid provider keys **unset** for an offline run.
2. `docker compose up postgres redis backend frontend` (migrations auto-apply; founder auto-bootstraps).
   For offline AI add `--profile local-llm` and `docker compose exec ollama ollama pull <model>` — **but
   resolve §4 first**, or accept that indexing/retrieval won't run offline yet.
3. Open the frontend; **log in** as the Founder. ✔ auth + navigation.
4. **Send a message**; confirm it is stored and the conversation persists. (Assistant reply only if a model
   is configured.) ✔ messaging.
5. **Upload one small text/PDF/DOCX file.** Confirm the Document row, the content-addressed original, and the
   `sha256`. ✔ durable storage + integrity.
6. Confirm **extracted text** matches the original (DOCX: tables/headers too, with #327). ✔ extraction.
7. If a model is configured: confirm the document reaches `indexed` and a query **retrieves it with
   provenance** (document/title + score). Without a model it will sit on `awaiting_provider` — expected. ✔/⏸
8. **Restart the backend**; confirm the file, messages, and any index survive and retrieval still works. ✔ durability.
9. Record results; only then proceed to real historical files (§ Blueprint §11 access procedure).

## 6. Next safe executable step (Deliverable #8)
**Assign independent review of #323 → #327 → #326, then do a supervised `docker compose up` dry-run of the §5
checklist in an authorized environment** (Docker is unavailable in the review clone, so the container path is
unproven here). In parallel, the Founder decides the **local embedding model** (Blueprint §14) so §4 can be
turned into a bounded migration PR — the one change that unblocks fully-offline retrieval. No further code is
landed here: no boot-blocking code defect was found, and the remaining blocker is a design decision, not a bug.

## 7. Independent-review package + fresh local evidence (Rev. 2, 2026-11-01)
Author cannot self-certify (repo `CLAUDE.md`; no independent reviewer available in this session). This is the
**review package + evidence**, not a PASS. Review order **#323 → #327 → #326**; all three base on
`claude/det-kommer-mer-879lcm` @ `64c07d5b0` (base migration head `0068`), all MERGEABLE.

| PR | Branch @ SHA | Migr. head | CI (required) | Local regression here (Postgres 5433 + pgvector 0.8.6) |
|---|---|---|---|---|
| **#323** | `claude/frontend-npm-audit-baseline-consolidation` @ `04b27e9` | none | **all green**; ready/not-draft | n/a (frontend/CI) |
| **#327** | `claude/docx-extraction-completeness` @ `69cdaf3` | none | backend/RLS/alembic **pass**; red only on the 3 #323-fixed checks | `tests/backend/rag/` = **407 passed, 1 skipped, 2 failed**; both failures reproduce on untouched base `64c07d5b0` and are **environmental**: one is a host-timezone artifact (recency assert off by exactly 3600 s — host is UTC+1, CI is UTC), one needs Docker (`..._via_docker_entrypoint`). **Zero #327 regressions.** |
| **#326** | `claude/egress-local-provider-no-egress-ledger` @ `3a07f58` | **0069** (single head after 0068; CHECK-widen for `decision='local'`) | backend/RLS/alembic **pass**; red only on the 3 #323-fixed checks | egress + provider-locality + cross-session restart demo = **26 passed** (incl. the restart-persistence proof, §P3). |

**Security invariants to confirm in review:** #326 never relaxes a deny path (NEVER_EGRESS + incomplete-request
still hard-deny for local), `is_external_egress` defaults True (fail-safe: a provider is external unless it
proves loopback/in-deployment), and the `local` decision still requires `sent_content_hash` NOT NULL (CHECK).
#327 treats documents as untrusted, runs no macros, is bounded/deterministic, and reports (never silently
drops) detected-but-unsupported constructs. **Integration blocker:** #323 must merge first; then #327 and #326
rebase and their three red checks go green. #326's `0069` is the only migration and must sequence after `0068`.
