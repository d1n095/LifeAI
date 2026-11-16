# Founder ecosystem vision — reconciliation against existing architecture

## What this document is and is not

Founder directive, "CONTINUE ENGINEERING + ECOSYSTEM VISION AUDIT" (2026-11-15), Priority 3,
explicit framing: *"This is a documentation and architecture reconciliation assignment, not
permission to implement future products."* This document is exactly that. It creates no new
table, model, service, router, or scheduled job. `first_cycle_go=false`; no code changes
accompany this document; nothing here authorizes starting Life Dating, LifeOS, Life Social, or
any other deferred product named below.

It reconciles the founder's own directive text (quoted/paraphrased faithfully, sourced to this
PR's description where the exact wording matters) against: every existing `docs/*.md` file on
`dev` that plausibly bears on it; two currently-open sibling PRs that independently touch
pieces of the same vocabulary (#424, #426); and this session's own direct, previously-verified
reading of the backend's provider/capability/tenancy architecture. Two background research
agents were used to read the authoritative planning docs (`MAINAI_PROJECT_UNDERSTANDING_PLAN.md`,
`ARCHITECTURE.md`, `ROADMAP.md`, `MAINAI_ARCHITECTURE.md`) and the security/ops docs
(`AUTH_THREAT_MODEL.md`, `SECURITY_BLOCKERS.md`, `VPS_THREAT_MODEL.md`, `OPERATIONS.md`,
`VPS_BACKUP_RESTORE.md`) in full, with exact citations; their findings are folded in below with
spot-checks against the underlying files, not taken on faith.

**The single most important repo-wide finding, established before any of the detail below:**
a `git grep -il` sweep (excluding `docs/BRANCH_REGISTRY.md`) across the *entire* repository for
twenty of the founder's own ecosystem terms — LifeOS, Life Phone, Life Dating, Life Social,
Life Causes, Life Universe, Life World, Life Engineering, Life Security Lab, Life Rescue, Life
Forge, Life Word, Life Patent, Life Voice, Life Compute, Life Mining, Life Development Monitor,
Life App, Life Store, Life Capability Interface, AOSP — returned **zero hits on `dev` for
nineteen of them**. "LifeOS" alone returned 61 hits, and those turn out to be something else
entirely (§1 below). This means that for most of sections D, F, and G of the founder's
directive, the honest answer is **"this directive is the first written record of this idea in
this repository,"** not "here is how it reconciles with existing documentation." Treating that
absence as a gap to fill with invented detail would be exactly the "generic high-level vision
document" the founder asked this PR to avoid — so this document says so plainly, item by item,
instead.

Two currently-open PRs partially narrow that absence for specific sub-areas:
- **PR #424** (`claude/life-deferred-product-vision-vendor-philosophy`, open, not merged) —
  the founder's own earlier "Priority 4" directive, consolidating a vendor philosophy plus two
  deferred product areas: Life Innovation & Patent Intelligence, and multi-coin Life Compute &
  Mining. Directly overlaps two of §G's named areas below — cross-referenced, not duplicated.
- **PR #426** (`copilot/life-development-monitor-architecture`, open, not merged, by a
  different agent) — defines the future, deferred "Life Development Monitor," one of §G's named
  areas, in real depth (existing-foundation table, evidence/provenance requirements, explicit
  non-authority constraints). Cross-referenced below, not duplicated. Per this repo's own
  standing instruction to build on existing work rather than compete with it, this document
  treats #426 as the authoritative source for that one item and does not re-derive it.

---

## 1. Conflicts and superseded assumptions

### 1.1 The "LifeOS" naming collision — the headline finding

The founder's directive (§D) describes a future **LifeOS**: a device/system-level operating
system, distinct from LifeAI, with its own hardware/driver/update/rollback concerns, evaluating
AOSP as a possible first mobile foundation.

**"LifeOS" already exists in this repository today — as something else entirely.** It is the
*original, historical product name for this same backend/AI platform*, not a device operating
system:

- `README.md:1` — the repository's own top-level title is literally **`# Life OS / MainAI`**.
- `docs/ARCHITECTURE.md:1,5` — `# Systemarkitektur — LifeOS / MainAI`; "LifeOS är en lokalt
  körd AI-plattform som fungerar som ett företags centrala kunskaps- och arbetshjärna" (LifeOS
  is a locally-run AI platform that serves as a company's central knowledge/work brain).
