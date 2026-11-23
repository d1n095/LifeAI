# Founder Alpha staging deployment — design and resource assessment

Workstream 3 of the "LifeAI Founder Alpha Staging Activation" program. Produced against PR
#491's exact head `192ce7b6e930ba54e5b9777d43c642c6e611eadc`, independent of (and without
changing) Workstream 1's consolidation or Workstream 2's policy-lock review.

**What this document is not**: a record of an actual deployment. This session has no
credentials for any real VPS, cloud provider, or external hosting infrastructure. Everything
below is design, configuration, and what could be verified structurally in an isolated
sandbox — see "What was and wasn't tested" at the end.

## The constraint that drives every choice here: 2GB RAM, already in use

The founder's directive states the production VPS has ~2GB RAM and already runs an older
LifeAI installation. Reading the repository's own existing production topology
(`docker-compose.vps.yml`) confirms this is tight even for production alone:

| Service  | `mem_limit` |
|----------|-------------|
| caddy    | 128m        |
| backend  | 512m        |
| worker   | 384m        |
| frontend | 512m        |
| redis    | 128m        |
| **Total**| **1664m**   |

That leaves roughly 384MB of a 2048MB host for the OS, Docker's own overhead, and any burst
above the configured limits — already a thin margin for production by itself. Two things are
worth flagging explicitly rather than assuming:

- Production's Postgres is **not** on this VPS at all — `docker-compose.vps.yml` has no
  `postgres` service; the backup script's own comment confirms it's Supabase-hosted. The VPS
  never needs to budget RAM for a database.
- Production has **no local Ollama/model-runtime container** either, and
  `scripts/vps/lib.sh`'s `LIFEAI_REQUIRED_ENV_VARS` lists no `OLLAMA_BASE_URL`/API-key
  variable for production at all. Whatever "the older LifeAI installation" currently does
  for AI responses, it is not running model weights on this box.
- Separately, `scripts/vps/lib.sh`'s own header comment states "there is no real server to
  run \[these scripts\] against yet" — which reads as in tension with the directive's "already
  running an older LifeAI installation." This may mean the *production topology in this repo*
  (`docker-compose.vps.yml`, Strato VPS) hasn't been deployed yet, and the "older installation"
  referenced is a different, earlier deployment (there are references elsewhere in this repo
  to a previous Render-based deployment that was abandoned). **Flagging this discrepancy
  rather than guessing which is current** — worth a one-line confirmation from the founder,
  not assumed either way in the design below.

Either way, the arithmetic above is the real constraint: **a 2GB host has no spare room for a
second full application stack next to a running production one**, regardless of which exact
history is current.

## Comparing the four options

| Option | Resource fit on the real 2GB VPS | Isolation from production | Real-model capability |
|---|---|---|---|
| **1. Isolated environment on existing infra** | **Fails arithmetically.** Even a minimal staging stack (Postgres+Redis+backend+worker+frontend, no Caddy, no model) needs ~1.4GB by itself (see below) — stacked on top of production's 1.66GB, that's over 3GB against a 2GB ceiling. Not viable without either shrinking production's own limits (risky) or buying more RAM for the same box (which stops being "existing infrastructure"). | Weakest even where it fits: shared kernel, shared disk I/O, shared network interface with production. | Same constraint as production — no local model without worsening the RAM problem further. |
| **2. Dedicated test instance** | Clean fit — a separate host sized for staging alone (the compose file below needs ~1.4GB of `mem_limit` headroom, so a small 2GB instance works, a 1GB one is too tight). | Strongest: separate kernel, disk, network path. A staging bug or crash cannot touch production at all. | Can run a local model if the instance is sized for it (RAM for the instance + the model's weights), or use a remote endpoint (see option 3) to stay small. |
| **3. Remote, approved Ollama endpoint** | Minimal — the staging host itself never needs RAM for model weights, only for the app services. | Orthogonal to where the app runs — can be combined with option 2. | Real model responses without the staging host needing to be large. Mirrors the project's own existing pattern: production already keeps its one stateful, resource-heavy dependency (Postgres) off the thin app host via Supabase; doing the same for the model-weight dependency is the same philosophy applied consistently, not a new one. |
| **4. Lightweight local model runtime where resources permit** | Only fits on a dedicated instance with real spare RAM beyond the app stack's own ~1.4GB — e.g. a 3-4GB instance running a ~0.5-1B-parameter quantized model (`qwen2.5:0.5b`, already proven real in Item 2's CI run) alongside it. Does not fit the 2GB production-class box at all. | Same as option 2 if co-located with it. | Real, fully local — no outbound dependency once weights are pulled, at the cost of a larger instance. |

**Recommendation: option 2 (dedicated test instance) combined with option 3 (remote Ollama
endpoint) as the default**, with option 4 as a documented upgrade path for a larger dedicated
instance if the founder wants a fully self-contained staging environment later. This is the
smallest reliable and secure combination that actually fits realistic hosting budgets, and it
reuses rather than reinvents the project's own established pattern (keep heavy stateful
dependencies off the thin app host).

## What was built

### `docker-compose.staging.yml`

A new, self-contained compose file — not a modification of `docker-compose.vps.yml` or
`docker-compose.yml` — implementing option 2+3:

