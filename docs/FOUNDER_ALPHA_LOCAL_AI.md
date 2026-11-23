# Founder Alpha — local-first AI (Ollama), operation and constraints

Operational reference for running MainAI's chat and retrieval on a **local** model runtime
(Ollama) with no external egress. It documents what the code guarantees, how to verify it on a
real machine, and the one model-selection constraint that is easy to get wrong.

This is not a second retrieval system: local embeddings use the existing pipeline
(`app/rag/ingest.py` → `app/rag/vector_store.py`), the existing embedding-space identity
(`app/rag/embedding_space.py`), and the existing chat grounding path
(`app/rag/retrieve.py` → `app/routers/chat.py`). Only the provider calls change.

## 1. Choosing a local embedding model

`document_chunks.embedding` is a **fixed** `vector(EMBEDDING_DIM)` column (1536 by default,
migration 0004). `app/rag/embedding_space.py` lets any model whose **native** dimension is at
most the storage dimension work with **no destructive migration and no re-embedding**: its
vectors are zero-padded up to the storage width on write and on query, which preserves cosine
similarity exactly, and every chunk is stamped with `(embedding_model, embedding_native_dim)`
so retrieval can never compare vectors from two different models.

The rule that follows:

| model native dim | behaviour |
|---|---|
| `== EMBEDDING_DIM` (1536) | stored as-is |
| `< EMBEDDING_DIM` | zero-padded on write, cosine ranking unchanged — **preferred for local models** |
| `> EMBEDDING_DIM` | **refused** (`EmbeddingSpaceError`) — a wider model needs a Founder-gated column-dimension migration |

Practical consequence: a 2560-dimension model (e.g. `qwen3-embedding:4b`) is *not* usable
against the default 1536 column. Prefer models such as `nomic-embed-text` (768),
`all-minilm` (384) or `mxbai-embed-large` (1024). After changing the embedding model, documents
must be re-indexed; chunks from the previous model remain in the table but are excluded by the
active-space filter, so old and new vectors are never mixed.

## 2. Configuration

| Variable | Purpose |
|---|---|
| `OLLAMA_BASE_URL` | local runtime (default `http://ollama:11434`) |
| `DEFAULT_LLM_PROVIDER=ollama`, `DEFAULT_LLM_MODEL` | chat on the local runtime |
| `DEFAULT_EMBEDDING_PROVIDER=ollama`, `DEFAULT_EMBEDDING_MODEL` | embeddings on the local runtime |
| `EMBEDDING_QUERY_PREFIX` | instruction prefix applied to the retrieval **query only** (default empty — see §3) |
| `LOCAL_VERIFICATION_TIMEOUT_SECONDS` | pre-flight budget for local providers (default 120; cloud providers keep the 10 s fail-fast budget) |
| `EMBEDDING_DIM` | storage column width; only a migration changes it |

## 3. Instruction-aware embedding models

Some models are trained asymmetrically: the **query** carries an instruction
(`Instruct: …\nQuery: …`) while **documents** stay bare. Used without it they do not error —
they rank worse, which is the worst failure mode for a knowledge base because nothing looks
broken. Set `EMBEDDING_QUERY_PREFIX` for such a model (e.g. Qwen3-Embedding); leave it empty
for symmetric models (`nomic-embed-text`, `all-minilm`, `text-embedding-3-*`).

The prefix is applied in `app/rag/query_embedding.py` to the semantic query channel only. The
independent text-match channel in `hybrid_search()` keeps the raw query, and stored documents
are unaffected.

## 4. Batched embedding and native widths

`app/providers/ollama_provider.py` embeds a whole batch in one `POST /api/embed` call and
returns vectors at the model's **native** width (padding is the storage layer's job, §1). This
matters on CPU runtimes: one request per document batch instead of one per chunk is the
difference between usable ingestion and a per-chunk crawl.

An Ollama older than 0.1.36 (no `/api/embed`, whose 404 is plain text) is handled by a fallback
to the legacy per-text `/api/embeddings` endpoint. A JSON `model not found` 404 is **not**
treated as an old server — it stays a classified `unsupported` failure.

## 5. Health contract

`GET /api/health` reports `local_model` **only when a role resolves to a local provider**;
cloud-only deployments see the exact payload they always did.