- `docs/ROADMAP.md:1` — `# Roadmap — LifeOS / MainAI`.
- `docs/MAINAI_ARCHITECTURE.md:3-4` — states explicitly, in the document's own purpose
  paragraph: *"produktnamnet — repot heter `LifeAI`/"LifeOS" av historiska skäl, se
  `README.md`"* (the product name — the repo is called `LifeAI`/"LifeOS" for historical
  reasons).
- A second, unrelated internal usage: `MAINAI_ARCHITECTURE.md:68` uses "operativsystem" once,
  purely as an architectural metaphor for the backend's own internal layering ("inte en app med
  AI ovanpå — det är ett operativsystem med en egen AI" — not an app with AI bolted on, it's an
  operating system with its own AI) — a description of the System Core / MainAI / external-model
  split, unrelated to any device, kernel, or hardware concept.

**Conclusion:** the founder's new LifeOS (device OS) and the existing "LifeOS" (this backend's
own old/alternate name) are two completely different things that happen to share a name. This
is a direct, concrete instance of the kind of naming collision the founder's directive asks this
document to surface. **Recommendation:** use a visibly distinct name for the new device-OS
product in all future documentation and code (the founder's own directive text already does
this implicitly by listing "LifeOS, Life Phone and Life App" as a named triad distinct from
"LifeAI") until/unless the founder explicitly decides the historical name should be reclaimed —
and add a short cross-reference note to `README.md`/`MAINAI_ARCHITECTURE.md` so a future reader
does not conflate the two (see §5).

### 1.2 The "Workbench" naming collision (already flagged, cross-referenced here)

PR #444 (`docs/LIFE_DURABLE_WORKSPACES_GAP_ANALYSIS.md`) already found and documented that
`app/routers/workbench.py` (`/api/workbench`) is an existing, unrelated single-shot AI-analysis
endpoint, not a persistent multi-session concept — a naming collision with the founder's "Life
Workspaces" directive, not a functional duplicate. Not re-derived here; see that document for
the full analysis. Relevant to this document only insofar as it is the *second* instance this
session has found of the founder's new vocabulary colliding with an existing, differently-scoped
name — the same caution applies to any future "Life ___" name before it is used in code.

### 1.3 `ROADMAP.md`/`STATUS.md` are themselves marked historical by the current docs

`docs/MAINAI_ARCHITECTURE.md`'s own text (per the research agent's direct reading) states that
`docs/ARCHITECTURE.md` is "delvis föråldrat" (partially outdated — e.g. Qdrant has since been
replaced by pgvector) and that `MAINAI_ARCHITECTURE.md` itself, not `ARCHITECTURE.md` or
`ROADMAP.md`, is the current authoritative architecture document. `README.md`'s own intro
confirms this explicitly: `ROADMAP.md` and `STATUS.md` are "historiska ögonblicksbilder från Fas
0" (historical snapshots from Phase 0). **Any future ecosystem-vision work that cites
`ROADMAP.md`'s phase list as current must instead treat `MAINAI_ARCHITECTURE.md` and
`MAINAI_PROJECT_UNDERSTANDING_PLAN.md` as authoritative** — this document does so throughout.

### 1.4 No conflict found between the founder's new §B (economic philosophy) and existing docs

Checked directly: no pricing, subscription, revenue, paywall, advertising, or data-sale language
exists anywhere in `MAINAI_PROJECT_UNDERSTANDING_PLAN.md`, `ARCHITECTURE.md`, `ROADMAP.md`, or
`MAINAI_ARCHITECTURE.md` for the product itself. The only "cost" language found is internal:
AI-provider token-cost tracking (`app/providers/pricing.py`), and one adjacent-but-distinct
mention of a possible future *paid Render hosting plan* for infrastructure scaling
(`MAINAI_ARCHITECTURE.md:155`) — not user-facing pricing. `ROADMAP.md:59`'s one explicit
non-goal ("no paid vector-database service — Qdrant/pgvector runs self-hosted") is consistent
with, but does not state, the founder's new "no unnecessary subscriptions" principle. **This is
a clean slate, not a conflict** — §B has no existing documented source to reconcile against.

---

## 2. Section-by-section requirement register (individual requirements, not categories)

Each row is one individual requirement extracted from the founder's own directive text. Status
values: **Implemented** (built and verified), **Partial** (some real coverage, named gap),
**Documented elsewhere** (an open, not-yet-merged sibling PR covers it — cross-referenced, not
duplicated), **No source** (zero existing documentation anywhere in this repository; this
directive is the first written record).

