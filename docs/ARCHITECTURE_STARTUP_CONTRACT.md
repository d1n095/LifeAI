# Architecture and startup contract

Recorded 2026-11-19 on `freebuff/founder-alpha-ops-recovery-20261118`, alongside the release
gate. This is not a plan for the ecosystem: it is the set of properties that must hold *at
startup*, now, so that the products named in the founder brief can be added later as modules
instead of as a rewrite of the founder's release path.

Machine-checked by `backend/scripts/ops/verify_architecture_contract.py` (7 checks, read-only,
no database, no process), which is wired into `founder_release_acceptance.py` as item
`15.architecture_invariants`. Each check is either:

- **enforced** — a structural property of the current code (import graph, configuration);
- **preserved** — an absence guard: the capability is not built, and the check fails the day
  something starts depending on it.

A `preserved` check passing is **not** evidence that the capability works. It is evidence that
nothing forecloses it yet.

## The eight requirements, classified honestly

| # | Requirement (founder brief §3) | Today | Check | What must stay true |
|---|---|---|---|---|
| 1 | Data minimisation, user-directed sharing | **partial** | `provider_adapters_cannot_reach_data` | Provider adapters receive messages, never a DB handle or the ORM. What a *caller* chooses to send is not yet bounded by policy — that is the remaining half, unbuilt. |
| 2 | A separate personal user-AI that shields the founder and filters what reaches MainAI | **not built** | — | MainAI's conversation entry point stays a single server-side path (`app/routers/chat.py`), so a personal layer can be inserted in front of it without changing the Core. Nothing in the current design assumes the browser is the only client. |
| 3 | MainAI's own independent, deterministic security checks | **enforced** | `deterministic_privileged_path` | Approval, session/CSRF, RLS and revocation modules import no provider. A decision that can consult a model is advisory by construction; these cannot. Approvals are additionally DB-enforced by migrations 0071/0072. |
| 4 | Modular LifeID, LifeSocial, LifeMessenger, LifeMusic, LifeBusiness | **preserved** | `life_products_not_core_coupled` | Zero references in `backend/app` and in the migration chain. Each product must arrive as its own module with its own migrations, never as an edit to a migration the founder's database has already run. |
| 5 | One research engine with extensible source adapters (web, GitHub, scientific, isolated Tor/.onion) | **partial** | `tor_not_a_startup_dependency`, `single_migration_head` | Retrieval is already one engine over documents/chunks with a single storage-backend ABC (`app/storage/base.py`) and content-addressed blobs. A new source is an *importer* into that model, not a second engine. GitHub evidence staging already exists (0076) as one such importer. |
| 6 | Tor must not be a hard startup dependency | **preserved** | `tor_not_a_startup_dependency` | Zero references to Tor/SOCKS anywhere in `backend/app`. Optional .onion access must arrive as an opt-in adapter; if it ever appears in the boot path, the check fails. |
| 7 | Strato as the planned operating environment, public web access separated from founder administration | **enforced in configuration** | `public_edge_only_ingress` | Exactly one published edge (Caddy, 80/443) and exactly one upstream (the frontend). The backend publishes no ports; founder administration reaches it only through the authenticated app. |
| 8 | Future local/offline operation and user-controlled encryption | **partial** | `provider_adapters_cannot_reach_data`, `tor_not_a_startup_dependency` | Local model operation already works (Ollama, `/api/health` reports reachability). Backups are AES-256-GCM with per-chunk nonces. Per-user at-rest encryption of documents is not built; the storage ABC is the seam it will attach to. |

## What this does not claim

- No Life product, research adapter, personal user-AI, or .onion access was built in this pass.
  Building them now is explicitly out of scope.
- Checks 2, 5 and 8 are marked **partial / not built** in the table because the corresponding
  capability does not exist. The verifier does not report them as passing requirements, only the
  narrower invariants it can actually measure.
- `single_prompt_fence` is not an ecosystem requirement, but it is the same class of control:
  one canonical fence, one defusing function, both prompt builders through it. It lives here
  because R12 will arrive with its own prompt path and must not bring a second fence.

## Why these specific checks

They were chosen because each one can fail *silently* while every test stays green:

- a second fence that still counts its markers correctly (`blueprint.py` did exactly that until
  this pass — a document could close its own fence; see defect 4 in
  `docs/FOUNDER_RELEASE_CANDIDATE.md`);
- an import of a provider into an approval path, which turns an enforcement point into advice;
- a Tor/SOCKS reference that quietly becomes a boot requirement;
- a Life product that arrives as a migration edit rather than a new chain;
- a second published port, or a reverse proxy pointed at the backend;
- a split migration DAG, which is also the exact hazard the R12 merge has to navigate.

## Next steps (not started, in dependency order)

1. Policy for what context may leave the host (requirement 1, second half) — needed before any
   third-party provider is enabled for founder data.
2. Personal user-AI as a filtering layer in front of `chat.py` (requirement 2).
3. Research adapters as importers into the existing retrieval model (requirement 5), with Tor as
   an optional egress adapter.
4. Per-user at-rest encryption behind `StorageBackend` (requirement 8).

None of these is a prerequisite for private founder use today, and none is implemented.