```json
{"status": "ok", "worker": "alive",
 "local_model": {"runtime": "ollama", "reachable": true,
                 "configured_models": ["llama3.1", "nomic-embed-text"],
                 "missing_models": []}}
```

- `reachable` is tri-state: `true` (probe answered), `false` (probe failed), `null` (no model
  configured, so nothing was probed).
- `missing_models` is **omitted** when `reachable` is `false` — nothing was checked, so
  claiming which models are missing would be a guess. Absent means unknown.
- A down local runtime never turns a live API unhealthy (no 503) — same principle as
  `worker: "unknown"`, because an API without its model runtime is still serving its other
  routes.

## 6. Frontend: dev-mode hydration

`next dev --hostname 0.0.0.0` viewed through a non-localhost origin (a preview or sandbox host)
blocks the cross-origin `/_next/webpack-hmr` request. React's `hydrateRoot()` then never
resolves its initial server response: the page **renders but stays unhydrated** — no event
handlers, `AuthGuard`'s client never initialises, login silently does nothing. It looks like an
application bug and never appears in `next build`.

Fix (in `frontend/next.config.mjs`): name the dev origin.

```bash
NEXT_ALLOWED_DEV_ORIGINS="pv-example.style.dev" npx next dev --hostname 0.0.0.0 --port 3000
```

`allowedDevOrigins` applies to `next dev` only, so production is unaffected.
`frontend/scripts/check-dev-origins.mjs` (run by CI, with and without the variable) fails the
build if that wiring is ever dropped.

## 7. Verifying a real local deployment

Run against a machine where the backend and the local runtime actually run:

```bash
# 1. readiness (must be reachable: true, missing_models: [])
curl -s localhost:8000/api/health | jq .local_model

# 2. ingest a document, then confirm it is indexed
#    (worker log should show no provider pause; document status `indexed`)
# 3. semantic search: the passage you ingested should rank first
curl -s "localhost:8000/api/library/search?q=<question>" -H "X-CSRF-Token: …" | jq '.results[0], .semantic_search_available'

# 4. retrieval-backed chat: the reply should cite the ingested source
curl -s localhost:8000/api/chat -H 'content-type: application/json' \
     -H "X-CSRF-Token: …" -d '{"message":"<question>"}' | jq '.assistant_status, .sources'

# 5. runtime outage: stop the local runtime, check health reports reachable:false,
#    and that chat fails honestly (assistant_status "failed") instead of retrying silently;
#    restart it, then confirm health and chat recover with no backend restart.
```

Real-Ollama CI (`.github/workflows/real-local-ai-verification.yml`) exercises the same
properties with a throwaway pgvector service container and small models, and fails if the
runtime is unreachable — never falling back to a paid API.

---

## 2026-11-22 — certification addendum: live discovery, reload and evidence


**Branch:** `freebuff/life-workspaces`, based on the Founder Alpha / Personal Work
checkpoint `04de94db7d7ccf51c23dd68670e789e8e9df6cde`.

**Code SHA:** `4f99518010cdf46df764cf6a9e5a5b6539095a06`
**Tree SHA:** `46ea9421d87706269fe605eaec6aefa036e7768f`

No push, pull request, merge, deployment or production VPS change was made. GitHub write
access is not available in this environment. `first_cycle_go=false`.

This round did not start another architecture. It connected and verified the existing
provider registry, streaming chat, durable document worker, pgvector retrieval, workspaces,
recovery paths and browser journeys.

## Implemented in this round

1. **Live local-model discovery** — `backend/app/providers/local_discovery.py` reads Ollama's
   bounded `/api/tags` response, normalizes the implicit `:latest` tag, deduplicates and
   sorts without trusting arbitrary model metadata. A down runtime fails soft instead of
   failing the admin page.
2. **Founder-only discovery API** — `GET /api/admin/providers/local-models` reports installed
   models, the active chat/embedding identity, whether a tag is already in the catalog and
   what capabilities are known. `selection_requires_verification` is explicit: discovery is
   not authorization. Selection still uses the existing verify/activate path.
3. **Active model identity** — `ProviderStatus` now carries `active_chat_model` and
   `active_embedding_model`. The chat header shows the actually configured model rather than
   inferring a model from the last verification, which can refer to a different tag.