### §A — review of existing project knowledge (method, not a separate requirement list)

Applied throughout this document. One concrete, previously-undocumented finding from applying
it: `MAINAI_PROJECT_UNDERSTANDING_PLAN.md`'s own §8 build order (lines 1332-1406) has **no
"research / not yet decided" status category** distinct from "designed, not built" — everything
not yet built is filed as "Målarkitektur" (target architecture). The founder's new three-way
split (implemented / planned / research) therefore has no existing precedent to map onto
cleanly; this document introduces that distinction itself rather than forcing it onto the
existing two-way implemented/designed split.

### §B — economic philosophy

| Requirement | Status | Note |
|---|---|---|
| People and product quality first | No source | Consistent in spirit with `ROADMAP.md`'s existing non-goals (no SaaS middleman, no paid vector-DB), but that is an engineering-dependency choice, not a stated economic philosophy. |
| Revenue generation not the initial architectural priority | No source | — |
| No artificial paywalls / exploitative engagement / unnecessary subscriptions / sale of private user data / excessive pricing | No source | Silence, not conflict — no existing doc proposes any of these either. |
| Evaluate transparent/sustainable pricing only when real operating costs exist | No source | One adjacent mention: possible future paid Render hosting tier for *infrastructure* cost (§1.4), not user pricing. |
| Prefer broad accessibility + modest margins over max extraction per user | No source | — |
| A financially successful, reinvesting company remains a desirable outcome | No source | — |
| Do not prematurely select a monetization model | No source | Consistent with this repo's own general engineering discipline (`CLAUDE.md`: don't build ahead of justified need) — a pattern that already exists, just not stated for monetization specifically. |

### §C — AI-optional software architecture

| Requirement | Status | Note |
|---|---|---|
| All Life apps must function without LifeAI | No source as a cross-app requirement | No second "Life app" exists yet to test this against. The *pattern* already exists for this one app: `MAINAI_ARCHITECTURE.md:66-107`'s three-layer split (System Core / MainAI / external models) treats MainAI as an optional intelligence layer on top of conventional core functionality — directly reusable framing, not a new concept. |
| Core app functionality conventional/testable/independently deployable | Partial | True for this app's System Core layer; unverified generality beyond one app. |
| LifeAI optional, permission-controlled | Partial | Extensive owner-level permission/RLS infrastructure exists (verified repeatedly this session), but no single "disable AI, keep the app working" toggle has been built or tested as such. |
| Model/provider independence mandatory, no hardcoded vendor dependence | **Implemented** | `ARCHITECTURE.md:50-77`: one `LLMProvider` Protocol, six implementations (OpenAI/Anthropic/Gemini/DeepSeek/OpenRouter/local Ollama) — "no other part of the system needs to know which provider is in use." `docs/AI_PROVIDER_ARCHITECTURE.md` §2 goes further: a capability-based interface (`Capability` enum: CHAT/EMBEDDING/VISION/TTS/…), `ProviderManifest`, `ProviderRegistry`, `CapabilityResolver`, a universal error taxonomy, and a conformance test suite any adapter must pass — exactly the reusable foundation §C's "model/provider independence mandatory" calls for, already built for AI providers specifically. |
| LifeAI may run locally or via authorized remote compute | Partial / target architecture | `MAINAI_ARCHITECTURE.md:66-107`'s "Local MainAI Capability Layer" is the stated target; chat already has cross-provider fallback; local-model fallback for chat is not yet built; RAG retrieval is already fully local (PR #18, pgvector). |
| Users must be able to disable integration / revoke permissions without losing ordinary app functionality | No source as a tested requirement | Closest existing primitive: revocable session/refresh tokens (`docs/AUTH_THREAT_MODEL.md`) — a different kind of revocation, not an "AI-disable" toggle. |

### §D — LifeOS, Life Phone, Life App, Life Capability Interface