- **Its own Postgres+pgvector container** (unlike production's Supabase), so a staging run can
  never touch real founder data, with its own disposable named volume.
- **Redis**, hardened the same way as production (`read_only`, `cap_drop: ALL`, no persisted
  disk, password-required) but with staging-specific credentials.
- **Backend/worker/frontend built from source** (`build:`, not a pinned `image:` digest) —
  staging exists to test an unreleased candidate commit, not a tagged release; a comment in
  the file documents switching to a pinned `${STAGING_BACKEND_IMAGE}` once one exists.
- **`OLLAMA_BASE_URL` has no default pointing at a bundled model container** — it's a required
  variable the operator points at a remote, already-approved endpoint. A separate `ollama`
  service exists but is gated behind the `local-llm` Compose profile (same opt-in pattern
  `docker-compose.yml` already uses) for the rare case staging runs on a large enough
  dedicated instance to afford option 4 instead.
- **Caddy**, gated by HTTP Basic Auth in front of the app's own founder login (`Caddyfile.staging`)
  — this candidate has not had independent security review yet, so staging gets a second,
  network-level gate, not just the app's own auth. Published on `8443`/`8080`, never
  `443`/`80`, so it cannot collide with a production Caddy if an operator ever did run both on
  one host despite the resource math above.
- **Total `mem_limit` budget: 256 (postgres) + 96 (redis) + 384 (backend) + 256 (worker) + 384
  (frontend) + 96 (caddy) = 1472MB** — fits a 2GB dedicated instance with headroom, intentionally
  lighter than production's 1664MB since there's no local model container in the default
  profile.
- Named volumes for Postgres data, uploads, and Caddy's TLS state — nothing ephemeral that
  would lose data across a restart.

### `Caddyfile.staging`

Reuses the production Caddyfile's structure (request body limit, security headers, access
log) verbatim, adding only the `basic_auth` block.

### `scripts/vps/backup-staging.sh` / `scripts/vps/rollback-staging.sh`

Kept as their own small scripts rather than parameterizing production's `backup.sh`/
`rollback.sh` — those two scripts are production-critical and already covered by CI's exact
current behavior; widening their interface to serve two different volume/project namings
would risk the one backup path the real VPS actually depends on.

- `backup-staging.sh` follows the same structure as `backup.sh` (archive compose-dir files +
  named volumes, checksum, prune old archives) but additionally runs a real `pg_dump` against
  the staging database, since — unlike production — staging's Postgres is local and would
  otherwise have no backup coverage at all.
- `rollback-staging.sh` differs from production's rollback by necessity: there is no
  pinned-image deployment history to roll back to (`build:`, not `image:`), so it takes an
  explicit `--to-commit <sha>`, checks it out, and reuses the exact live-tested migration
  **downgrade** procedure Stream 6/#484 already proved clean (a real `alembic downgrade`
  round-trip, not a theoretical one) before rebuilding — falling back to restoring the backup
  script's `pg_dump` output if the target commit predates a migration already applied.

## Network-security notes (the directive's explicit requirement)

- Nothing in `docker-compose.staging.yml` binds a container port to the host except Caddy's
  `8443`/`8080` — every other service (`postgres`, `redis`, `backend`, `worker`, `frontend`,
  `ollama`) has no `ports:` entry at all, reachable only over the two internal Docker
  networks (`staging_internal`, `staging_data` — the latter `internal: true`, no route out),
  mirroring production's own two-network isolation exactly.
- The one publicly-reachable port is gated by HTTP Basic Auth (`Caddyfile.staging`) in
  addition to the app's own founder login — explicitly because this is a pre-review
  candidate.
- **Before this is ever actually brought up on a real host**, the operator must still
  firewall `8443`/`8080` to only the founder's own IP (or a VPN), since Basic Auth alone is
  not a substitute for network-level restriction against automated scanning/credential
  stuffing. This document does not configure a firewall itself — that's host-specific and
  outside what a compose file can express — but it is a required step before use, not
  optional hardening.

## Restore from backup (referenced above)

1. Stop the staging stack: `docker compose -p lifeai-staging -f docker-compose.staging.yml down`.
2. Start only `postgres` fresh: `docker compose -p lifeai-staging -f docker-compose.staging.yml up -d postgres` and wait for it to report healthy.
3. Restore the dump: `docker compose -p lifeai-staging -f docker-compose.staging.yml exec -T postgres psql -U lifeos_staging -d lifeos_staging < db.sql` (from the extracted `backup-staging.sh` archive).
4. Restore `staging_uploads`/`staging_caddy_data`/`staging_caddy_config` from their respective `.tar.gz` members into fresh volumes the same way `docs/VPS_BACKUP_RESTORE.md` already documents for production's equivalents.
5. Bring the rest of the stack up: `docker compose -p lifeai-staging -f docker-compose.staging.yml up -d`.

## What was and wasn't tested in this sandbox

- **Tested, real**: `docker-compose.staging.yml` parses and fully resolves with `docker
  compose config` (all required variables, volumes, networks, healthchecks render correctly;
  exit code 0) — the Docker CLI and Compose plugin are present in this sandbox, but the
  daemon is not reachable (`Cannot connect to the Docker daemon`), consistent with earlier
  phases of this integration effort finding Docker builds blocked here. `bash -n` passed
  clean on both new scripts.
- **Not tested here, and said plainly rather than assumed**: actually bringing the staging
  stack up (`docker compose up`), a real backup/restore cycle against it, and the live
  rollback script's downgrade step. CI's `vps-scripts-check` job (shellcheck + bash syntax
  check over every `scripts/vps/*.sh`) will pick up both new scripts automatically via its
  glob, but that job is gated to a specific set of branches (not this one), so it did not run
  as part of this change — noted, not worked around.
- Both new shell scripts could not be run through `shellcheck` itself in this sandbox (the
  binary is not installed here); only `bash -n` syntax validation was performed locally.
