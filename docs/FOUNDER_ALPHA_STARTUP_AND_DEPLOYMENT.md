# Founder Alpha — startup & deployment proposal

Status: the complete Founder Alpha workflow passed end to end under a real local model in CI
(`Founder Alpha verification (real local)` run **41849139859 = SUCCESS**; `CI` run
**41844888832** green on all in-scope checks). This document is a **proposal**, not an executed
deployment. **No STRATO change, no merge, no paid provider, no local-original deletion has been
performed.** `first_cycle_go` remains false. Independent review of the candidate is still required
before any merge or deploy.

## 1. Exact candidate

- **Candidate branch head (reviewable SHA):** `8fabade` on `claude/founder-alpha-blueprint-draft`
  (PR #340).
- **What runs in production is a deterministic composition** (verified in CI, merges cleanly, one
  Alembic head):
  ```
  #323 trunk 04b27e9  +  #328 fa840e4  +  #329 830ab4d  +  #327 69cdaf3  +  #340 head
  ```
  This is what the real-Ollama workflow reconstructs and verifies. Merging PR #340 (after review)
  onto the integration base is the normal way to make this a single ref.

## 2. What was proven (real model, real DB, real storage)

Founder login → real-model chat (Ollama) → upload original → extract/index (real embed) →
grounded retrieval → **generate Blueprint with the real model** → inspect source refs → save a new
version → **restart backend+worker** → original (SHA-256 unchanged) + draft both survived →
**Founder approval** (approved_by set; re-approval refused 409). Embedding-space identity on the
real chunk: model `all-minilm`, native dim 384, stored 1536. No paid provider is reachable
(empty fallback chain; a failed local call errors rather than silently calling a paid API).

## 3. Host requirements

| Component | Requirement | Notes |
|---|---|---|
| Backend / worker / frontend / caddy / redis | as in `docker-compose.vps.yml` | already defined; images pinned by digest via `/etc/lifeai/lifeai.env` |
| PostgreSQL 16 + **pgvector** | external managed or a dedicated container | the VPS compose does not run Postgres itself — `DATABASE_URL`/`APP_DATABASE_URL` point at the managed instance; pgvector extension required |
| **Ollama runtime** (local model) | **NOT yet in the VPS topology — see Blockers** | CPU is enough for small models; RAM scales with the chosen model. Backend points at it via `DEFAULT_LLM_PROVIDER=ollama`, `DEFAULT_EMBEDDING_PROVIDER=ollama`, `DEFAULT_LLM_MODEL`, `DEFAULT_EMBEDDING_MODEL`, `OLLAMA_BASE_URL`, and an empty `CHAT_FALLBACK_ORDER` |
| Persistent storage | volume for uploads | `STORAGE_ROOT` defaults to `/var/lib/lifeai/uploads` (see the `uploads`/`uploads-init` volume in `docker-compose.vps.yml`) — this holds the immutable originals and must be on durable, backed-up storage |

## 4. Startup / deployment commands (existing scripts — run by the operator, not by me)

Local/dev (already proven by the CI workflow and the local runs):
```bash
# backend: real local models via Ollama, Postgres+pgvector, filesystem storage
# (see .github/workflows/founder-alpha-verification.yml for the exact, verified env)
alembic upgrade head
python scripts/security/apply_runtime_privileges.py
uvicorn app.main:app   # + python -m app.worker
```

Production (Strato VPS) — existing, documented scripts; do **not** run until review + blockers below are cleared:
```bash
# one-time host bootstrap (idempotent): scripts/vps/00_preflight.sh … 90_verify_installation.sh
# deploy a pinned image set:
scripts/vps/deploy.sh         # see docs/STRATO_VPS_DEPLOY.md
# roll back to the previous release:
scripts/vps/rollback.sh       # see docs/VPS_OPERATIONS_RUNBOOK.md
```

## 5. Backups, restore test, and the Founder's original archive

- **DB + uploads backup/restore:** `scripts/vps/backup.sh` / `scripts/vps/restore.sh`
  (`docs/VPS_BACKUP_RESTORE.md`). CI has a dedicated job — *"VPS backup.sh / restore.sh — archive
  structure, checksums, and restore behavior"* — run it (it is skipped on non-VPS runs) and confirm
  a **real restore of a real backup succeeds** before relying on it.
- **Original-document archive (the "migrate files off the Mac" goal):** originals are stored
  content-addressed with SHA-256 identity and are Founder-only downloadable (#329). **Do not delete
  any local original** until a separate backup → restore → integrity-verification cycle on the
  uploads volume has demonstrably succeeded (checksums equal after restore). This procedure has
  **not** been run here.

## 6. Access URL strategy

Caddy terminates TLS and reverse-proxies the frontend/backend (see `docker-compose.vps.yml` `caddy`
service + `docs/VPS_ARCHITECTURE.md`). Set `PUBLIC_APP_URL` and `FRONTEND_ORIGINS` to the real
host; session cookies are `Secure` + same-origin via the Next.js proxy route (`COOKIE_SECURE`
stays true in production — it is only disabled for the synthetic localhost CI run).

## 7. Remaining deployment blockers (honest)

1. **Ollama is not in the production topology.** `docker-compose.vps.yml` has no model-runtime
   service. Add an Ollama service (or equivalent) on the internal network, pull the chosen models,
   and point the backend at it — then re-run the real-local verification against that topology.
2. **Production model decision is open.** CI used TEST models (`qwen2.5:0.5b`, `all-minilm`),
   explicitly *not* a production choice. Pick production chat + embedding models and the host RAM to
   match; re-indexing is required if the embedding model/native-dim changes (the embedding-space
   guard will not mix spaces).
3. **Independent review of PR #340 at `8fabade` is required** (auth, RLS, concurrency, provenance,
   source refs, model failures, approval authorization) — not self-certified; no merge until then.
4. **Original-archive backup/restore/integrity cycle must pass** before authorizing any local-file
   deletion (see §5).
5. **Pre-existing CI issues (separate branches, not this candidate):** `npm audit` `next`-pinning;
   Playwright chromium-install on the runner image.

Until 1–4 are cleared, Founder Alpha is **launch-ready for a controlled/staged run on a prepared
host**, not for an unattended production cutover. `first_cycle_go=false`.