| Requirement | Status | Note |
|---|---|---|
| LifeOS as a device/system-services operating system | No source / **naming collision** | See §1.1 — the name already exists for something unrelated. |
| Life Phone | No source | Zero hits anywhere in the repo. |
| Life App / Life Store | No source | A differently-scoped, pre-existing single-shot router happens to be named "Workbench," not "Life App" — no actual naming collision here, just worth noting the adjacent #444 finding (§1.2). |
| Life Capability Interface (permission-governed AI integration for third-party apps) | No source for the app-grant concept itself; a reusable *pattern* exists one layer down | `AI_PROVIDER_ARCHITECTURE.md` §2's capability-based interface (above) governs which AI-*provider* capabilities MainAI itself may call — not which *external application* gets access to MainAI. These are structurally different authorization directions, but the capability-scoped-interface design pattern (typed capability enum, registry, resolver, conformance tests) is a proven, reusable template for designing the as-yet-nonexistent app-facing version. |
| Evaluate AOSP as a first mobile foundation; verified boot, signed updates, rollback, driver maintenance, hardware compatibility, app sandboxing, mobile privacy, long-term security maintenance | No source | Zero mentions anywhere in the repo (confirmed by direct grep for "AOSP" specifically, in addition to the 20-term sweep). Pure research question — see §4. |

### §E — universal LifeAI integration (own apps first, licensed external integration later)

| Requirement | Status | Note |
|---|---|---|
| Own apps first; external developers apply for licensed integration later (licensing model undecided) | No source as an external-developer concept | Consistent prioritization logic already exists elsewhere: `MAINAI_ARCHITECTURE.md:293-314`'s deliberately-deferred "Organization" (multi-tenant) entity is explicitly gated on "en verklig andra-kund-drivkraft finns" (until a real second customer exists) — the same "don't build ahead of real demand" logic, applied to a different (but related) multi-tenancy question. Organization is about multiple *companies* sharing one installation, not third-party *developers'* own separate apps getting licensed access — a related but distinct future decision. |
| Explicit per-app capability grants, separate read/write permissions, revocable, strong tenant/privacy isolation, no unrestricted shared memory access | No source for apps specifically | The exact mechanism this would need to extend already exists and is proven: owner-scoped RLS (`owner_id` + policy on every tenant-isolated table, verified repeatedly this session) is the correct foundation to extend to a per-app grant, not duplicate. |
| No requirement for separate AI subscriptions per app; no automatic third-party authority; AI integration must remain removable | No source | — |

### §F — Life Dating

