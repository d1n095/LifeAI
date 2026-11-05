# Founder Alpha — first isolated startup checklist (executable, minimal)

Candidate (frozen): **`d13efd7`** on `claude/founder-alpha-blueprint-draft` (PR #340).
Scope: bring up Founder Alpha **once, isolated**, so the Founder can sign in, chat with a real
local model, import originals, and build a Blueprint. **This is a checklist, not a deployment** —
do not deploy, merge, modify STRATO, delete originals, or activate paid providers. `first_cycle_go=false`.
Independent security review of `d13efd7` (Codex's lane) remains mandatory before first cycle.

Reuses existing tooling only — `docker-compose.vps.yml`, `scripts/vps/*.sh`,
`docs/STRATO_VPS_DEPLOY.md`, `docs/VPS_BACKUP_RESTORE.md`, and the verified env in
`.github/workflows/founder-alpha-verification.yml`. No new architecture.

## 1. Startup configuration requirements (verify each before first boot)

| Area | Requirement (exact) | Source of truth | Verification criterion |
|---|---|---|---|
| **Model runtime** | An `ollama` service reachable at `http://ollama:11434`, with the chosen chat + embedding models pulled. Config default `ollama_base_url=http://ollama:11434` expects the compose service name `ollama`. | `backend/app/config.py:135`; workflow env | `curl -sf http://ollama:11434/api/tags` lists the two models; a backend `/api/chat` returns `provider=ollama` |
| **No paid provider reachable** | Override the paid defaults: `DEFAULT_LLM_PROVIDER=ollama`, `DEFAULT_EMBEDDING_PROVIDER=ollama`, `DEFAULT_LLM_MODEL=<chat>`, `DEFAULT_EMBEDDING_MODEL=<embed>`, and **`CHAT_FALLBACK_ORDER=` (empty)**. Defaults are `openai…` — must be changed. | `config.py:124-139` | env shows ollama + empty fallback; no `OPENAI/ANTHROPIC/GEMINI_API_KEY` set → a failed local call errors, never silently calls a paid API |
| **Database** | PostgreSQL 16 + **pgvector**; `DATABASE_URL` (admin/migration) + `APP_DATABASE_URL` (restricted `mainai_app`). Migrations applied to head incl. `0070`/`0071`. | `docker-compose.vps.yml` (external/managed PG); `alembic upgrade head` | `alembic current` = `0071_blueprint_approval_guard`; `SELECT extname FROM pg_extension` includes `vector` |
| **Persistent storage** | Durable volume for `STORAGE_ROOT=/var/lib/lifeai/uploads` (the immutable originals). | `config.py:151`; compose `uploads` volume | volume mounted + survives container restart |
| **Authentication** | Founder account bootstrapped from `FOUNDER_EMAIL` + `FOUNDER_PASSWORD`; `require_founder` gate; `COOKIE_SECURE=true` in production (HTTPS). | `config.py:20,35`; `app/deps.py` | `POST /api/auth/login` as founder → 200 + csrf; a non-founder is 403 on `/api/blueprint/*` |
| **Environment variables** | `SECRET_KEY`, `REDIS_URL` (valkey), `FRONTEND_ORIGINS`, `PUBLIC_APP_URL` set to the real host; no placeholders. | `config.py`; compose | backend `/api/health` 200; no placeholder-secret warning |
| **Network isolation** | Backend/worker/redis on the internal-only network (`internal: true`), DB not publicly exposed, only caddy (TLS) faces the internet. | `docker-compose.vps.yml` network topology; `docs/VPS_ARCHITECTURE.md` | only caddy ports published; `docker network inspect` shows the data network internal |

## 2. Original-file backup / independent restore / SHA-256 integrity (prepare — do NOT delete or transfer originals yet)

Originals are stored **content-addressed** with a SHA-256 identity and are Founder-only
downloadable (#329). The procedure, using existing tooling, before any local-file deletion:

1. **Backup:** run `scripts/vps/backup.sh` (archives DB + the `uploads` volume) — see
   `docs/VPS_BACKUP_RESTORE.md`.
2. **Independent restore:** restore that archive into a **separate throwaway** target with
   `scripts/vps/restore.sh` (never over the live instance).
3. **Integrity verification (SHA-256):** for every original, confirm the restored bytes hash to
   the stored checksum — compare `sha256sum` of each restored file against `Document.checksum`
   (the content-addressed id the app already records). All must match.
4. **CI evidence:** the repo's CI job *"VPS backup.sh / restore.sh — archive structure, checksums,
   and restore behavior"* must pass against a real backup (it is skipped on non-VPS runs — run it).
5. **Only after 1–4 succeed** may deletion/transfer of a local original be *proposed* to the
   Founder. This checklist authorizes none; it prepares the procedure.

## 3. Life can begin before all of LifeOS is complete (confirmed)

The Founder Alpha boot directive (`docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`) makes the first
objective a functioning, secure, persistent MainAI — not a finished LifeOS. The minimum that is
**already verified end-to-end** (real-Ollama run **41903171671 @ `d13efd7`**, every criterion
PASS) covers exactly the startup capabilities:

- **Real conversation** — `/api/chat` answered by the local model (provider=ollama).
- **Persistent memory / document access** — upload → real embed/index → grounded retrieval; the
  immutable original is retrievable and survived a backend+worker restart (SHA-256 unchanged).
- **Project understanding** — source-grounded Blueprint generated by the real model, versioned,
  traceable to the originals, and approved only by an explicit Founder action.

So Life can start and be useful now; the remaining LifeOS features are built incrementally after
startup, per the boot directive.

## 4. Actual remaining startup blockers (only real ones)

1. **Ollama is not yet in `docker-compose.vps.yml`.** Add an `ollama` service on the internal
   network and pull the chosen models. (Founder decision + ops step.)
2. **Production model decision is open** — CI used TEST models (`qwen2.5:0.5b`, `all-minilm`); pick
   production chat + embedding models and size host RAM. Re-indexing is required if the embedding
   model/native-dim changes (the embedding-space guard will not mix spaces).
3. **Independent security review of `d13efd7`** (Codex's lane) must complete — no self-cert, no merge.
4. **Backup→restore→integrity cycle (§2) must pass** before any original is deleted/transferred.

Not blockers (already satisfied): Blueprint API+UI, the full real-model chain, the #333 grounding
fix, migration chain (single head + reversible), the two P1 security findings (DB-enforced), the
frontend build/lint/typecheck.

## 5. Minimal startup checklist (exact references + verification)

> All paths are at `d13efd7`. Operator runs these; I do not deploy.

1. **Provision host** — `scripts/vps/00_preflight.sh … 90_verify_installation.sh` (idempotent).
   ✔ `90_verify_installation.sh` exits 0.
2. **Add the `ollama` service** to `docker-compose.vps.yml` on the internal network; pull the
   production chat + embedding models. ✔ `curl http://ollama:11434/api/tags` lists both.
3. **Set `/etc/lifeai/lifeai.env`** per §1 (ollama providers, empty fallback, DB/redis/secret/
   origins, `COOKIE_SECURE=true`, `STORAGE_ROOT`). ✔ no placeholder values; no paid API keys.
4. **Migrate** — `alembic upgrade head` + `python scripts/security/apply_runtime_privileges.py`.
   ✔ `alembic current = 0071_blueprint_approval_guard`; privilege policy reports no errors
   (blueprint_drafts = SELECT,INSERT; `blueprint_approve_draft` EXECUTE granted).
5. **Bring up the stack** — `scripts/vps/deploy.sh` (see `docs/STRATO_VPS_DEPLOY.md`).
   ✔ `/api/health` 200; frontend reachable via caddy TLS at `PUBLIC_APP_URL`.
6. **Smoke the startup capabilities** (§3) manually as the founder: login → chat (provider=ollama)
   → upload one original → see it indexed → ask a grounded question → generate a Blueprint draft →
   inspect source refs → approve. ✔ matches the CI acceptance criteria.
7. **Prepare backups** — configure + run §2 steps 1–4 (do not delete originals). ✔ restore test +
   SHA-256 match pass.
8. **Hold at `first_cycle_go=false`** until the Founder, after independent security review, flips it.

## Founder-approval gates (what needs the Founder, not me)

- Approve the frozen candidate `d13efd7` after Codex's independent security review.
- Choose the production model + host sizing.
- Authorize adding the `ollama` service and the first isolated bring-up.
- Approve each original's deletion/transfer only after §2 passes.
- Flip `first_cycle_go` to true.

Boundaries: no merge, no STRATO change, no deletion of originals, no paid-provider activation;
Codex's #340 security review + the #341 coordination lane untouched.