4. **Reload-safe conversations** — `/chat` is URL-addressable as
   `/chat?conversation=<id>`. A browser refresh reopens the durable conversation from
   PostgreSQL instead of silently starting a new one.
5. **Admin local-model UI** — the existing founder admin page lists installed local models,
   their size and whether they are catalog-known, and fills the exact tag into the existing
   chat/embedding selection controls. It does not activate a model by itself.
6. **Test corrections, not product weakening** — the V6 journey's exact marker assertion now
   includes the suffix the UI actually renders. The resilience network journey now intercepts
   `/api/dashboard` (the endpoint the dashboard actually calls), and the project creation
   assertion targets the project link instead of matching both a link and a select option.

## Verified existing behavior

The following was already implemented and was re-verified against real PostgreSQL, Redis,
backend and worker processes:

- SSE chat streaming, persisted final messages and idempotent replay.
- Browser cancellation: click **Avbryt** stops the stream, persists an honest terminal state,
  leaves exactly one user message, and survives reload without duplicating it.
- Provider failure and browser disconnect handling without a blind second external call.
- Durable upload → import job → worker indexing → retrieval → cited answer.
- Duplicate upload detection and idempotent re-import.
- Deleted, foreign and legacy-space sources are excluded from chat retrieval.
- Source conflict/confidence classification and claim-level conflict signalling.
- Owner isolation and restart persistence for the cognitive acceptance path.
- Workspace/project/task/milestone/calendar/search/dashboard persistence and navigation.
- Dashboard error surface and recovery after a real failed API request.
- Keyboard-only chat operation and Enter-to-send.
- Fault injection for backend termination, worker termination, Redis loss, PostgreSQL outage,
  provider timeout/malformed response, duplicate job delivery, expired leases and storage
  failure.

No second memory store, vector database, provider router or workspace module was introduced.

## Work-package status

| Package | Status | Evidence / limitation |
| --- | --- | --- |
| 1. Real local AI | **PARTIAL — implementation verified, live model BLOCKED** | Discovery, authorization boundary, active identity, streaming, persistence, reload, cancel, downtime handling and restart recovery are covered. No Ollama runtime is installed (`127.0.0.1:11434` refused), so a real local model was not invoked. |
| 2. Persistent memory and knowledge | **VERIFIED EXISTING** | 13 source-grounding tests plus duplicate import, owner isolation, deletion, legacy-space isolation and founder citation journey. No new memory database. |
| 3. Document pipeline | **VERIFIED EXISTING** | Durable upload/import, crash before/after commit, worker resume, duplicate bytes, empty file, blob cleanup, indexing and retrieval. The browser journey exercises TXT end to end; other format validators were not re-audited this round. |
| 4. Project workspace integration | **VERIFIED EXISTING** | Workspace/project/task/calendar/search/dashboard browser journey and project-detail journey, reload persistence, owner-scoped API and no cross-context selection. |
| 5. Full-stack user journey | **PASS for the covered journey** | Founder login → project → upload → ingestion → cited chat → persisted conversation → related task → reload/restart. See browser evidence. |
| 6. Recovery and concurrency | **PASS for the executed matrix** | 11 fault-injection tests and the reliability suite passed, including duplicate delivery, interrupted stream, DB/Redis outage, provider errors and lease fencing. Retries do not blindly repeat an uncertain provider effect. |
| 7. Performance | **MEASURED, provider-limited** | 45-second isolated soak: chat p50 **57.4 ms**, p95 **121.9 ms**; retrieval chat p50 **64.3 ms**; health p50 **30.1 ms**; RSS **183–186 MB**. Backend used a deterministic fake provider, so this measures application overhead, not real model latency. |
| 8. Endurance | **BLOCKED** | Only 45 seconds ran. F23 requires 14,400 seconds; the result is explicitly `BLOCKED`, never PASS. |
| 9. UX and quality | **VERIFIED / IMPROVED** | URL-backed history, truthful active model label, loading/error states, keyboard chat, dashboard network-error recovery, responsive existing layouts. No cosmetic rewrite. |
| 10. Certification | **PASS locally, external blockers explicit** | 57 backend tests, 13 browser tests, TypeScript, lint, production build and a verified bundle. GitHub CI/PR, real Ollama and four-hour endurance remain blocked. |

