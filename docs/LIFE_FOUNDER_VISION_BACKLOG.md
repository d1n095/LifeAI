# Founder vision — atomic, traceable future-requirements backlog

## Purpose and how to read this document

Founder directive ("CLAUDE — FIRST-CYCLE READINESS + COMPLETE FOUNDER VISION PRESERVATION"),
Priority 3/4. This document extends `docs/LIFE_ECOSYSTEM_VISION_RECONCILIATION.md` (this same
PR) from a narrative reconciliation into an **atomic, retrievable backlog**: one stable-ID
entry per individual requirement or question, not a generic overview of 20 product names. It
is written so that a future, authorized MainAI knowledge-ingestion pass (see §4) can parse one
entry at a time, each with the same fixed schema — but **existing in GitHub today does not by
itself mean MainAI has ingested it**; see §4 for exactly what wiring that would still require.

**Every item below is documentation only.** No item's inclusion here authorizes building it.
`first_cycle_go=false`. No new table, model, service, scheduler, or parallel planning/memory
authority is introduced — every item that needs durable tracking later reuses this repository's
existing structures (GitHub issues/PRs, `docs/BRANCH_REGISTRY.md`, and, once authorized, the
existing `MemorySourceUnit`/`KnowledgeClaim`/Active Context chain already built for founder
knowledge — never a second one).

**Provenance convention.** Per the directive's own instruction: where no prior repository
documentation exists for an item (verified by the repo-wide grep already run for
`LIFE_ECOSYSTEM_VISION_RECONCILIATION.md` — 19 of 20 named ecosystem terms returned zero
hits), the item is labeled **"Founder-provided (2026-11-15/16 directives); no historical
repository source."** This is not a gap in this document — it is an honest statement that the
founder's own directive text IS the first and only source, and nothing has been invented to
fill it in.

**Schema.** Each item has: Stable ID · Domain · Source/provenance · Requirement (the founder's
own words, quoted or closely paraphrased, never invented) · Dependencies · Current status ·
Privacy/security implications · Acceptance criteria (where meaningful at this stage) ·
Activation prerequisites · Phase (pre-first-cycle prerequisite / future phase).

---

## 1. LifeOS, Life Phone, Life App, Life Store

### FV-OS-001 — LifeOS as a device/system-level operating system
- **Domain:** Platform / device OS.
- **Source/provenance:** Founder-provided (directive §D); no historical repository source.
- **Requirement:** A future operating system and system services layer, distinct from LifeAI
  (the optional intelligence layer) and from Life App/Store (the application environment).