| Requirement | Status | Note |
|---|---|---|
| All items (genuine-human/anti-bot, anti-scam/impersonation, no illegal commercial solicitation, legal compliance, privacy-sensitive data handling, free core functions, no paid match-visibility manipulation, research-informed matching, optional outcome research) | **No source, zero hits.** | This is the clearest section-wide instance of "this directive is the first written record." Notably, the one piece of adjacent infrastructure that *would* matter here — multi-user platform moderation / anti-fraud at scale — has literally never been needed before, because this is currently a single-founder-user system (`require_founder` throughout; confirmed by the security/ops research pass). Existing anti-abuse coverage (`AUTH_THREAT_MODEL.md`'s honeypot + per-IP rate limiting) is scoped to automated-registration abuse on one app, not multi-user dating-platform moderation — a different problem at a different scale. |

### §G — other named future product areas (checked individually, not as one block)

| Area | Status | Note |
|---|---|---|
| Life Social, Life Causes, Life Universe, Life World, Life Engineering, Life Security Lab, Life Rescue & Recovery, Life Forge, Life Word/spreadsheets/Teams/business tools | **No source, zero hits.** | — |
| Life Innovation & Patent Intelligence | **Documented elsewhere** | PR #424 §2 (open, not merged) already covers this as a deferred product area: structured public patent/innovation search tied to Life Library's existing provenance/Trust-Engine architecture, explicitly not a legal-advice tool, with the specific data source/API deliberately left undecided pending real need. |
| Life Voice and Media | **Partially documented elsewhere** | PR #424 §3 covers "ElevenLabs research" (voice synthesis) specifically as a research track, not a committed integration — tied to the already-planned LifeCast podcast-production feature in `docs/LIFE_LIBRARY_PLAN.md`. Does not cover "Life Voice and Media" as broadly as the founder's new directive names it; the broader scope beyond voice synthesis has no existing source. |
| Life Compute and multi-coin Mining | **Documented elsewhere** | PR #424 §4 covers this in real depth: a "Life Mining Advisor" that must compare coins/algorithms/current hardware/upgrade cost/energy cost/temperature/difficulty/profitability before any recommendation, explicitly never assuming profitability and never locking in today's coin/hardware list — matching the founder's new directive's requirements closely. Cross-referenced, not duplicated. |
| Life Development Monitor | **Documented elsewhere** | PR #426 (open, by a different agent/session, not merged) defines this in full: a read-only, non-authoritative GitHub-evidence observer built on top of already-existing infrastructure (`GitHubClient`, `build_github_work_queue_report()`, `mainai_jobs`), with explicit non-authority constraints (no merge/deploy/approval-bypass, no inferring agent "aliveness" from commit activity, no competing scheduler/queue). This document defers entirely to #426 for this item. |
| Portable installation/migration/recovery; multi-tenant customer AI services | **No source** for "portable installation" specifically; multi-tenant customer AI services overlaps the already-deferred Organization entity (§E above) | — |

### §H — additional architecture gaps (research questions, checked against existing coverage)

Full findings from the direct security/ops documentation pass (all citations verified against
the named files):

| # | Item | Status |
|---|---|---|
| 1 | Data portability and account deletion | **Implemented.** `DELETE /api/account` (irreversible, transactional; shared content decoupled not deleted) and `GET /api/account/export` (structured JSON export of profile/conversations/audit log/S1A memory/MainAI jobs) both exist. Export is explicitly not yet a full binary/ZIP bundle of original files — a separate, not-yet-approved widening. |
| 2 | Consent / sensitive-information handling | Partial. Full on the account/auth surface. `docs/LIFE_LIBRARY_PLAN.md` §9 defines per-object `consent_flags`/`rights_basis`, but `docs/FOUNDER_KNOWLEDGE_STUDIO_HANDOVER_2026-07-20.md` notes the workflow that would advance them is deliberately not yet built. No general sensitive-data (health/biometric) classification exists. |
| 3 | Anti-fraud / platform moderation | Partial, narrow. Covers automated-registration abuse only (one app, single-user system). No multi-user moderation/anti-impersonation system exists — directly relevant to §F's absence above. |
| 4 | Accessibility / internationalization | No coverage as policy. A handful of incidental `aria-label`s and one locale-formatted number display exist; no WCAG reference, no i18n architecture. |
| 5 | Long-term update support | Partial, infra-only. OS-level `unattended-upgrades` + an active, documented dependency-patching discipline exist; no product-level LTS policy (irrelevant until a shippable product like Life Phone exists). |
| 6 | Offline-first operation | Partial, one subsystem. MainAI's own "local-first learning school" (`docs/MAINAI_LOCAL_INTELLIGENCE_SCHOOL.md`) proves offline operation for that subsystem specifically ("external APIs = teachers not brain"); the web app/backend generally are online-dependent by default. |
| 7 | Application security certification | **No coverage.** No SOC2/ISO27001/pen-test-cadence reference anywhere; security posture is internally-reasoned threat models + test-suite verification only. |
| 8 | Licensing and extension revocation (third-party developers) | **No coverage** for this meaning. Existing "revocation" hits are session/token revocation and third-party *software license* compliance (Valkey/Redis BSL vs. BSD-3) — unrelated to a future developer capability-grant system. |
| 9 | Cross-device synchronization | **No coverage.** "Logout from all devices" revokes sessions across devices but does not synchronize application state between them. |
| 10 | Disaster recovery | **Implemented**, VPS/infra scope. Tested, CI-verified backup/restore (`docs/VPS_BACKUP_RESTORE.md`) for Caddy/TLS/deploy records/uploaded files; Postgres itself is Supabase's responsibility, out of scope. Not evaluated at any future multi-tenant/ecosystem scale. |
| 11 | Resource efficiency | Partial, implicit. The provider-agnostic abstraction (§C) already enables model/provider replacement for cost reasons; no standalone compute/cost-efficiency policy exists as its own goal. |
| 12 | Independent verification / transparency | Partial, internal-process only. This whole session's own exact-SHA, disposable-branch-verified engineering discipline functions as de facto internal verification; no external-facing transparency or audit mechanism exists. |

---

## 3. Missing or insufficiently documented founder decisions

Concrete, unresolved decisions this reconciliation surfaced — not proposals, just the open
questions a future directive would need to settle:

1. Whether "LifeOS" (the historical name) should be formally retired in favor of a fully
   distinct name, or whether the founder wants to reclaim it for the new device-OS concept once
   that work begins — currently undecided, and currently a live collision risk (§1.1).
2. Whether the future app-facing "Life Capability Interface" (§D) should be designed as a
   generalization of the existing AI-provider capability-interface pattern (`Capability` enum +
   registry + resolver), or as a structurally different mechanism — no existing document
   commits to either.
3. Whether third-party-developer licensed integration (§E) is intended to compose with, replace,
   or stay entirely separate from the already-deferred Organization/multi-tenant entity
   (§E above) — both are "wait for real demand" deferrals today, but nothing says whether they
   are the same future feature or two different ones.
4. Which specific patent-data source/API, voice-synthesis vendor, and mining-coin/hardware
   target list will eventually be chosen — all three are *explicitly and deliberately* left open
   by PR #424 itself (§1's vendor-independence principle), not an omission of this document.
5. What local-model fallback timeline exists, if any, for making chat (not just RAG retrieval)
   genuinely able to run with zero external providers — relevant to §C's "may run locally"
   requirement; `MAINAI_ARCHITECTURE.md` names this as a target without a committed date.

---

## 4. Explicit future research questions

Carried forward from the per-item analysis above, collected in one place so they are not lost
inside the tables:

- AOSP viability as a first mobile foundation, and the full verified-boot/signed-update/
  rollback/driver-maintenance/hardware-compatibility/app-sandboxing/long-term-security-
  maintenance question set (§D) — zero existing material to start from.
- What evidence base, if any, would make a dating-compatibility matching algorithm
  scientifically defensible, per the founder's own explicit caution ("do not assume that any
  particular matching algorithm has scientifically proven effectiveness without evidence") — a
  research question for independent specialists, not an engineering one (§F).
- The external-developer licensing model for universal LifeAI integration (§E) — undecided by
  the founder's own words ("licensing model undecided").
- Application security certification strategy (§H.7) — whether/when a formal external audit or
  certification becomes worth pursuing, and against which standard.
- Cross-device synchronization architecture (§H.9) — genuinely unexplored; would need its own
  design once a second device/surface (e.g. a future mobile app) exists to synchronize with.
- Resource-efficiency policy as a named goal in its own right (§H.11), distinct from the
  already-existing provider-replaceability mechanism that happens to serve it indirectly.

---

## 5. Smallest justified additions to existing documentation

Consistent with the founder's instruction to avoid "another generic high-level vision document,"
this document recommends exactly one small addition beyond itself, and nothing more:

- **A short naming-collision note added to `README.md` and `docs/MAINAI_ARCHITECTURE.md`**,
  pointing future readers to this document (`docs/LIFE_ECOSYSTEM_VISION_RECONCILIATION.md`) so
  "LifeOS" is never silently assumed to mean the founder's new device-OS product. This is a
  documentation cross-reference, not a rename, and not a code change.

No other addition is recommended. In particular, this document deliberately does **not**
propose a new master "ecosystem roadmap" document beyond itself — for the ~19 of 20 areas with
zero existing source material, adding speculative structure now would manufacture detail the
founder has not yet provided, which is the exact failure mode this directive asked to be
avoided.

---

## 6. Prioritized distinction — current MainAI prerequisites vs. deferred ecosystem products

**Already a current MainAI prerequisite, proven or in active progress (not deferred):**
- §C's model/provider independence — already implemented and exercised (`AI_PROVIDER_ARCHITECTURE.md`).
- §A's architecture/knowledge layers — the subject of this entire session's prior work (S1A-S1C,
  execution authority chain, Active Context, Memory Threads, checkpoints).
- §H.1/§H.10 — account export/erasure and VPS backup/restore are built and tested today.
- The vendor-study-not-accumulate principle (PR #424 §1) — already the operative convention for
  how this whole project chooses dependencies, now generalized by the founder's new §C/§E.

**Explicitly deferred ecosystem products, gated on `MAINAI_PROJECT_UNDERSTANDING_PLAN.md`'s own
build order (§8) completing first** — this document generalizes PR #424's own "byggspärr"
(build-block) framing, originally stated for just its three product areas, to all of sections
D, F, and most of G:
- LifeOS (device OS), Life Phone, Life App/Store, Life Capability Interface (§D) — zero source,
  explicitly research-stage per §4.
- Life Dating (§F) — zero source, explicitly research-stage per §4.
- Life Social, Causes, Universe, World, Engineering, Security Lab, Rescue & Recovery, Forge,
  Word/business tools (§G) — zero source.
- Life Patent Intelligence, Life Compute & Mining, parts of Life Voice/Media, Life Development
  Monitor — already deferred and documented in depth by the two open sibling PRs (#424, #426);
  this document does not re-defer what is already deferred, it just confirms the dependency
  holds and points to the right source.

No merge, deployment, activation, or security bypass. `first_cycle_go=false`.

---
_Generated by [Claude Code](https://claude.ai/code/session_016d4iixQnxxqNhDrxHHxV4Q)_