## Certification results

| Check | Result |
| --- | --- |
| Focused backend suite | **57 passed, 0 failed, 0 skipped** (JUnit `tests="57"`, `failures="0"`, `errors="0"`) |
| Founder V6 browser journey | **2 passed** |
| Personal work browser journey | **1 passed** |
| Model routing / local discovery browser tests | **2 passed** |
| Resilience browser suite | **8 passed** |
| Total browser journeys | **13 passed, 0 failed** |
| TypeScript | **PASS** |
| ESLint | **0 errors, 1 pre-existing `api.ts` warning** |
| Production build | **PASS**, 29 routes |
| Short soak | **BLOCKED for F23**, 0 request failures |
| Real local Ollama model | **BLOCKED — no runtime** |

The soak used isolated PostgreSQL `lifeos_soak_test` and Redis DB 15. It completed
**24 chats, 24 streams, 4 document imports with grounded retrieval, 8 MainAI jobs, 16
concurrent health checks and one backend restart**. Startup took **4.84 s**; database and
Redis stayed readable at every sample; no incidents, stalled jobs or failures were recorded.
The run deliberately returned exit 3 because its duration was below four hours.

## Blockers

1. **No Ollama runtime**: `127.0.0.1:11434` refuses connections. Real local-model discovery
   with an installed model and a real generation cannot be claimed. This is an infrastructure
   blocker, not a passing local-AI acceptance.
2. **No four-hour endurance window**: F23 is BLOCKED. The 45-second soak only proves the
   harness and recovery path work; it is not endurance evidence.
3. **No GitHub write access**: exact-head CI, a draft PR, merge and deployment cannot be
   performed. The verified Git bundle is the delivery artifact.
4. **No production access**: no production VPS, database or credentials were touched.

## Reproduction

Focused backend suite:

```bash
cd backend
export DATABASE_URL=postgresql://postgres@127.0.0.1:55432/lifeos_workmgmt_test
export APP_DATABASE_URL=postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_workmgmt_test
export REDIS_URL=redis://127.0.0.1:56379/0
export LIFEAI_TEST_DATABASE_NAME=lifeos_workmgmt_test
/tmp/r15-venv/bin/python -m pytest \
  tests/backend/providers/test_local_model_discovery.py \
  tests/backend/chat/test_chat_streaming.py \
  tests/backend/test_documents_upload_durable_delivery.py \
  tests/backend/chat/test_chat_source_grounding.py \
  tests/backend/rag/test_library_import.py::test_reimporting_identical_content_is_idempotent_marks_duplicate_not_a_second_document \
  tests/founder_acceptance/test_founder_acceptance_suite.py::test_scenario_2_import_document_and_cite_source \
  tests/backend/test_v5_fault_injection.py \
  tests/backend/test_v5_reliability.py \
  tests/backend/chat/test_chat_request_idempotency.py \
  tests/backend/test_cognitive_acceptance_e2e.py::test_owner_isolation_failure_handling_and_restart_persistence \
  tests/backend/test_personal_work_management_api.py::test_dashboard_search_restart_persistence_and_query_budget
```

Browser journeys:

```bash
cd frontend
export LD_LIBRARY_PATH=/tmp/lifeai-testenv/root/usr/lib/x86_64-linux-gnu:/tmp/lifeai-testenv/root/usr/lib
export E2E_FOUNDER_PASSWORD='TestFounderPassword123!'
export E2E_INTERNAL_API_URL=http://127.0.0.1:8010
npx playwright test \
  e2e/founder-journey-v6.spec.ts \
  e2e/personal-work.spec.ts \
  e2e/model-routing.spec.ts \
  e2e/founder-alpha-resilience.spec.ts \
  --reporter=list
```

Short soak (expected exit 3 and `f23_status=BLOCKED`):

```bash
cd backend
/tmp/r15-venv/bin/python scripts/acceptance/v5_soak.py \
  --database-url postgresql://postgres@127.0.0.1:55432/lifeos_soak_test \
  --app-database-url postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_soak_test \
  --redis-url redis://127.0.0.1:56379/15 \
  --duration-seconds 45 --sample-every-seconds 10 --restart-every-seconds 20 \
  --concurrency 4 \
  --out ../docs/evidence/founder-alpha-local-ai/soak-short.json
```