- **Dependencies:** A device/hardware target (see FV-OS-002); a decision on AOSP as a possible
  first mobile foundation (explicitly not committed — "do not commit prematurely to a custom
  kernel or manufacturing strategy").
- **Status:** No source. **Naming collision already flagged and resolved in
  `docs/LIFE_ECOSYSTEM_VISION_RECONCILIATION.md` §1.1** — "LifeOS"/"Life OS" already exists in
  this repository as the historical, superseded name for the CURRENT LifeAI/MainAI backend
  (`README.md:1`). This item's name must stay visibly distinct from that historical usage
  until/unless the founder explicitly decides otherwise.
- **Privacy/security implications:** Verified boot, signed updates, rollback protection, driver
  maintenance, hardware compatibility, app sandboxing, mobile privacy, long-term security
  maintenance — all named by the founder as considerations, none designed yet.
- **Acceptance criteria:** Not yet defined — research-stage.
- **Activation prerequisites:** A founder decision on practical mobile foundation (AOSP or
  otherwise); MainAI's own build-order (§8) substantially complete first, per this repo's
  established "byggspärr" (build-block) convention (PR #424).
- **Phase:** Future phase (deferred platform research), gated behind MainAI prerequisites.

### FV-OS-002 — Life Phone
- **Domain:** Platform / device hardware.
- **Source/provenance:** Founder-provided (directive §D); no historical repository source;
  zero repo-wide hits for "Life Phone."
- **Requirement:** A future device/hardware product, implied as the hardware counterpart to
  LifeOS.
- **Dependencies:** FV-OS-001.
- **Status:** No source.
- **Privacy/security implications:** Same hardware-security list as FV-OS-001.
- **Acceptance criteria:** Not yet defined.
- **Activation prerequisites:** Same as FV-OS-001.
- **Phase:** Future phase.

### FV-APP-001 — Life App / Life Store (application environment and distribution)
- **Domain:** Platform / app distribution.
- **Source/provenance:** Founder-provided (directive §D); no historical repository source.
- **Requirement:** An application environment and distribution mechanism for independent Life
  applications, separate from LifeOS and from LifeAI itself.
- **Dependencies:** FV-APP-002 (independent-app architecture) should exist conceptually before
  a store/distribution layer has anything to distribute.
- **Status:** No source. Adjacent-but-unrelated existing name: `app/routers/workbench.py`'s
  single-shot "Workbench" endpoint (flagged in PR #444, not a collision with this item).
- **Privacy/security implications:** App sandboxing, update/signing (shared with FV-OS-001).
- **Acceptance criteria:** Not yet defined.
- **Activation prerequisites:** FV-OS-001/002 or an alternative non-device distribution
  decision.
- **Phase:** Future phase.

### FV-APP-002 — All Life applications function without LifeAI (AI-optional architecture)
- **Domain:** Cross-cutting architecture principle.
- **Source/provenance:** Founder-provided (directive §C); the underlying PATTERN already
  exists for the current app — `docs/MAINAI_ARCHITECTURE.md:66-107`'s three-layer split
  (System Core / MainAI / external models) treats MainAI as an optional intelligence layer on
  top of conventional, independently-functioning core software.
- **Requirement:** "All Life applications must function without LifeAI; core app functionality
  must be conventional, testable, independently deployable software; LifeAI must be optional
  and permission-controlled; users must be able to disable integration or revoke permissions
  without losing ordinary app functionality."
- **Dependencies:** None technical — this is an architectural constraint on every future app,
  not a feature to build once.
- **Status:** **Partial.** The pattern is proven for THIS app's System Core layer; no second
  "Life app" exists yet to verify the principle generalizes; no single "disable AI, keep app
  working" toggle has been built or tested as such (see
  `docs/LIFE_ECOSYSTEM_VISION_RECONCILIATION.md` §2, §C row).
- **Privacy/security implications:** Revocable AI permission grants must not be able to
  silently degrade to "AI still has access" — a real design question for the first app that
  implements a revoke toggle.
- **Acceptance criteria:** A future app demonstrably passes its own test suite with LifeAI
  integration fully disabled/unconfigured.
- **Activation prerequisites:** None blocking — this is a standing design constraint, not a
  gated capability.
- **Phase:** **Pre-first-cycle guiding principle** (applies to how MainAI's own work on THIS
  app should already be organized, not gated behind a future phase) — but not a technical
  blocker, since the pattern is already substantially followed.

---

## 2. Universal LifeAI integration and licensed third-party access

### FV-INT-001 — Universal LifeAI integration, own apps first
- **Domain:** Platform / AI integration.
- **Source/provenance:** Founder-provided (directive §E); no historical repository source for
  the universal-integration framing specifically.
- **Requirement:** Implement LifeAI integration in the founder's own apps first; external
  developers may later apply for licensed integration (licensing model explicitly undecided).
- **Dependencies:** FV-APP-001/002; FV-INT-002 (capability-grant model) before any external
  developer access is meaningful.
- **Status:** No source for the external-developer framing. Consistent prioritization logic
  already exists elsewhere in this repo: `docs/MAINAI_ARCHITECTURE.md:293-314`'s deliberately
  deferred "Organization" (multi-tenant) entity, gated on "a real second customer exists" — the
  same discipline, applied to a related but distinct question (see FV-TEN-001).
- **Privacy/security implications:** See FV-INT-002.
- **Acceptance criteria:** Not yet defined.
- **Activation prerequisites:** A real second consumer of this integration (internal or
  external) before building speculative capability.
- **Phase:** Future phase.

### FV-INT-002 — Life Capability Interface (per-app, permission-governed AI integration)
- **Domain:** Platform / AI integration, security.
- **Source/provenance:** Founder-provided (directive §D, §E); no historical repository source
  for a third-party-app-facing capability interface.
- **Requirement:** "Explicit per-app capability grants; separate read/write permissions;
  revocable authorization; strong tenant/privacy isolation; no unrestricted shared memory
  access; no requirement for separate AI subscriptions across every app; no automatic authority
  for third-party apps; AI integration must remain removable."
- **Dependencies:** FV-INT-001.
- **Status:** No source for this exact concept. **A reusable design pattern already exists one
  layer down and should be the template, not duplicated**: `docs/AI_PROVIDER_ARCHITECTURE.md`
  §2's capability-based interface (`Capability` enum, `ProviderManifest`, `ProviderRegistry`,
  `CapabilityResolver`) — but that governs which AI-*provider* capabilities MainAI itself may
  call, not which *external application* gets access to MainAI. Structurally different
  authorization direction; the PATTERN (typed capability set + registry + resolver +
  conformance tests) is proven and reusable, the concept itself is not built.
- **Privacy/security implications:** This is the single most security-sensitive item in this
  entire backlog — "no unrestricted shared memory access" directly implies extending this
  repo's own proven owner-scoped RLS pattern (verified repeatedly this session) to a
  per-app/per-tenant grant, never inventing a separate isolation mechanism.
- **Acceptance criteria:** A third-party app can be granted read-only access to one named
  capability, verified to be unable to read anything outside that grant, and have that grant
  revoked with immediate effect (no caching, no stale-token window) — mirroring this repo's own
  existing session-revocation tests.
- **Activation prerequisites:** FV-INT-001; a founder decision on the licensing model (currently
  undecided by the founder's own words).
- **Phase:** Future phase.

---

## 3. Life Dating

### FV-DAT-001 — Core platform integrity (genuine users, anti-bot, anti-scam)
- **Domain:** Consumer product / trust & safety.
- **Source/provenance:** Founder-provided (directive §F); no historical repository source;
  zero repo-wide hits for "Life Dating."
- **Requirement:** Genuine human users with strong anti-bot defenses; anti-scam/impersonation/
  spam protections; no commercial exploitation of dating accounts; no illegal
  sales/inappropriate commercial solicitation; compliance with applicable national laws and
  safety obligations.
- **Dependencies:** Multi-user platform moderation infrastructure, which **does not exist
  anywhere in this repo today** — this is currently a single-founder-user system
  (`require_founder` throughout), so there is no prior art to build on, not even partially.
- **Status:** No source, zero existing coverage of any kind — the clearest instance in this
  entire backlog of "this directive is the first written record."
- **Privacy/security implications:** Location and personal-data handling at a scale and
  sensitivity this repo has never had to handle; a dedicated threat model would need to be
  written from scratch, not adapted from `docs/AUTH_THREAT_MODEL.md`.
- **Acceptance criteria:** Not yet defined.
- **Activation prerequisites:** A multi-user moderation/anti-fraud foundation (not yet started
  anywhere, including for non-dating purposes); legal review per jurisdiction.
- **Phase:** Future phase.

### FV-DAT-002 — Free-first access and no paid match-visibility manipulation
- **Domain:** Consumer product / economic philosophy intersection.
- **Source/provenance:** Founder-provided (directive §F).
- **Requirement:** Free matching, communication, and essential safety functions; no paid
  manipulation of romantic match visibility.
- **Dependencies:** FV-DAT-001.
- **Status:** No source. Directly consistent with the cross-cutting economic philosophy items
  in §9 below — this is that philosophy applied to one specific product.
- **Privacy/security implications:** None beyond FV-DAT-001.
- **Acceptance criteria:** Not yet defined.
- **Activation prerequisites:** FV-DAT-001.
- **Phase:** Future phase.

### FV-DAT-003 — Research-informed compatibility matching
- **Domain:** Consumer product / research.
- **Source/provenance:** Founder-provided (directive §F).
- **Requirement:** Research-informed compatibility matching, with potential cooperation with
  independent researchers/matching specialists, and voluntary privacy-preserving evaluation of
  relationship outcomes. Explicit founder caution: "do not assume that any particular matching
  algorithm has scientifically proven effectiveness without evidence."
- **Dependencies:** FV-DAT-001.
- **Status:** No source. This is explicitly a **research question**, not an assumed-missing
  implementation — see the roadmap's long-horizon research bucket (§5).
- **Privacy/security implications:** Any outcome-evaluation research must be voluntary and
  privacy-preserving by the founder's own explicit instruction.
- **Acceptance criteria:** Not applicable at this stage — this is a research question.
- **Activation prerequisites:** FV-DAT-001; an actual research partnership or methodology,
  neither of which exists today.
- **Phase:** Future phase, long-horizon research.

---

## 4. Life Social, Life Causes, and community services

### FV-SOC-001 — Life Social
- **Domain:** Consumer product / social platform.
- **Source/provenance:** Founder-provided (directive §G); no historical repository source;
  zero repo-wide hits.
- **Requirement:** Named as a future product area; no further detail given in the founder's
  directive text beyond the name itself.
- **Dependencies:** Likely overlaps FV-DAT-001's moderation/trust-and-safety foundation once
  that exists, but this is not stated by the founder — recorded as a plausible dependency, not
  invented as a requirement.
- **Status:** No source.
- **Privacy/security implications:** Same class as FV-DAT-001 (multi-user platform at scale).
- **Acceptance criteria:** Not yet defined.
- **Activation prerequisites:** Not yet defined.
- **Phase:** Future phase.

### FV-CAU-001 — Life Causes, petitions, donations, and community services
- **Domain:** Consumer product / civic/community.
- **Source/provenance:** Founder-provided (directive §G); no historical repository source;
  zero repo-wide hits for "Life Causes."
- **Requirement:** Named as a future product area (causes, petitions, donations, community
  services); no further detail given.
- **Dependencies:** Payment/donation handling would eventually intersect the economic
  philosophy items in §9 (no exploitative mechanisms, transparent costs) — recorded as a
  foreseeable intersection, not a stated requirement.
- **Status:** No source.
- **Privacy/security implications:** Donation/payment handling, if ever built, would need its
  own dedicated security review — not assumed safe by extension from anything existing today.
- **Acceptance criteria:** Not yet defined.
- **Activation prerequisites:** Not yet defined.
- **Phase:** Future phase.

---

## 5. Life Workspaces

### FV-WSP-001 — Durable, multi-workspace sessions
- **Domain:** Core MainAI capability (not a deferred consumer product).
- **Source/provenance:** Founder-provided ("RESUME ACTIVE DEVELOPMENT" directive, Priority 3);
  **already has a full, dedicated gap analysis** — `docs/LIFE_DURABLE_WORKSPACES_GAP_ANALYSIS.md`
  (PR #444). Not re-derived here; cross-referenced only, per this repo's own standing
  instruction not to duplicate existing work.
- **Requirement:** Multiple independently resumable workspaces; saved annotations/highlights/
  references/task state; safe project switching; crash/restart recovery; detection of changed
  source versions; no cross-workspace/tenant leakage; no duplicate external actions after
  recovery; bounded model context/resource usage.
- **Dependencies:** `ActiveContextSet`, `MainAIGoal`/`MainAITask`, `MainAICheckpoint` — all
  already built and reused, per PR #444's own analysis; no new system.
- **Status:** **Partial.** Six of eight founder-named requirements are already fully covered by
  reusing existing architecture. Two genuine gaps remain (a workspace-level grouping unit; a
  workspace-level source-change-detection wiring), both with their exact acceptance tests and
  dependencies already recorded (PR #444's own addendum).
- **Privacy/security implications:** Owner-scoped RLS already covers the reused primitives;
  the as-yet-unbuilt grouping unit must extend that pattern, not introduce a new one.
- **Acceptance criteria:** See PR #444's "Acceptance tests and dependencies" section directly —
  not duplicated here.
- **Activation prerequisites:** A concrete, named consuming feature that actually needs the
  grouping unit — none exists yet, which is itself why implementation was correctly deferred.
- **Phase:** Pre-first-cycle-adjacent (this is current MainAI capability work, not a deferred
  ecosystem product), but the two remaining gaps are themselves deferred pending real demand.

---

## 6. Life Word, Sheets, Teams, and other productivity tools

### FV-PRD-001 — Life Word (documents)
- **Domain:** Productivity tools.
- **Source/provenance:** Founder-provided (directive §G); no historical repository source.
- **Requirement:** Named as a future productivity tool; no further detail given.
- **Dependencies:** Not stated.
- **Status:** No source.
- **Privacy/security implications:** Document-level permissions would need their own design.
- **Acceptance criteria:** Not yet defined.
- **Activation prerequisites:** Not yet defined.
- **Phase:** Future phase.

### FV-PRD-002 — Life Sheets (spreadsheets)
- **Domain:** Productivity tools.
- **Source/provenance:** Founder-provided (directive §G); no historical repository source.
- **Requirement:** Named as a future productivity tool; no further detail given.
- **Dependencies:** Not stated.
- **Status:** No source.
- **Privacy/security implications:** Not yet defined.
- **Acceptance criteria:** Not yet defined.
- **Activation prerequisites:** Not yet defined.
- **Phase:** Future phase.

### FV-PRD-003 — Life Teams and other business productivity tools
- **Domain:** Productivity tools / business collaboration.
- **Source/provenance:** Founder-provided (directive §G); no historical repository source.
- **Requirement:** Named as a future productivity/collaboration tool; no further detail given.
- **Dependencies:** Likely intersects FV-TEN-001 (multi-tenant business AI) if it involves
  cross-organization collaboration — recorded as a foreseeable intersection, not a stated
  requirement.
- **Status:** No source.
- **Privacy/security implications:** Cross-organization data isolation, if built, would need
  its own dedicated review.
- **Acceptance criteria:** Not yet defined.
- **Activation prerequisites:** Not yet defined.
- **Phase:** Future phase.

---

## 7. Life Engineering, research, invention, and patent tools; Life Security Lab

### FV-ENG-001 — Life Innovation & Patent Intelligence
- **Domain:** Research/knowledge tooling.
- **Source/provenance:** Founder-provided (directive §G); **already has a dedicated section**
  in the open, unmerged PR #424 (`docs/LIFE_DEFERRED_PRODUCT_VISION_AND_VENDOR_PHILOSOPHY.md`
  §2). Cross-referenced, not duplicated.
- **Requirement:** A future knowledge/analysis area on top of Life Library to help the founder
  understand existing innovations, patent landscapes, and technical possibilities — explicitly
  NOT an automated patent-filing or legal tool, and not a replacement for professional advice.
- **Dependencies:** Life Library's existing provenance/Trust-Engine architecture
  (`docs/LIFE_LIBRARY_PLAN.md`).
- **Status:** Documented elsewhere (PR #424, open). No specific data source, API, or
  third-party service is chosen — deliberately, per PR #424's own vendor-study-not-accumulate
  principle (see §9).
- **Privacy/security implications:** None beyond the existing Life Library provenance model
  until a real external data source is chosen.
- **Acceptance criteria:** Not yet defined (PR #424 is itself still a vision document, not an
  implementation spec).
- **Activation prerequisites:** MainAI's own build order substantially complete (PR #424's
  "byggspärr").
- **Phase:** Future phase.

### FV-ENG-002 — Life Engineering and research tools (general)
- **Domain:** Research/engineering tooling.
- **Source/provenance:** Founder-provided (directive §G); no historical repository source
  beyond FV-ENG-001's narrower patent-intelligence scope.
- **Requirement:** Named as a future product area (general engineering/research/invention
  tooling); no further detail given beyond what FV-ENG-001 already covers for the patent
  sub-area specifically.
- **Dependencies:** Not stated.
- **Status:** No source for the general case.
- **Privacy/security implications:** Not yet defined.
- **Acceptance criteria:** Not yet defined.
- **Activation prerequisites:** Not yet defined.
- **Phase:** Future phase.

### FV-SEC-001 — Life Security Lab
- **Domain:** Security research tooling.
- **Source/provenance:** Founder-provided (directive §G); no historical repository source;
  zero repo-wide hits for "Life Security Lab." **Note:** PR #426's "Life Development Monitor"
  (see FV-DEV-001) explicitly mentions that "Security Lab may consume relevant security
  findings through its own authorized review path" — i.e., a *different, open* document
  already anticipates this item's existence as a future consumer, without itself defining it.
- **Requirement:** Named as a future product area; no further detail given.
- **Dependencies:** Whatever security-finding evidence surface FV-DEV-001 eventually builds,
  per that document's own forward reference.
- **Status:** No source for this item's own definition.
- **Privacy/security implications:** A security-focused product would need its own, probably
  stricter, review standard than general features.
- **Acceptance criteria:** Not yet defined.
- **Activation prerequisites:** Not yet defined.
- **Phase:** Future phase.

### FV-DEV-001 — Life Development Monitor
- **Domain:** Engineering tooling (observability over LifeAI's own development).
- **Source/provenance:** **Already fully documented** in open PR #426
  (`docs/LIFE_DEVELOPMENT_MONITOR.md`, by a different agent session/lineage). Not re-derived
  here — this entry exists only so the backlog has a stable ID pointing at it.
- **Requirement:** See PR #426 directly — a future, read-only, non-authoritative capability for
  observing verifiable GitHub engineering activity and presenting source-grounded summaries;
  explicitly not an agent, task queue, approval system, or supervisor.
- **Dependencies:** Per PR #426: PR #395 (`GitHubClient` read methods), PR #396 (stateless
  work-queue evidence report, whose own open proposal-staging question this item does not
  resolve).
- **Status:** Documented elsewhere (PR #426, open, not merged).
- **Privacy/security implications:** Read-only credentials only; must never inherit
  write-enabled configuration; must redact secret-bearing alert details — all specified in
  PR #426 itself.
- **Acceptance criteria:** See PR #426 directly.
- **Activation prerequisites:** PR #396's dependency review and merge, per PR #426's own text.
- **Phase:** Future phase (PR #426's own framing: "implementation remains deferred").

---

## 8. Portable installation/recovery; multi-tenant business AI agents; provider independence

### FV-REC-001 — Portable installation, migration, and recovery
- **Domain:** Platform operations.
- **Source/provenance:** Founder-provided (directive §G); no historical repository source for
  the cross-platform "portable installation" framing specifically. **Partially adjacent
  existing coverage**: `docs/VPS_BACKUP_RESTORE.md` is tested, CI-verified disaster recovery for
  the CURRENT single-VPS deployment topology only — not evaluated at any future
  multi-install/portable scale.
- **Requirement:** Named as a future capability; no further detail given beyond the name.
- **Dependencies:** Depends on which future platform(s) (LifeOS/Life Phone, a future hosted
  offering) "portable" needs to span — undecided.
- **Status:** No source for the general, cross-platform case. Existing VPS backup/restore is
  real but narrowly scoped (see `docs/LIFE_FIRST_CYCLE_READINESS_MATRIX.md` row 7/10 for the
  current, already-resolved infra-level disaster-recovery finding).
- **Privacy/security implications:** Not yet defined.
- **Acceptance criteria:** Not yet defined.
- **Activation prerequisites:** A concrete second deployment target to port to.
- **Phase:** Future phase.

### FV-TEN-001 — Multi-tenant business AI agents
- **Domain:** Platform / multi-tenancy.
- **Source/provenance:** Founder-provided (directive §G); **a related, already-deferred
  concept exists**: `docs/MAINAI_ARCHITECTURE.md:293-314`'s "Organization" entity (multiple
  companies sharing one installation), explicitly deferred until "a real second customer
  exists." This is about multiple *companies*, not third-party *developers'* own separate apps
  (that's FV-INT-001/002) — a related but distinct question.
- **Requirement:** Named as a future capability (business customers running their own AI
  agents on this platform); no further detail given.
- **Dependencies:** The Organization entity above, if that is the intended mechanism — not
  confirmed by the founder.
- **Status:** No source for this item specifically; a directly adjacent, already-deferred
  concept exists and should be reused/extended rather than duplicated if this is ever
  authorized.
- **Privacy/security implications:** Cross-tenant isolation at a stronger bar than today's
  single-founder-user RLS has ever been tested against.
- **Acceptance criteria:** Not yet defined.
- **Activation prerequisites:** A real second customer, per the Organization entity's own
  existing deferral condition.
- **Phase:** Future phase.

### FV-PRV-001 — Local/cloud AI and provider independence
- **Domain:** Cross-cutting architecture principle.
- **Source/provenance:** Founder-provided (directive §C, §G); **substantially already
  implemented**.
- **Requirement:** "LifeAI may run locally or via authorized remote compute... model/provider
  independence mandatory, no hardcoded vendor dependence."
- **Dependencies:** None remaining for the AI-provider case specifically.
- **Status:** **Implemented** for AI providers: `docs/AI_PROVIDER_ARCHITECTURE.md` §2's
  capability-based interface, six working provider implementations (OpenAI/Anthropic/Gemini/
  DeepSeek/OpenRouter/local Ollama). **Partial/target architecture** for "runs fully locally":
  `docs/MAINAI_ARCHITECTURE.md`'s "Local MainAI Capability Layer" target — chat has
  cross-provider fallback; no local-model fallback for chat yet; RAG retrieval is already fully
  local (PR #18, pgvector).
- **Privacy/security implications:** None beyond what's already covered by the existing egress
  gate (`docs/LIFE_FIRST_CYCLE_READINESS_MATRIX.md` row 6).
- **Acceptance criteria:** A future local-model fallback path for chat, matching RAG's
  already-local precedent — not yet scoped.
- **Activation prerequisites:** None blocking further use of what already exists; local-chat
  fallback has no committed date.
- **Phase:** Pre-first-cycle (already substantially satisfied); the remaining local-chat-
  fallback gap is future phase.

---

## 9. Economic philosophy (cross-cutting principles, not product items)

### FV-ECO-001 — People and product quality first; revenue secondary
- **Domain:** Company philosophy (cross-cutting, applies at every phase).
- **Source/provenance:** Founder-provided (2026-11-15/16 directives). No historical repository
  source — confirmed by direct grep of the four core planning docs during this session's own
  research: zero mentions of monetization, pricing, subscriptions, revenue, paywalls,
  advertising, or data sale anywhere in `MAINAI_PROJECT_UNDERSTANDING_PLAN.md`,
  `ARCHITECTURE.md`, `ROADMAP.md`, or `MAINAI_ARCHITECTURE.md`. This is a clean slate, not a
  conflict with anything already written.
- **Requirement:** "Build a successful, sustainable company without exploiting the people
  using its products. Revenue generation is secondary to genuine utility and reasonable costs."
  No artificial paywalls, exploitative engagement mechanisms, unnecessary subscriptions, sale
  of private user data, or excessive pricing.
- **Dependencies:** None — this governs every future product decision, not a build task.
- **Status:** No source; founder-stated principle, recorded verbatim-in-spirit, nothing
  invented beyond it.
- **Privacy/security implications:** "No sale of private user data" is itself a privacy
  commitment that should be checked against any future feature that handles user data at
  scale (e.g., FV-DAT-001, FV-SOC-001).
- **Acceptance criteria:** Not applicable — a principle, not a feature.
- **Activation prerequisites:** None.
- **Phase:** Cross-cutting — applies now and at every future phase, not gated behind one.

### FV-ECO-002 — Transparent, sustainable pricing only when real costs exist
- **Domain:** Company philosophy.
- **Source/provenance:** Founder-provided (2026-11-15/16 directives); no historical repository
  source. One adjacent, pre-existing, narrower mention: `docs/MAINAI_ARCHITECTURE.md:155`
  names a *possible future paid Render hosting plan* for infrastructure cost scaling — about
  infrastructure cost, not user-facing pricing, and not the same commitment as this item.
- **Requirement:** "Evaluate transparent and sustainable pricing when real operating costs
  exist. Prefer broad accessibility and modest margins across many users over maximum
  extraction per individual. Do not prematurely select monetization models."
- **Dependencies:** FV-ECO-001.
- **Status:** No source.
- **Privacy/security implications:** None directly.
- **Acceptance criteria:** Not applicable.
- **Activation prerequisites:** None — explicitly deferred by the founder's own words
  ("do not prematurely select").
- **Phase:** Cross-cutting.

### FV-ECO-003 — Future profits may fund accessible public services
- **Domain:** Company philosophy.
- **Source/provenance:** Founder-provided (2026-11-16 directive) — **new language not present
  in any earlier directive this session recorded**, distinct from FV-ECO-001/002's
  already-documented principles.
- **Requirement:** "Building a financially successful, reinvesting company remains desirable.
  Potential future business profits may help fund accessible public services and contributions
  to society."
- **Dependencies:** FV-ECO-001/002 — this does not relax either; it is an additional, forward-
  looking statement about what success could eventually enable, not a monetization plan.
- **Status:** No source; newly founder-stated, recorded verbatim-in-spirit, nothing invented.
- **Privacy/security implications:** None directly.
- **Acceptance criteria:** Not applicable — an aspirational principle, not a feature.
- **Activation prerequisites:** None — explicitly contingent on the company actually becoming
  successful first, which is itself contingent on everything else in this backlog and beyond.
- **Phase:** Cross-cutting, long-horizon.

---

## 10. Already-covered items, cross-referenced rather than duplicated

For completeness and traceability, these founder-named items already have a dedicated, open
source document and are NOT re-derived as new backlog entries:

| Item | Where it is already covered |
|---|---|
| Life Patent Intelligence | FV-ENG-001 above → PR #424 §2 |
| Multi-coin Life Compute & Mining | PR #424 §4 (Life Mining Advisor — coin/algorithm/hardware/energy/difficulty/profitability comparison, never assuming profitability, never locking in today's technology list) |
| ElevenLabs/voice-synthesis research (part of "Life Voice and Media") | PR #424 §3 — research track only, no vendor chosen |
| Life Development Monitor | FV-DEV-001 above → PR #426 |
| Life Workspaces | FV-WSP-001 above → PR #444 |
| LifeOS naming collision | `docs/LIFE_ECOSYSTEM_VISION_RECONCILIATION.md` §1.1 (this same PR) |
| `/api/workbench` naming collision | PR #444 |
| Vendor philosophy ("study for ideas, don't accumulate vendors") | PR #424 §1 — the same principle the founder's new §C/§E generalizes |

---

## 11. Multi-model AI workforce (cost-efficient, vendor-independent)

Founder directive ("MULTI-MODEL WORKFORCE + STRATO TERMINAL" / "NEW FOUNDER DIRECTIVE",
2026-11-16, two consolidated messages treated as one). **Checked first, per standing
instruction:** `backend/app/providers/registry.py` already defines six real provider classes
— `openai`, `anthropic`, `gemini`, **`deepseek`** (already a dedicated provider, not merely a
future candidate), `openrouter`, `ollama`. OpenRouter and Ollama are both model-string-routed,
meaning **every one of the eight named candidate models is already technically reachable
through existing adapters today, without a new provider class** — OpenRouter hosts
Qwen/Mistral/GLM/MiniMax/gpt-oss-style models by name; Ollama can run Qwen/Mistral/Gemma/OLMo/
GLM/DeepSeek locally by model string. This changes the shape of what's genuinely missing: not
"build six new provider integrations," but "evaluate specific models through adapters that
already exist."

### FV-WRK-001 — Evaluation roster (not automatically activated providers)
- **Domain:** AI provider strategy / cost.
- **Source/provenance:** Founder-provided (2026-11-16); no historical repository source for
  this specific roster.
- **Requirement:** Register Qwen, DeepSeek, OpenAI gpt-oss, Mistral, Gemma, OLMo, GLM, and
  MiniMax as **evaluation candidates**, explicitly not automatically activated providers. The
  founder may expand this roster as more suitable models become available.
- **Dependencies:** FV-WRK-006 (benchmarking) before any candidate is recommended for real use.
- **Status:** Partial foundation already exists (see the section intro above) — DeepSeek
  already has a dedicated provider; the other seven are reachable via OpenRouter/Ollama
  model-string routing, confirmed by reading `backend/app/providers/registry.py` directly.
  No evaluation has been run on any of them.
- **Privacy/security implications:** Same egress-policy boundary that already gates
  `chat_with_fallback()` (`docs/LIFE_FIRST_CYCLE_READINESS_MATRIX.md` row 6) must cover any
  new model exercised through these adapters — not a new boundary, the existing one applied.
- **Acceptance criteria:** Not yet defined — gated on FV-WRK-006's benchmarking design.
- **Activation prerequisites:** None of these models may be called with real API spend until
  FV-WRK-007's spend-control requirement is satisfied for that specific call path.
- **Phase:** Future phase (evaluation, not activation).

### FV-WRK-002 — Reuse existing architecture; no competing system
- **Domain:** Architecture discipline (cross-cutting).
- **Source/provenance:** Founder-provided (2026-11-16), explicit requirement #1 of the
  directive.
- **Requirement:** "Reuse the existing provider architecture, OpenRouter/Ollama adapters,
  execution authority, workforce coordination, spend controls and egress protection. Do not
  build a competing system."
- **Dependencies:** None — this is a constraint on how every other item in this section must
  be built, not a feature itself.
- **Status:** The reusable foundation is confirmed to exist and was directly verified this
  session, not assumed: provider registry + OpenRouter/Ollama (above); execution authority
  (`app.mainai_execution`, confirmed canonical per `docs/LIFE_EXECUTION_AUTHORITY_CANONICAL_MAPPING.md`);
  coding-agent adapter abstraction (`AgentAdapter` Protocol, `LocalCLIAdapter`,
  `app/agent_coordination/adapters.py` — PR #406 already registered Copilot as a supported
  adapter type using this exact mechanism); spend controls (`ProviderSpendAuthorization`,
  `authorize_provider_spend()`/`revoke_provider_spend()`, `app/provider_spend/service.py`);
  egress protection (`app/egress_policy`, confirmed closed for the chat/RAG paths in
  `docs/LIFE_FIRST_CYCLE_READINESS_MATRIX.md` row 6).
- **Privacy/security implications:** None beyond what each reused component already enforces.
- **Acceptance criteria:** Any future multi-model-workforce PR is reviewed against this list —
  a new table/service duplicating any of the above is a rejection criterion, not a design
  choice.
- **Activation prerequisites:** None — this is already satisfiable with existing code.
- **Phase:** Cross-cutting principle, applies from the first implementation PR onward.

### FV-WRK-003 — Keep model families, API providers, and coding-agent runtimes separate
- **Domain:** Architecture discipline.
- **Source/provenance:** Founder-provided (2026-11-16), requirement #2.
- **Requirement:** Keep model families (e.g. "Qwen"), API providers (e.g. "OpenRouter"), and
  coding-agent runtimes (e.g. "Claude Code," "Codex," "Copilot") as distinct concepts, never
  conflated.
- **Dependencies:** FV-WRK-002.
- **Status:** The existing codebase already draws exactly this distinction structurally:
  `_PROVIDERS` (API providers) vs. a model string passed to one (model family) vs. the
  separate `AgentAdapter` abstraction (coding-agent runtime) are three different types in
  three different modules today — not something to newly invent, just something to keep
  intact as this roster grows.
- **Privacy/security implications:** None beyond FV-WRK-002.
- **Acceptance criteria:** A new model candidate is added as a model string against an
  existing provider, never as a new top-level "provider" class unless it genuinely needs its
  own API integration (as DeepSeek already does).
- **Activation prerequisites:** None.
- **Phase:** Cross-cutting principle.

### FV-WRK-004 — Multiple concurrent coding assignments with isolated worktrees
- **Domain:** Execution infrastructure.
- **Source/provenance:** Founder-provided (2026-11-16), requirement #3.
- **Requirement:** Support multiple concurrent coding assignments, each with its own isolated
  worktree.
- **Dependencies:** FV-WRK-002.
- **Status:** **Substantially already built.** `mainai_task_worktrees` (confirmed via
  `app/mainai_execution/recovery_inspector.py`'s own description: "does an isolated checkout")
  already gives each task's execution attempt its own worktree — the per-task isolation this
  requirement asks for already exists for the one coding-agent runtime (the human-operated
  session) that is live today. Extending it to multiple SIMULTANEOUS candidate-model
  assignments running in parallel (not just multiple sequential tasks) has not been built or
  tested.
- **Privacy/security implications:** Concurrent worktrees must not share mutable state or
  credentials across assignments — an extension of the existing per-task isolation, not a new
  isolation concept.
- **Acceptance criteria:** Two concurrent assignments against different candidate models
  provably cannot read or write each other's worktree contents.
- **Activation prerequisites:** FV-WRK-001's evaluation roster producing at least one
  candidate worth running concurrently with the existing workflow.
- **Phase:** Future phase (the single-assignment case already works; the concurrent case does
  not exist yet).

### FV-WRK-005 — Genuinely independent review across different model families + deterministic CI
- **Domain:** Quality / security review process.
- **Source/provenance:** Founder-provided (2026-11-16), requirement #4.
- **Requirement:** Require genuinely independent review using different underlying model
  families, followed by deterministic CI verification — never the same model reviewing its
  own output.
- **Dependencies:** FV-WRK-001 (at least two independent candidate families available).
- **Status:** No source for cross-model independent review specifically. The DETERMINISTIC CI
  half already exists and is exercised every PR this session has opened (exact-SHA
  `get_check_runs` verification, real backend test suites). The cross-model-family review half
  does not exist — today's "independent review" is a human (the founder) reviewing Claude's
  own output, not a second, different model family.
- **Privacy/security implications:** A reviewing model must not receive broader egress/data
  access than the model being reviewed — same egress-policy boundary, applied symmetrically.
- **Acceptance criteria:** A change proposed by model family A is reviewed by a genuinely
  different model family B before merge, with B's review recorded as evidence (matching this
  session's own exact-SHA evidence discipline).
- **Activation prerequisites:** FV-WRK-001; FV-WRK-002's reuse constraint applied to however
  review results are recorded (reuse `MainAITaskEvent`-style evidence, not a new table).
- **Phase:** Future phase.

### FV-WRK-006 — Controlled benchmarking (verified output, security, tokens, latency, cost)
- **Domain:** Measurement / cost governance.
- **Source/provenance:** Founder-provided (2026-11-16), requirement #5. Target figure stated
  explicitly by the founder: current AI subscription cost is approximately **SEK 850/month**;
  the goal is to investigate reducing this by roughly two-thirds while maintaining or
  improving verified development output — stated as an investigation target, not a committed
  outcome.
- **Requirement:** Implement controlled benchmarking covering: successful verified changes,
  security quality, token consumption, latency, and cost per accepted result.
- **Dependencies:** FV-WRK-001, FV-WRK-005 (a "verified change" needs the independent-review +
  CI pipeline to exist first).
- **Status:** No source. `backend/app/providers/pricing.py` already tracks token-cost data per
  call (per `docs/SECURITY_BLOCKERS.md` item 4 — noted there as "estimated, not invoice-grade,"
  an operational-drift risk, not a security defect) — a partial foundation for the
  cost-per-call half, not the full benchmarking harness this item asks for.
- **Privacy/security implications:** Benchmarking data about which model produced which
  change must retain the same provenance discipline as everything else in this codebase — no
  anonymized/unattributed "it was AI" result.
- **Acceptance criteria:** A benchmarking report can answer, for a given time window: accepted
  changes per model family, security findings per model family, tokens/latency/cost per
  accepted result — comparable across families on the same basis.
- **Activation prerequisites:** FV-WRK-001, FV-WRK-005.
- **Phase:** Future phase.

### FV-WRK-007 — Prefer free/low-cost models; no unrestricted API spending
- **Domain:** Cost governance / security.
- **Source/provenance:** Founder-provided (2026-11-16), requirement #6.
- **Requirement:** Prefer free and low-cost models when suitable. No unrestricted API
  spending.
- **Dependencies:** None beyond what already exists.
- **Status:** **Already substantially built and enforceable today**, not a future gap:
  `ProviderSpendAuthorization`/`authorize_provider_spend()`/`revoke_provider_spend()`
  (`backend/app/provider_spend/service.py`) already provide founder-controlled,
  revocable spend authorization per provider. Applying that existing control to each new
  evaluation candidate as it's wired up is the actual remaining work, not inventing a new
  spend-control mechanism.
- **Privacy/security implications:** None beyond the existing mechanism's own guarantees.
- **Acceptance criteria:** Every evaluation candidate's real API calls (once any are ever
  made) are gated by an explicit `ProviderSpendAuthorization` row, never an unbounded default.
- **Activation prerequisites:** None — reuse, don't build.
- **Phase:** Cross-cutting principle, immediately satisfiable with existing code.

### FV-WRK-008 — Maintain the existing Claude/Codex/Copilot workflow until replacements prove equivalent-or-better
- **Domain:** Operational continuity.
- **Source/provenance:** Founder-provided (2026-11-16), requirement #7.
- **Requirement:** Maintain the existing Claude/Codex/Copilot development workflow until any
  candidate replacement demonstrates equivalent or better results.
- **Dependencies:** FV-WRK-006 (a demonstrated result requires the benchmarking harness).
- **Status:** No source for a formal "replacement must prove itself first" gate — but this is
  exactly the same conservative, evidence-before-adoption discipline this entire session's own
  engineering practice already follows for everything else (verify before trusting, exact-SHA
  evidence before claiming resolved). Recorded as a policy, not a technical gap.
- **Privacy/security implications:** None directly.
- **Acceptance criteria:** No candidate model is substituted into the real workflow without a
  recorded FV-WRK-006 benchmark showing it is not worse.
- **Activation prerequisites:** FV-WRK-006.
- **Phase:** Cross-cutting principle.

### FV-WRK-009 — LifeAI eventually self-evaluates and recommends assignments
- **Domain:** Long-horizon autonomy (explicitly gated).
- **Source/provenance:** Founder-provided (2026-11-16), requirement #8: "When LifeAI is ready,
  let her evaluate candidate models and recommend assignments based on measured performance,
  cost and security." Second directive's closing line: "Once safety approval is obtained, the
  objective is for LifeAI to begin evaluating these models herself and gradually build our own
  cost-efficient AI development team."
- **Requirement:** MainAI itself (not a human) eventually performs the evaluation and makes
  assignment recommendations, based on FV-WRK-006's measured data.
- **Dependencies:** FV-WRK-001 through FV-WRK-008 all substantially in place first; independent
  security acceptance (`docs/LIFE_FIRST_CYCLE_READINESS_MATRIX.md` row 11, the one true
  remaining BLOCKER) completed; explicit founder authorization.
- **Status:** No source. This explicitly requires the currently-inert autonomous-dispatch
  systems (`app.agent_coordination`, `app.workforce` — confirmed structurally inert, matrix
  row 1) to be deliberately activated for this one, narrow, measurement-driven purpose — a
  real, future decision, not something this document authorizes.
- **Privacy/security implications:** The largest in this entire section — MainAI
  recommending (not yet executing) model assignments is a lower-risk first step than letting
  it autonomously switch providers; this distinction should be preserved in any future design,
  not collapsed.
- **Acceptance criteria:** Not yet defined — explicitly described by the founder as something
  for "when LifeAI is ready," not now.
- **Activation prerequisites:** Everything in this section; independent security acceptance;
  explicit founder authorization. **This item alone, of everything in this backlog, most
  directly requires the first-cycle gate itself to be passed first** — it is not merely
  future-phase, it is downstream of the gate.
- **Phase:** Future phase, long-horizon, explicitly furthest downstream of all gates in this
  backlog.

## 12. STRATO terminal execution (restricted, audited)

Founder directive ("MULTI-MODEL WORKFORCE + STRATO TERMINAL" / "NEW FOUNDER DIRECTIVE",
2026-11-16). **Checked first:** this repository's VPS deployment tooling (the real "Strato VPS
compose topology" CI job, `docs/VPS_ARCHITECTURE.md`, `docs/VPS_OPERATIONS_RUNBOOK.md`,
`docs/VPS_THREAT_MODEL.md`, `docs/VPS_BACKUP_RESTORE.md`) is about **deploying this application
to STRATO**, not about giving an AI model terminal access to that same infrastructure for
development — a genuinely different concern. A repo-wide search for any existing
restricted-terminal/SSH-execution-for-AI-models mechanism (`paramiko`, `ssh_client`, a
dedicated remote-shell executor) found **zero hits** — this entire capability has no existing
source anywhere in this repository.

### FV-TRM-001 — Restricted execution worker, never unrestricted root/SSH to AI models
- **Domain:** Infrastructure security.
- **Source/provenance:** Founder-provided (2026-11-16); no historical repository source.
- **Requirement:** "Never provide unrestricted root access or expose SSH credentials directly
  to AI models. Use a dedicated restricted execution worker and isolated environments."
- **Dependencies:** None technical yet — this is the foundational constraint everything else
  in this section depends on.
- **Status:** No source. Zero existing mechanism of any kind.
- **Privacy/security implications:** The single most security-sensitive item in this entire
  backlog — a credential-handling design error here (e.g. a worker that CAN be made to reveal
  its own SSH key through a crafted command) would be a severe, not merely conditional, risk.
  Should reuse this repo's own existing credential-isolation discipline (e.g. `request_credential`-
  style secret injection that the calling code never sees directly) as a design reference, not
  invent a new pattern.
- **Acceptance criteria:** A model-facing execution request can be proven, by design (not by
  policy alone), unable to read or exfiltrate the worker's own SSH/root credentials.
- **Activation prerequisites:** A full, separately-reviewed security design — not a
  "smallest safe correction," the same judgment this session already applied to
  `docs/LIFE_FIRST_CYCLE_READINESS_MATRIX.md` row 5's subprocess-environment finding.
- **Phase:** Future phase — explicitly not activated by this document.

### FV-TRM-002 — Scoped commands, paths, permissions, network, timeouts, resource limits
- **Domain:** Infrastructure security.
- **Source/provenance:** Founder-provided (2026-11-16).
- **Requirement:** Enforce scoped commands, paths, permissions, network access, timeouts, and
  resource limits on any terminal-based operation.
- **Dependencies:** FV-TRM-001.
- **Status:** No source for a STRATO-facing version of this. The closest EXISTING precedent in
  this repo is `execution_job.py`'s own `subprocess.run(..., timeout=timeout_seconds)` for
  `run_tests` — already scoped by command (fixed argv) and by timeout, but (per
  `docs/LIFE_FIRST_CYCLE_READINESS_MATRIX.md` row 5) NOT yet scoped by environment/network —
  directly relevant precedent for what a future STRATO worker must do better, not duplicate.
- **Privacy/security implications:** Same class as FV-TRM-001.
- **Acceptance criteria:** Every dimension named (commands/paths/permissions/network/timeouts/
  resources) has an enforced, testable limit, not merely a documented intention.
- **Activation prerequisites:** FV-TRM-001.
- **Phase:** Future phase.

### FV-TRM-003 — Audit trails, human approval gates, kill-switch
- **Domain:** Infrastructure security / governance.
- **Source/provenance:** Founder-provided (2026-11-16).
- **Requirement:** Preserve audit trails, human approval gates, and kill-switch enforcement
  for any terminal-based operation.
- **Dependencies:** FV-TRM-001.
- **Status:** **Strong existing foundation to reuse, not duplicate**: `MainAITaskEvent`'s
  append-only audit trail, `require_task_approval()`/`grant_task_approval()`'s approval gate,
  and `cancel_task()`'s audited cancel path (all verified directly this session,
  `docs/LIFE_FIRST_CYCLE_READINESS_MATRIX.md` rows 3/7) are the exact mechanisms a STRATO
  terminal worker should extend, never reinvent. What's missing is only the STRATO-specific
  execution surface itself, not the governance pattern around it.
- **Privacy/security implications:** None beyond FV-TRM-001 given the reuse above.
- **Acceptance criteria:** Every STRATO terminal action produces a `MainAITaskEvent`-style
  durable record and passes through the existing approval gate before executing, with a
  tested kill-switch that halts an in-progress operation.
- **Activation prerequisites:** FV-TRM-001, FV-TRM-002.
- **Phase:** Future phase.

### FV-TRM-004 — Fix the run_tests subprocess-environment gap before any untrusted execution reaches the runtime
- **Domain:** Infrastructure security (explicit activation prerequisite, not a standalone
  feature).
- **Source/provenance:** Founder-provided (2026-11-16), directly naming
  `docs/LIFE_FIRST_CYCLE_READINESS_MATRIX.md` (PR #446) row 5's own finding.
- **Requirement:** "Address the `run_tests` subprocess environment inheritance identified in
  #446 before any untrusted code execution is permitted to access the runtime environment."
- **Dependencies:** This item does not introduce new work — it PROMOTES an existing,
  already-recorded CONDITIONAL finding into an explicit precondition for this specific new
  capability (STRATO terminal execution), exactly matching that finding's own stated trigger
  ("becomes a BLOCKER the moment any unsupervised/non-human-reviewed code path... is allowed
  to reach `run_tests` without a human reviewing the diff first").
- **Status:** **Open, unfixed, exactly as #446 already recorded** — `execution_job.py:369`'s
  `subprocess.run()` still passes no `env=`, still inherits the worker's full secrets. Not
  fixed in this pass either: the real remediation (an environment allowlist) remains design
  work, and #446 is correctly left as CONDITIONAL since STRATO terminal execution is not yet
  authorized or built.
- **Privacy/security implications:** Direct secret-exfiltration risk if ever combined with
  untrusted/unsupervised code execution — the founder's own instruction correctly identifies
  this as a precondition, not a parallel task.
- **Acceptance criteria:** Reuses #446 row 5's own acceptance framing: an environment
  allowlist design that preserves legitimate DB-backed test execution while excluding
  provider API keys and any STRATO/SSH credentials from the test subprocess's inherited
  environment.
- **Activation prerequisites:** This IS the activation prerequisite for FV-TRM-001 through
  FV-TRM-003 collectively — STRATO terminal execution must not proceed until this is closed.
- **Phase:** Future phase, but sequenced FIRST within this section, ahead of every other
  FV-TRM item.

### FV-TRM-005 — Separate production infrastructure from model-controlled development environments
- **Domain:** Infrastructure architecture.
- **Source/provenance:** Founder-provided (2026-11-16).
- **Requirement:** Keep production infrastructure separate from any environment a model
  controls for development.
- **Dependencies:** FV-TRM-001.
- **Status:** No source for a STRATO-specific version. The existing VPS topology
  (`docs/VPS_ARCHITECTURE.md`) already separates production services by Docker network
  boundary (e.g. Redis/Valkey isolated with zero route from frontend/Caddy, per
  `docs/LIFE_FIRST_CYCLE_READINESS_MATRIX.md` row 6's egress findings) — the same network-
  isolation discipline should extend to any future model-controlled dev environment, not
  invent a separate isolation model.
- **Privacy/security implications:** A compromised or misbehaving model-controlled dev
  environment must have no network path to production services or credentials.
- **Acceptance criteria:** A model-controlled dev environment is provably unable to reach
  production database credentials, the production Caddy/TLS material, or any production-only
  network segment.
- **Activation prerequisites:** FV-TRM-001.
- **Phase:** Future phase.

### FV-TRM-006 — Evaluate STRATO's actual hardware before choosing local inference
- **Domain:** Infrastructure / cost strategy.
- **Source/provenance:** Founder-provided (2026-11-16): "Evaluate STRATO's actual hardware
  capabilities before choosing local inference. Hosted inference may initially be more
  economical."
- **Requirement:** Base any local-vs-hosted inference decision on STRATO's REAL, measured
  hardware capability, not an assumption either way.
- **Dependencies:** None technical — this is a research/measurement task.
- **Status:** No source. Directly relevant existing context: `docs/MAINAI_ARCHITECTURE.md`'s
  "Local MainAI Capability Layer" target and `docs/ROADMAP.md`'s Fas 4 (local model/GPU
  capacity) already name local inference as a future direction in general — this item adds
  the specific, STRATO-hardware-grounded evaluation step that must happen before committing to
  it, consistent with this repo's own "study before building" discipline (PR #424 §1).
- **Privacy/security implications:** None directly.
- **Acceptance criteria:** A documented measurement of STRATO's actual CPU/GPU/memory
  capability against the resource needs of any candidate local model, before a local-inference
  decision is made.
- **Activation prerequisites:** None — this can be investigated independently of everything
  else in this section, and should happen early since it is comparatively low-risk.
- **Phase:** Future phase, but one of the lowest-risk, earliest-doable items in this entire
  backlog (it is pure measurement, not a security-sensitive capability).

## 12a. Adaptive multi-step reasoning and verification

Founder directive ("MULTI-STEP REASONING & VERIFICATION", refined by "ADAPTIVE INTELLIGENCE &
VERIFICATION", 2026-11-16). The second message explicitly refines the first into an
evidence-driven, effort-proportional capability — this section records the refined version;
the original 8-point list is superseded, not duplicated. Per both directives' own instruction
("first determine what is already implemented... preserve only genuinely missing
requirements"), checked against `app.mainai_execution` directly before writing anything.

**Already substantially implemented — not re-recorded as gaps:**
- *Incremental execution with checkpoints* (req: "execute changes incrementally with
  checkpoints"): `MainAICheckpoint`, job-scoped, append-only, with the full recovery pipeline
  already exercised this session.
- *Auditable decision/evidence trail, never private model reasoning* (req: "record decisions
  and evidence, not private model reasoning"): `MainAITaskEvent.detail` is structured JSON
  evidence only; `app/mainai_execution/lesson_from_verification.py`'s own docstring states the
  exact discipline the founder is asking for, already enforced: *"never `str(exc)`, provider
  prose, or free-form logs."*
- *Learning from verified outcomes to improve future strategy* (req: "measure results and
  improve future task strategies from verified outcomes"): `EngineeringLesson`
  (`app/models/mainai_execution.py:302`) + `apply_lessons_to_verification_plan()`
  (`app/mainai_execution/lessons.py`), already wired into `planner.py`'s `create_plan()` — a
  past exhausted-verification failure can add a regression test to a FUTURE task's
  verification plan automatically, with mandatory provenance and durable event-recorded
  effect. This is real, live, already-built machinery for exactly this requirement, not a
  future one.
- *Bounded retries, no infinite loops* (req: "prevent infinite retry loops"):
  `ck_mainai_tasks_attempts_within_budget` + `max_attempts`, enforced at the database level.
- *No unsafe self-approval* (req: "never allow self-review... to bypass authorization"):
  `require_task_approval()`'s design already requires a distinct, separately-recorded
  `approved_by` event — no code path lets the same call that requests approval also grant it.
- *Existing approval gates hold regardless* (req: same): confirmed structurally, not just by
  convention — see `docs/LIFE_FIRST_CYCLE_READINESS_MATRIX.md` row 3.

**Genuine gaps** (the actual content to preserve):

### FV-RSN-001 — Candidate-approach comparison before plan selection
- **Domain:** Planning.
- **Source/provenance:** Founder-provided (2026-11-16); no historical repository source.
- **Requirement:** Develop and compare candidate approaches before selecting an execution
  plan, rather than committing to the first plan produced.
- **Dependencies:** None technical — this would extend `planner.py`'s `create_plan()` input
  shape, not replace it.
- **Status:** No source. `create_plan()` today takes exactly one already-decided list of
  `PlannedTaskSpec`s; there is no concept of multiple candidate plans or a comparison step
  anywhere in `app.mainai_execution`.
- **Privacy/security implications:** None beyond existing planning-authority boundaries.
- **Acceptance criteria:** Not yet defined.
- **Activation prerequisites:** A concrete consuming scenario that needs it — none exists yet.
- **Phase:** Future phase.

### FV-RSN-002 — Pre-mortem risk analysis proportional to consequence
- **Domain:** Risk assessment.
- **Source/provenance:** Founder-provided (2026-11-16); no historical repository source.
- **Requirement:** Perform pre-mortem risk analysis "when consequences justify it" — not for
  every task uniformly.
- **Dependencies:** `MainAIGoalRiskLevel` (`low`/`medium`/`high`, already a real field on
  goals/tasks) is the natural signal to key "justify it" against — reuse, don't duplicate.
- **Status:** No source. The risk-level FIELD already exists; nothing currently reads it to
  trigger a deeper analysis step.
- **Privacy/security implications:** None directly.
- **Acceptance criteria:** Not yet defined.
- **Activation prerequisites:** None blocking; could in principle reuse the existing
  `risk_level` field without new schema.
- **Phase:** Future phase.

### FV-RSN-003 — Effort-proportional investigation and execution depth
- **Domain:** Planning / execution strategy.
- **Source/provenance:** Founder-provided (2026-11-16): "dynamically allocate investigation,
  planning, execution and verification effort according to task complexity, uncertainty,
  risk, reversibility and potential impact... the right amount of thinking, not maximum
  thinking."
- **Requirement:** Lightweight execution for simple tasks; deeper analysis for complex or
  security-critical ones; identify the most valuable missing evidence before inspecting more
  files or acting.
- **Dependencies:** FV-RSN-002 (shares the same risk-signal reuse principle).
- **Status:** No source. This is a reasoning-strategy concept, not missing infrastructure —
  the signals to key off (`risk_level`, task_type, `verification_plan` depth) already exist;
  nothing currently varies effort based on them.
- **Privacy/security implications:** None directly.
- **Acceptance criteria:** Not yet defined.
- **Activation prerequisites:** None blocking.
- **Phase:** Future phase.

### FV-RSN-004 — Independent model-family and adversarial review before acceptance
- **Domain:** Quality / security review.
- **Source/provenance:** Founder-provided (2026-11-16): "support independent model-family
  review," "use independent adversarial review where warranted."
- **Requirement:** A change is reviewed by a genuinely different model family (not the one
  that produced it) and, where warranted, an adversarial reviewer, before proposing
  acceptance.
- **Dependencies:** **This is the same requirement as `FV-WRK-005`**, recorded in §11 above —
  not duplicated here, only cross-referenced, since this directive restates it from the
  reasoning-architecture angle rather than the multi-model-workforce angle.
- **Status:** No source — see FV-WRK-005 for the full status (deterministic CI already exists;
  cross-model-family review does not).
- **Privacy/security implications:** See FV-WRK-005.
- **Acceptance criteria:** See FV-WRK-005.
- **Activation prerequisites:** See FV-WRK-005.
- **Phase:** Future phase.

### FV-RSN-005 — Explicit reasoning, tool-use, and cost budgets
- **Domain:** Resource governance.
- **Source/provenance:** Founder-provided (2026-11-16): "apply explicit reasoning, tool-use,
  cost and retry budgets" / "enforce bounded reasoning/execution iterations, token budgets."
- **Requirement:** Hard-enforced limits on reasoning iterations, tool calls, and cost per
  task — not just retry count.
- **Dependencies:** `backend/app/providers/pricing.py` already tracks token cost per call
  (noted in `docs/SECURITY_BLOCKERS.md` item 4 as "estimated, not invoice-grade") — a partial
  foundation for cost VISIBILITY, not an enforced BUDGET/limit.
- **Status:** Partial. Retry budget exists and is enforced (`max_attempts`). Reasoning-
  iteration and tool-use budgets, and a hard (not just visible) cost ceiling, do not exist.
- **Privacy/security implications:** An unenforced cost signal is itself a minor spend-control
  gap, related to but distinct from `FV-WRK-007`'s spend-authorization reuse.
- **Acceptance criteria:** Not yet defined.
- **Activation prerequisites:** None blocking; a natural extension of existing
  `ProviderSpendAuthorization` machinery rather than a new mechanism.
- **Phase:** Future phase.

### FV-RSN-006 — Mid-execution plan revision when evidence contradicts assumptions
- **Domain:** Planning / execution.
- **Source/provenance:** Founder-provided (2026-11-16): "validate intermediate results and
  revise the plan when evidence contradicts assumptions."
- **Requirement:** A plan can be revised DURING a task's own execution attempt when new
  evidence contradicts what the plan assumed — not only between separate goal-level
  replanning passes.
- **Dependencies:** The lessons system (see the "already implemented" list above) already
  revises FUTURE tasks' verification plans from PAST failures; `create_plan()` already
  supersedes a goal's previous plan wholesale. Neither is mid-attempt, intra-task revision.
- **Status:** Partial — the two adjacent, already-built mechanisms above are not quite this
  requirement; genuinely missing is revision WITHIN one execution attempt, not just between
  attempts or between goal-level plan versions.
- **Privacy/security implications:** A plan revision mid-execution must not silently widen
  approval scope — any new work implied by a revision should require the same approval gate
  fresh work already does, never inherit the original plan's approval by default.
- **Acceptance criteria:** Not yet defined.
- **Activation prerequisites:** FV-RSN-001 (candidate comparison) likely informs the design.
- **Phase:** Future phase.

No new orchestration authority, planning engine, or execution system is introduced by this
section. `first_cycle_go=false`; documentation only, as directed.

## 13. Making this backlog recoverable — ingestion wiring is a separate dependency

**Documentation existing in GitHub does not by itself prove MainAI can discover or retrieve
it.** This repository already has a real, working knowledge chain for founder-provided
material — `MemorySourceUnit` → `KnowledgeClaim` → `InterpretationProposal` →
`ProjectEntity` (the S1A/S1B/S1C source-identity and extraction architecture, verified
directly and repeatedly this session) — but this backlog's own content has not been run
through that pipeline, and doing so is explicitly **not authorized by this document**.

**What would actually be needed, named as a dependency, not built here:**
1. A `repository_file` (or equivalent) `MemorySourceUnit` subtype covering `docs/*.md` files
   specifically — PR #437 already built the general repository-file source-identity arm
   (S1D, `commit_sha`-pinned); whether this backlog's own files are in that extraction's
   actual scope, or need their own pass, is unresolved and not decided here.
2. An extraction/classification pass (Pass 1-3's own established pattern, per
   `docs/LIFE_PROJECT_KNOWLEDGE_INVENTORY_MODEL.md` and its Pass 1/2/3 documents) that
   specifically recognizes this backlog's fixed per-item schema (Stable ID, domain, status,
   phase) as structured data worth extracting as individual `KnowledgeClaim`s — one claim per
   `FV-*` ID — rather than treating the whole file as one undifferentiated document chunk.
3. A founder decision on whether backlog items should become `KnowledgeClaim`s at all, or
   whether a future, separately-authorized "deferred product backlog" source type is more
   honest (a backlog item is a founder INTENTION/requirement, not a founder KNOWLEDGE CLAIM
   about something that already exists — the same SIGNAL-PRODUCER != TRUTH-WRITER distinction
   `app/github_evidence` already draws for live GitHub state vs. founder knowledge, per this
   session's own prior work, likely applies here too and is worth deciding explicitly rather
   than defaulting either way).

None of this is built, authorized, or scheduled by this document. It is named here precisely
so that "this is now in GitHub" is never mistaken for "MainAI can now retrieve this."

## 14. Concise roadmap

**First-cycle prerequisites** (from `docs/LIFE_FIRST_CYCLE_READINESS_MATRIX.md`, PR #446 —
not restated in full here):
- Independent security acceptance actually occurring (the one true remaining BLOCKER; owned
  by the founder/external reviewer, not engineering).
- Everything else engineering-side is already resolved or correctly scoped as conditional on
  capabilities not in the first cycle's actual scope.

**Next development phases** (ordered, lowest-effort/most-reusable first):
- FV-TRM-006 (measure STRATO's actual hardware) — pure measurement, no security exposure,
  doable immediately and independent of everything else in this document.
- FV-WRK-007 (spend-control reuse for evaluation candidates) — already satisfiable with
  existing `ProviderSpendAuthorization` code; only needs applying, not building.
- FV-WSP-001's two remaining Life Workspaces gaps, if a concrete consuming feature ever
  justifies them (currently correctly deferred).
- FV-INT-002's Life Capability Interface design, reusing the existing AI-provider
  capability-interface pattern as a template.
- FV-PRV-001's local-chat-fallback gap, extending RAG's already-local precedent.
- **FV-TRM-004 (the run_tests subprocess-environment fix) sequenced BEFORE any FV-TRM-001/002/003
  work** — it is this entire STRATO section's own stated activation prerequisite, not an
  independent phase.

**Deferred consumer products** (no first-cycle dependency, "FUTURE PRODUCT" in the matrix's
own classification language): LifeOS/Life Phone/Life App/Store (§1), Life Dating (§3), Life
Social/Life Causes (§4), Life Word/Sheets/Teams (§6), Life Engineering/Security Lab (§7, except
FV-DEV-001 which already has its own PR), portable installation (§8), multi-tenant business AI
(§8), the multi-model AI workforce (§11, except FV-WRK-007/008/002/003 which are reusable
principles satisfiable now) and STRATO terminal execution (§12) in their entirety as
*capabilities*, though §12's measurement item (FV-TRM-006) and §11's spend-reuse item
(FV-WRK-007) are exceptions already noted above.

**Long-horizon platform research** (explicitly research questions, not assumed-missing
implementations): AOSP viability (FV-OS-001); dating-matching scientific evidence base
(FV-DAT-003); external-developer licensing model (FV-INT-001); application security
certification strategy (per `docs/LIFE_ECOSYSTEM_VISION_RECONCILIATION.md` §4); cross-device
synchronization architecture (same source); FV-WRK-009 (LifeAI self-evaluating models), the
single most downstream item in this entire backlog, explicitly gated behind independent
security acceptance and founder authorization.

## See also

- `docs/LIFE_ECOSYSTEM_VISION_RECONCILIATION.md` (this PR) — the narrative reconciliation this
  backlog extends.
- `docs/LIFE_FIRST_CYCLE_READINESS_MATRIX.md` (PR #446) — the separate, engineering-focused
  readiness classification this backlog does not duplicate.
- `docs/LIFE_DURABLE_WORKSPACES_GAP_ANALYSIS.md` (PR #444), `docs/LIFE_DEFERRED_PRODUCT_VISION_AND_VENDOR_PHILOSOPHY.md`
  (PR #424), `docs/LIFE_DEVELOPMENT_MONITOR.md` (PR #426) — cross-referenced throughout, never
  duplicated.

No merge, deployment, or activation. `first_cycle_go=false`. No item above authorizes building
anything.

---
_Generated by [Claude Code](https://claude.ai/code/session_016d4iixQnxxqNhDrxHHxV4Q)_
