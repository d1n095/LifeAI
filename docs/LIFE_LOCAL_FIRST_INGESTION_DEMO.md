# Local-first historical ingestion → retrieval — first measurable proof

_Author: Claude Opus 4.8 (independent). Branch: `claude/egress-local-provider-no-egress-ledger`,
off `claude/det-kommer-mer-879lcm` @ `64c07d5b0`. `first_cycle_go=false`. Independent review
required — the author does not self-certify._

## Why this unit

The reconnaissance (PR #325) asked for the **smallest useful, non-overlapping** step that moves
Life toward ingesting, preserving, understanding and retrieving the Founder's authorized
historical material **locally, without paid-provider data egress**.

A full code audit (not docs) of the ingestion→memory pipeline on the default branch found that
the document pipeline is already substantially built end-to-end:

```
upload → durable ImportJob → immutable content-addressed storage → extract (pdf/docx/html/text)
       → chunk → egress-gated embed → pgvector DocumentChunk (owner-scoped, RLS) → retrieval
```

and that a **local embedding provider already exists** (`OllamaProvider.embed`, on-host, no API
key, no cost). So the document path is **not hard-blocked**. The genuine gaps are about the
*no-egress guarantee*, not the plumbing:

1. **The egress gate could not tell a local provider from an external one.** Every embed with
   `owner_id` set was ledgered as an external-AI disclosure (`decision='allowed'`), even when it
   went to a local on-host Ollama. There was **no checkable invariant** "this ingestion sent
   nothing to a third-party AI." → **This PR fixes this.**
2. **`embedding_dim` is hardwired to 1536 (OpenAI's size).** A real local embedding model
   (nomic-embed-text=768, mxbai-embed-large=1024, …) emits a different dimension and cannot be
   used without a schema migration of `DocumentChunk.embedding` **plus a Founder decision** on
   which local model/dimension to standardize on. → **Reported as a blocker, deliberately NOT
   worked around here** (see "First genuine architectural blocker" below).

## What this PR implements (bounded)

A truthful **local-vs-external** distinction in the egress gate:

- `LLMProvider.is_external_egress` — fail-safe `True` by default; a provider is only treated as
  on-host when it proves it. `OllamaProvider` derives it from the **configured** `ollama_base_url`
  (loopback / in-deployment `ollama` host = local; any remote host = external, fail-safe), so a
  misconfigured remote endpoint is never mislabelled as local.
- `enforce_egress_policy(..., provider_is_local=False)` — records an on-host call with the new
  `decision='local'` instead of `allowed`. It **never relaxes a deny**: the incomplete-request
  deny and the NEVER_EGRESS/private-key hard-deny apply identically to local calls, and
  secret-pattern redaction still runs. Only the allowed-path *label* changes.
- Migration `0069` widens the ledger's `decision` CHECK constraint to allow `local` (and makes
  `local` carry a `sent_content_hash` like `allowed`). No other schema change.
- `embed_with_policy` / `chat_with_fallback` pass the provider's locality through.

Net effect: **a local-first ingestion now produces zero external `allowed` disclosure rows** —
only `local` — which is the invariant the demo asserts.

## The supervised demonstration

`backend/tests/backend/rag/test_local_first_ingestion_retrieval_demo.py` runs the **real**
library-import pipeline on three synthetic "historical" documents using an on-host local
embedding provider (a deterministic offline stub standing in for Ollama — reproducible with no
network, no model, no paid call), then **restarts the session** and retrieves.

Acceptance criteria asserted (recon §I):

| # | Criterion | How asserted |
|---|---|---|
| 1 | Ingestion runs end-to-end on the real pipeline | all three docs reach `IndexStatus.indexed`, `chunk_count ≥ 1` |
| 2 | The embedding provider is the on-host local one | active `ProviderConfig` = ollama; `is_external_egress` False |
| 3 | Retrieval after worker/session **restart** | ingest session is closed/discarded; a fresh `SessionLocal()` serves retrieval from durable DB state only |
| 4 | The correct answer is selected among several sources | query by a token unique to one doc returns that doc's chunk; the other two do not win |
| 5 | The hit is linked to its exact original source | `document_id` and `title` match the originating document |
| 6 | No external-AI disclosure occurred | every disclosure-ledger row for the owner is `local` or `denied`; **no `allowed`** |
| 7 | Both ingest and retrieval embeds are on-host | `rag_ingest` and `rag_retrieval` both present as `local` |

Run it:

```bash
cd backend
TZ=UTC <venv>/bin/python -m pytest tests/backend/rag/test_local_first_ingestion_retrieval_demo.py -q
```

## First genuine architectural blocker (reported, not worked around)

**`settings.embedding_dim = 1536` is a single global pgvector column dimension** on
`DocumentChunk.embedding`. Switching to a real local embedding model whose vectors are not 1536-d
requires a schema migration and a one-way re-embed of existing chunks — and a **Founder decision**
on which local model to standardize on (dimension, quality, resource cost). That is a larger
architecture decision, so per the assignment it is surfaced here rather than hacked. The demo
sidesteps it honestly by using a 1536-d deterministic stub for synthetic data; it does **not**
claim a real local model is wired.

## Scope / governance

- Touches only `egress_policy`, `providers`, the disclosure model + migration `0069`, and tests.
  No coordination/large-file/messaging code (Codex #319–#322/#324, Cursor #285–#315 lanes untouched).
- Preserves original bytes + provenance (unchanged storage/extract path), idempotent/restart-safe
  (gate is stateless; ingestion already durable), owner isolation + RLS (unchanged), and treats
  document content as untrusted data (gate denies/redacts before any provider call).
- No paid external model calls; no merge/deploy/Recall/autonomy. Independent review required.