## Evidence

`docs/evidence/founder-alpha-local-ai/` contains the JUnit/XML, backend output, TypeScript,
lint, build, all browser transcripts, the soak JSON/text and `summary.json`. The verified
bundle is under `delivery/founder-alpha-local-ai/` with `SHA256SUMS` and a clean-clone
verification transcript.

---

## 2026-11-23 — a real local model was actually run (blocker 1 closed)

**Branch:** `freebuff/life-workspaces`. Base `04de94db7d7ccf51c23dd68670e789e8e9df6cde`.
No push, PR, merge or deployment — GitHub write access is still unavailable in this
environment (no credential helper, no token; `git push --dry-run` fails to authenticate).
`first_cycle_go=false`.

### Why the earlier "BLOCKED" was an install problem, not a code problem

Ollama was genuinely absent (`127.0.0.1:11434` refused, no binary on PATH) and `sudo` is
unavailable. It is installable without root: the repository's own
`backend/scripts/ci/install_pinned_ollama.sh` pins `v0.40.15` and the archive SHA-256
`628c018404a4206493efd38e46251c154ede5141b8b8dc85c659b3b027c47379`, but extracts with
`sudo tar -C /usr/local`. The same verified archive was extracted into a user-writable
prefix instead, so the supply-chain contract (pinned version + verified digest) is unchanged:

```bash
curl -L -o ollama-linux-amd64.tar.zst \
  https://github.com/ollama/ollama/releases/download/v0.40.15/ollama-linux-amd64.tar.zst
printf '%s  %s\n' 628c018404a4206493efd38e46251c154ede5141b8b8dc85c659b3b027c47379 \
  ollama-linux-amd64.tar.zst | sha256sum --check --strict   # OK
mkdir -p /tmp/ollama && tar --use-compress-program=unzstd -xf ollama-linux-amd64.tar.zst -C /tmp/ollama
OLLAMA_HOST=127.0.0.1:11434 OLLAMA_MODELS=/tmp/ollama-models OLLAMA_NUM_PARALLEL=1 \
  OLLAMA_MAX_LOADED_MODELS=1 /tmp/ollama/bin/ollama serve
/tmp/ollama/bin/ollama pull all-minilm      # 45 MB, native 384-dim (zero-padded, §1)
/tmp/ollama/bin/ollama pull qwen2.5:0.5b    # 397 MB
```

The models were chosen from §1's rule: `all-minilm` at 384 dims is zero-padded to the fixed
1536 column, so no destructive migration and no re-embedding.

### What was verified against the real runtime

The acceptance driver embedded in `.github/workflows/real-local-ai-verification.yml` was
extracted and run unmodified against a real backend + real worker with
`DEFAULT_LLM_PROVIDER=ollama`, `DEFAULT_EMBEDDING_PROVIDER=ollama`, `CHAT_FALLBACK_ORDER=""`
(so no silent fallback to a paid API could hide a local failure) and a throwaway PostgreSQL
database `lifeos_reallocal` at migration head `0095`.

| Check | Result | Evidence |
| --- | --- | --- |
| `local_model` health | **PASS** | `reachable: true`, `configured_models: [qwen2.5:0.5b, all-minilm]`, `missing_models: []` |
| Founder login + CSRF | **PASS** | 200, session cookie + csrf token |
| Upload → real embedding → indexing | **PASS** | `status=indexed`, `chunk_count=1`, chunk stamped `all-minilm`/384 |
| Authorized retrieval | **PASS** | chat cited the uploaded document |
| Real local generation | **PASS** | `assistant_status=succeeded`, `provider=ollama`, `model=qwen2.5:0.5b` |
| Conversation persistence | **PASS** | conversation id addressable via `GET /api/conversations/{id}` |
| Restart persistence | **PASS** | after killing and restarting backend + worker: conversation, indexed source and live retrieval all survived (5/5 post-restart checks) |
| Real SSE streaming | **PASS** | `text/event-stream`, **22 delta events** over 24 events, terminal `done` with `provider=ollama`, cited source, persisted turn |
| Runtime outage | **PASS (honest)** | with Ollama stopped: `reachable: false`, `missing_models` omitted, API still `status: ok` (no 503); chat returned `assistant_status=failed`, `provider=null` — no fallback call |
| Recovery without restart | **PASS** | Ollama restarted → `reachable: true`, chat `succeeded` (reply 775 chars), **backend PID unchanged** |

