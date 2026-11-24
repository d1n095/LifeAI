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

# 6. real streaming (Founder Alpha Item 1): the SSE endpoint must deliver more than one
#    token-bearing `data: {...}` event before `done: true`, and ending the connection after
#    reading only the first event (simulating a closed browser tab) must not crash or wedge
#    the backend — a health probe taken right after must still return 200.
curl -sN localhost:8000/api/chat/stream -H 'content-type: application/json' \
     -H "X-CSRF-Token: …" -d '{"message":"<question>"}'
```

Real-Ollama CI (`.github/workflows/real-local-ai-verification.yml`, `workflow_dispatch`)
exercises all of the above — including the streaming/disconnect checks in step 6 — against a
real installed Ollama runtime and a throwaway pgvector service container on a GitHub-hosted
runner (which has real internet access, unlike most interactive development sandboxes), and
fails if the runtime is unreachable — never falling back to a paid API. Dispatch it with
`candidate_ref`/`candidate_sha` set to the exact commit under test; its own defaults point at
a specific historical candidate and should not be read as "the current one" without checking.
