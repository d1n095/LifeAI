# Founder Requirements Delta — 2026-11-08

> **Purpose.** Preserve, in a source-grounded and reviewable form, the Founder requirements
> re-confirmed on 2026-11-08 so that Life (MainAI) can use them as **future Blueprint input**
> once Founder Alpha passes its security gates. This is a **documentation-only** record. It is
> **not** permission to implement, and it does **not** claim any capability is built or verified.
>
> **Boundaries honored.** No private conversation exports, personal details, credentials, or
> sensitive material are copied here — only pointers to repository files already in the tree.
> No historical decision is overwritten; where statements conflict, both are preserved with
> provenance for future reconciliation. Nothing here modifies frozen candidates (#352/#357/#358/#366).
> `first_cycle_go=false`.

## How to read this document

Every requirement line is tagged with exactly one **classification**:

| Tag | Meaning |
|---|---|
| **[EXISTING]** | Already documented in the repository (citation given). Not re-stated as if new. |
| **[NEW]** | Newly confirmed Founder requirement with little or no prior repository documentation. |
| **[PROPOSED]** | An architecture/approach implied by the requirement that is **pending Founder approval** — written here as a candidate, not a decision. |
| **[UNVERIFIED]** | A capability whose feasibility/implementation status is **not verified**; must not be presented as working. |
| **[BLUEPRINT-INPUT]** | Material Life should treat as input when it later proposes Blueprints, after ingesting the historical corpus. |

A single line may carry a primary tag plus `+[BLUEPRINT-INPUT]` when it is also future Blueprint feed.

## Provenance index (repository sources reconciled for this delta)

| Source file | What it already establishes (relevant to this delta) |
|---|---|
| `MAINAI_CONTEXT_BUNDLE.md` | Vision: Life OS as a platform "för miljoner användare"; MainAI as executive ("Vice VD"); products Life OS / My Money Master / 4thepeople AB; three-layer architecture (memory / stations / MainAI); GitHub as source of truth; Founder has final say. Lists the historical intake material (FKP_curated, Life_OS_Claude_Handoff 19 docs, chatgpt_export 500–1000+ conversations). |
| `docs/MAINAI_ARCHITECTURE.md` | §4/§9 domain model; **deliberate RLS asymmetry** (Conversation/DocumentChunk are RLS-isolated per user; Document/Project/Task are shared org data by design); **multi-tenant is an explicitly documented TARGET** — "Idag är MainAI en enanvändar-/enföretagsmodell … Att bli en multi-tenant SaaS-plattform (flera separata företag på samma installation) kräver: Organization (NY)" (≈L294–297). |
| `docs/MEMORY_ARCHITECTURE.md` | One memory substance with a `scope` of `USER | PROJECT | ORGANIZATION | SHARED` (L26–40); SHARED is "always opt-in, never implicit"; per-tenant vectors with the rule to "never let one tenant's [data leak]" (L544); organization memory is today a "degenerate single-tenant case" (L189–194). |
| `docs/LIFE_FOUNDER_MEMORY.md` | Founder-memory reconciliation machinery: notes with `status` (`active | superseded | disputed | unknown`), `authority`, `basis`, `provenance`, `observed_at`, and `supersedes_note_id` (supersede, never silent overwrite). |
| `docs/FOUNDER_KNOWLEDGE_BOOTSTRAP.md` | `founder_vault/index.yaml` with `source: import`, `version`, checksum; import order after approval; supersede-not-overwrite rule; §8 "vad finns, vad saknas, hur importeras det". |
| `docs/KNOWLEDGE_IMPORT_SECURITY.md` | Security model for knowledge import (trust boundary for ingested material). |
| `docs/LIFE_LIBRARY_PLAN.md` | Library ingestion plan; provider inference boundary ("…till providerns inferens-API, aldrig till en fine-tuning-endpoint", L200). |
| `docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md`, `…_HANDOVER_2026-07-20.md` | Founder Knowledge Studio surface (curation of founder knowledge). |
| `docs/LIFE_PROJECT_ENTITIES_FOUNDER_API.md` | Founder-authority API surface over project entities. |
| Branch registry / merged PRs | MainAI internal workforce + composed executive loop (#230–#236), executive business workflow (#335), model-backed workers (#336) — the existing **agent/executive substrate** relevant to the business-platform requirement. |

> **Conflict preserved (do not silently resolve):** `docs/FOUNDER_KNOWLEDGE_BOOTSTRAP.md:330,338`
> flags that `docs/MAINAI_ARCHITECTURE.md:249` and the multi-tenant section both lean on the
> "single-user model" framing, which the bootstrap doc says risks conflation. This delta keeps
> both framings; reconciliation is a future task, not done here.

---

## A. Life Portable & Installation

| Requirement (Founder, 2026-11-08) | Classification | Grounding / gap |
|---|---|---|
| Browser-based access | **[EXISTING]** | The product is already a web app (Next.js frontend + FastAPI; `MAINAI_CONTEXT_BUNDLE.md:37`). |
| Downloadable desktop installation | **[NEW] +[BLUEPRINT-INPUT]** | No desktop-packaging design in the tree (only README install/ops notes). Genuine gap. |
| Portable USB execution (where technically supported) | **[NEW] [UNVERIFIED]** | No design; feasibility unproven. Future Blueprint input. |
| Portable SSD installation with optional local models | **[NEW] [UNVERIFIED]** | Local-model runtime exists as a direction (Ollama; VPS RAM-blocked, `MAINAI_CONTEXT_BUNDLE.md:47`), but portable-SSD packaging is undesigned. |
| Private-server and enterprise installations | **[NEW] [PROPOSED]** | Multi-tenant SaaS is a documented target (`MAINAI_ARCHITECTURE.md` §9); self-/customer-hosted install packaging is new. |
| User-approved installation through a Life conversation | **[NEW] [PROPOSED]** | Conversation-mediated install flow is undesigned; must respect host-security (see §B). |
| Device registration & trusted-device management | **[NEW] [PROPOSED]** | No device model in the domain model (`MAINAI_ARCHITECTURE.md` §4). |
| Secure migration between installations | **[NEW] [PROPOSED]** | No migration/export protocol beyond backup.sh's VPS-state archive (which is explicitly *not* a Life-to-Life migration format). |
| Encrypted, integrity-verified export/import of transferable memory, documents and settings | **[NEW] [PROPOSED]** | Memory/provenance/checksum primitives exist (`LIFE_FOUNDER_MEMORY.md`, `FOUNDER_KNOWLEDGE_BOOTSTRAP.md` checksums); a portable *encrypted* export/import envelope does not. |
| Preserve the original installation and a tested rollback path | **[NEW] [PROPOSED]** | Deploy-side rollback exists for the VPS (`scripts/vps/rollback.sh`); an installation-level rollback for Life instances is new. |
| **Constraint:** hardware-bound credentials must NOT be copied as ordinary archive data | **[NEW] constraint** | Aligns with backup.sh's existing secrets-exclusion policy (secrets are never archived). Must be carried into any export/migration design. |

## B. Life Internet & Device Access

| Requirement | Classification | Grounding / gap |
|---|---|---|
| Operate across authorized devices via a secure device-agent architecture | **[NEW] [PROPOSED]** | No device-agent component exists today. |
| Capabilities depend on OS / installed agent / permissions / runtime | **[NEW] [PROPOSED]** | Capability-gating exists conceptually (ActivationGateSet, capability_reality in the workforce layer) but not for on-device agents. |
| An internet chat cannot silently install software or bypass host security; first install needs a user-mediated trust/bootstrap step | **[NEW] constraint** (security invariant) | Consistent with the project's human-in-the-loop posture (`MAINAI_CONTEXT_BUNDLE.md:12` "Sista ordet är alltid mitt"). Must be an enforced invariant, not a UI nicety. |
| Sensitive actions require explicit authorization + appropriate step-up authentication | **[NEW] [PROPOSED]** | Auth exists (refresh-token rotation, jti-blocklist, role classes — `MAINAI_ARCHITECTURE.md` §4/§governance); step-up auth is new. |

## C. Life Business Platform

| Requirement | Classification | Grounding / gap |
|---|---|---|
| A reusable AI business platform serving different companies | **[NEW] [PROPOSED] +[BLUEPRINT-INPUT]** | Substrate exists: MainAI executive + internal workforce (#230–#236), executive business workflow (#335), model-backed workers (#336). Cross-company *reuse as a product* is new. |
| Business admin & operational coordination; financial planning/accounting; customer service/case mgmt; inventory/commerce/ordering; HR/scheduling; marketing/analytics | **[NEW] [BLUEPRINT-INPUT]** | Life OS modules exist for personal use (objects, shopping, chores, reminders — `MAINAI_CONTEXT_BUNDLE.md:42`); the business-grade versions are future Blueprint input. My Money Master is the finance product seed. |
| Specialized rentable business agents | **[NEW] [PROPOSED]** | Agent/worker abstraction exists internally; "rentable" (external, metered) is new. |
| Organizational dashboards & shared workflows | **[NEW] [BLUEPRINT-INPUT]** | Org-scope memory is a documented target (`MEMORY_ARCHITECTURE.md` §Organization). |
| Configurable agent capabilities + subscription/licensing models | **[NEW] [PROPOSED]** | No billing/licensing surface in the tree. |
| Agents reusable across business types, not rebuilt per customer | **[NEW] [PROPOSED]** | Design principle; aligns with the "don't build parallel subsystems" ethos in `MEMORY_ARCHITECTURE.md:18`. |
| **Constraint:** financial transfers, consequential HR decisions, other sensitive ops require authorized human approval | **[EXISTING] principle** | Matches the standing human-approval invariant (`MAINAI_CONTEXT_BUNDLE.md:12`) and the system prompt's financial-action boundaries. |

## D. Customer Distribution

| Requirement | Classification | Grounding / gap |
|---|---|---|
| Online chat / web apps | **[EXISTING]** | Web product exists. |
| Downloadable applications | **[NEW]** | See §A desktop packaging (undesigned). |
| USB / SSD products | **[NEW] [UNVERIFIED]** | See §A portable execution. |
| Customer-hosted servers / enterprise deployments | **[NEW] [PROPOSED]** | Depends on multi-tenant + install packaging. |
| Authorized integrations with external chat platforms | **[NEW] [PROPOSED]** | No external-chat integration layer today. |
| All delivery methods share one identity/authorization/deployment/migration architecture | **[NEW] [PROPOSED]** | This is the unifying architectural requirement across A–E; genuine gap. |

## E. Security & Tenant Separation

| Requirement | Classification | Grounding / gap |
|---|---|---|
| Founder MainAI remains separate from customer AI instances | **[NEW] [PROPOSED]** | Today there is one MainAI; separating "Founder MainAI" from tenant instances is new and security-critical. |
| Every organization has isolated data, permissions and security boundaries | **[EXISTING] target** | Documented multi-tenant target (`MAINAI_ARCHITECTURE.md` §9; `MEMORY_ARCHITECTURE.md` scopes + "never let one tenant's data leak", L544). Today is the degenerate single-tenant case (L189–194). |
| Separate tenant AND device identities | **[NEW] [PROPOSED]** | Tenant identity is a documented target; device identity is new (§B). |
| Per-tenant encryption / key management | **[NEW] [PROPOSED]** | No per-tenant key management today. |
| No cross-customer persistent memory leakage | **[EXISTING] invariant** | Directly stated as a design rule in `MEMORY_ARCHITECTURE.md` (SHARED is opt-in, never implicit; per-tenant isolation). |
| No privilege inheritance merely from physical USB access | **[NEW] constraint** | Ties to §A hardware-bound-credential constraint; new invariant for portable media. |
| Signed installation/update packages | **[NEW] [PROPOSED]** | No package-signing in the tree. |
| Explicit approval for privileged operations | **[EXISTING] principle** | Standing human-approval invariant. |
| Auditable agent actions and recoverable deployments | **[EXISTING]** | `AuditLog` (append-only) exists (`MAINAI_ARCHITECTURE.md` §4); deploy recovery via `scripts/vps/*` and PR #366 backup integrity. |

## F. Existing Historical Material

| Requirement | Classification | Grounding / gap |
|---|---|---|
| Extensive additional requirements already exist in historical documents & conversations | **[EXISTING] fact** | `MAINAI_CONTEXT_BUNDLE.md:50–54` enumerates the intake corpus (FKP_curated, 19 vision docs, 500–1000+ ChatGPT conversations, My Money Master). |
| Life must eventually ingest, classify, reconcile and understand this corpus before proposing comprehensive new Blueprints | **[EXISTING] plan + [BLUEPRINT-INPUT]** | Reconciliation primitives exist (`LIFE_FOUNDER_MEMORY.md` supersede/authority/provenance; `FOUNDER_KNOWLEDGE_BOOTSTRAP.md` import+checksum). Ingestion *readiness* of the actual corpus is assessed separately (see `docs/FOUNDER_KNOWLEDGE_INGESTION_READINESS_2026-11-08.md`). |
| **Constraint:** do not invent missing document contents | **[NEW] constraint** | Honored: this delta cites only files in the tree and does not reproduce private corpus contents. |

---

## Genuine gaps (nothing in the repository covers these yet)

1. **Installation & packaging layer** (desktop download, USB/SSD portable, customer-hosted, signed packages) — no design exists (§A, §D, §E).
2. **Device-agent architecture** (on-device agent, device identity/registration, trusted-device management, step-up auth) — none (§B, §E).
3. **Portable encrypted export/import & installation migration/rollback envelope** — primitives exist (checksums, provenance, secrets-exclusion) but no end-to-end format (§A).
4. **Founder-MainAI vs. customer-instance separation** and **per-tenant key management** — beyond the documented data-scope isolation (§E).
5. **Commercial surface** (rentable agents, subscription/licensing, billing) — none (§C).
6. **Unifying identity/authorization/deployment/migration architecture across all delivery methods** — the cross-cutting requirement in §D is undesigned.

## What is explicitly NOT claimed

- No statement here asserts that portable execution, device agents, multi-tenant separation,
  encryption/key-management, or the business platform is **implemented or verified**. Items
  tagged **[UNVERIFIED]/[PROPOSED]** are candidates pending approval and engineering proof.
- The historical corpus has **not** been ingested; its detailed requirements are not reproduced
  here and must be learned from the originals (see the ingestion-readiness assessment).

## Next steps (requirements/provenance only — not an implementation mandate)

1. Founder review of this classification; correct any mis-tag or missing conflict.
2. Feed the **[BLUEPRINT-INPUT]** items into Life's Blueprint source set **after** Founder Alpha
   passes its security gates and the historical corpus is ingested.
3. For each **[PROPOSED]** area, a separate, independently-reviewed design (one purpose = one
   branch) — none started here.

*Builder (Claude Code). Documentation-only; review-pending. `first_cycle_go=false`.*