Generation was real, not replayed: a single grounded turn with a 1219-token prompt took
**31.1 s** (Ollama access log) and the loaded model held **511 MB RSS** on this 2-vCPU host.
That number matters for §7 below — it is a latency budget, not a correctness signal.

### Browser journeys against the real model

Two environment facts made the first browser run fail for reasons unrelated to the product,
both worth knowing:

1. **`COOKIE_SECURE` must stay at its default (`true`) for any browser run.** The session
   cookies are `SameSite=None`, and Chromium rejects `SameSite=None` **without** `Secure`.
   With `COOKIE_SECURE=false` (what the non-browser CI workflows set) the proxy login returns
   200 but the browser drops the cookie, so every authenticated journey bounces back to
   `/login`. `http://localhost` is a secure context, so `Secure` cookies work there over http.
2. **`LD_LIBRARY_PATH` must include the extracted GTK/ATK libraries** for the bundled
   Chromium, exactly as the E2E commands above already document.

With both set, `founder-journey-v6.spec.ts` and `model-routing.spec.ts` pass **against the
real model** (streaming, reload from durable history, upload, worker indexing, citation link,
supervised agent task, and live local-model discovery). Two genuine spec defects were fixed
rather than papered over: the admin discovery assertion was ambiguous once a real
`qwen2.5:0.5b` row also existed in the usage table (now scoped to the `Lokala modeller`
section), and the V6 journey asked for a citation before the worker had finished indexing the
upload it had just made (now waits for the queue item to report `Klar`).

Four resilience tests still fail **against a real model, by construction**: two assert the
deterministic fake provider's canned strings (`Hej! Detta ar ett riktigt svar`,
`Detta är ett långsamt strömmande svar`), and two use 10–30 s windows for a stream that takes
31 s here (`MainAI skriver…` still visible; `route.fetch()` cannot resolve until the live SSE
stream ends). Those specs certify the fake-provider harness and were left as they are; the
real-model equivalents are the acceptance checks above.

### Four-hour endurance

The 14 400-second soak is running in isolation (`lifeos_v5_soak`, Redis DB 15, its own
backend on a private port, restart every hour, concurrency 4) and is **not** claimed as a
result until it completes. A shortened run is never reported as endurance.

### Compatibility with Claude's PR #471

PR #471 (`claude/founder-alpha-phase3-staging`, head `9938fd2`, base
`integration/founder-alpha-20261121`, open draft, 83 commits, 248 files) was compared at file
and hunk level. It contains **no** `local-models` endpoint, **no** `Lokala modeller` section
and **no** `providers/local_discovery.py`, so this round's discovery feature is not duplicated
there. Six files are shared by name, but the actual conflict surface is small and semantic
disjointness is clear:

| File | PR #471 hunks | This branch hunks | Risk |
| --- | --- | --- | --- |
| `backend/app/routers/admin.py` | 16, 193, 257 | 1–3, 19–20, 36–38, 47–51, 64–70, 130–146 | low — only the import block can touch |
| `backend/app/schemas.py` | 1, 91, 191, 234, 284, 696, 1077, 1565, 1705, 1903, 1973 | 599 | low — 599 is untouched by #471 |
| `frontend/lib/api.ts` | 11, 200, 230, 253, 372, 439, 446, 703, 1190, 1331, 1372, 1592, 1634 | 750, 1591 | low–medium — 750 borders #471's 703 hunk |
| `frontend/app/(shell)/admin/page.tsx` | 15, 148, 178 | 1–7, 22, 31–32, 74–81, 223–234 | medium — 22 sits inside #471's 15–27 |
| `frontend/e2e/model-routing.spec.ts` | none vs this base | 43 | none |
| `docs/BRANCH_REGISTRY.md` | — | — | registry only; resolve on rebase |

Recommendation: rebase this branch onto #471's merge target only **after** #471 merges, per
`CLAUDE.md`'s merge rule, and expect at most three small textual conflicts (admin router
imports, `api.ts` around 750, admin page around 22) — none of them behavioural.

