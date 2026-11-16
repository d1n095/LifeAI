# MainAI V2 — Sovereign Local Intelligence: Architecture Map (Stage V2-A)

**Status:** design-only, isolated lane. Does not modify, rebase, or depend on the pending
safe-internal certification candidate (PR #245, exact SHA `818dfb732da47901eb5ae06ffdd9c829fe00c4c5`
on `claude/final-blocker-closeout`). Branched from `claude/det-kommer-mer-879lcm` (the shared
integration tip at the time this lane opened, post-#236), not from #244's or #245's branch —
deliberately, so this program can proceed without any risk of contaminating the frozen
certification candidate.

**Program name:** MainAI V2 — Sovereign Local Intelligence.
**Scope of this document:** Stage V2-A only — architecture map, contracts, threat model
skeleton. Stages V2-B through V2-J are separate documents in this same directory.

---

## Canonical product narrative (founder's own words, 2026-09-03 — quoted, not paraphrased)

This is the reference framing for every V2 stage document. Where a design decision is
ambiguous, resolve it toward this narrative rather than toward generic "AI assistant"
conventions:

> LifeAI ska i slutändan nästan inte kännas som ett operativsystem. Du sätter dig vid
> enheten och pratar med henne. Hon har datorn som kropp, Sentinel som immunförsvar,
> Guardian som reflexer, agenterna som specialistteam, minnet som kontinuitet, Vault som
> hennes låsta kassaskåp, och orben som den enda person du egentligen behöver förhålla dig
> till.

Mapped onto this document's own vocabulary (§1–§2), so the metaphor and the architecture
stay the same thing, not two parallel descriptions of it:

| Founder's metaphor | This document's primitive |
|---|---|
| datorn som kropp (the computer as body) | the device/runtime MainAI operates through — everything below "MainAI" in §2's trust chain |
| Sentinel som immunförsvar (immune system) | V2-D — detects and responds to threats, does not itself hold root authority |
| Guardian som reflexer (reflexes) | V2-B — fast, mechanical, pre-authorized-scope-only; a reflex acts before "thinking," which is exactly why Guardian must stay small and judgment-free |
| agenterna som specialistteam (specialist team) | V2-E — invisible unless asked, bounded, never inheriting broader access from their specialization |
| minnet som kontinuitet (memory as continuity) | Intent Objects (§1) + the proven same-device restart durability this session built — continuity is a *tested property*, not an aspiration |
| Vault som hennes låsta kassaskåp (the locked safe) | the existing Vault/egress boundary this project already has, which V2-C's Privacy Boundary Engine extends, not replaces |
| orben som den enda person (the orb as the one person) | `MAINAI_ORB` (§1) — V2-I's entire premise: `MAINAI IS THE PRIMARY UI. SCREENS ARE TOOLS, NOT THE PRODUCT.` |

The point of this table: a reflex that requires deliberation is not a reflex (Guardian must
stay small); an immune system that needs to see your diary to fight an infection is a
surveillance system, not an immune system (`SPECIALIZATION != SURVEILLANCE`, V2-D); a safe
whose combination the landlord also holds is not a safe (V2-C/V2-G's whole point). The
metaphor is not decoration — it is a genuine constraint-generating device for every V2 stage.

---

## 0. What already exists — do not rebuild this (checked before writing anything)

This session spent one entire night building and red-teaming a real safety-invariant
foundation for MainAI's *first* internal execution surface (the Workforce/Memory Frontier
stack, #209–#245). V2 does not start from zero — several of Part T's required invariants in
the founder's own brief are **already implemented, tested, and (pending #245's independent
certification) load-bearing today**:

| V2 constitution line (founder's own list) | Existing V1 primitive | Status |
|---|---|---|
| `MODEL OUTPUT != AUTHORITY` | `app.execution_envelopes` — `propose_execution_scope()` never writes to `execution_authorization_envelopes`; only `authorize_execution_scope()` does, always the caller's own explicit assertion | Implemented, tested (real TOCTOU race found+fixed this session) |
| `SECURITY FAILURE -> REDUCE AUTHORITY` | `app.workforce.kill_switch` — DB-backed `workforce_authority_epoch`, `SELECT .. FOR SHARE`/`FOR UPDATE` serializing grant vs. stop, per-owner + global scopes | Implemented, tested (the single most severe fix of tonight's campaign, PR #243) |
| `EVIDENCE EXISTS != EVIDENCE SUPPORTS CLAIM` | `app.evidence_claim.evidence_supports_claim()` — shared gate, now requires exact subject match + real outcome, not caller-supplied truthiness | Implemented, tested, still being independently re-verified (PR #245) |
| `SPECIALIZATION != BROADER ACCESS` | `app.workforce` — `WorkforceAssignment`'s explicit, never-inherited `allowed_read_paths`/`allowed_write_paths`/`allowed_tool_classes`/`allowed_network_destinations`/`spend_ceiling_usd` | Implemented (Stage T, #230–232) |
| `RECOVERY != BACKDOOR` (partial) | `clear_kill_switch_for_recovery()` requires a founder-ack token | Implemented but **weak** — ack validation is only a denylist+regex, no real cryptographic verification (found, not yet fixed, tonight) — V2's Sovereign Identity work (V2-G) should close this properly, not patch it locally |
| `KNOWLEDGE INHERITANCE != AUTHORITY INHERITANCE` | `app.capability_reality` + `app.mainai_school` — a proven local competence never itself grants execution authority; a separate explicit grant path does | Implemented (Stage T + Local Intelligence School, #236) |
| Readiness must fail closed on UNKNOWN | `app.mainai_startup_readiness.evaluate_startup_readiness()` | Implemented, tested tonight (#245) |

**Practical implication for V2:** the Guardian/Trust Kernel (V2-B) and Sentinel (V2-D) do not
need to invent authority-boundary or evidence-truth primitives from scratch. They should
**reuse `execution_envelopes`, `evidence_claim`, and `kill_switch`'s actual mechanisms**,
extended for new domains (device/network/file events, not just workforce assignments) rather
than re-implemented in parallel. Re-implementing these has already produced real bugs at
least six independent times this session (see `docs/BRANCH_REGISTRY.md`'s "evidence-semantics"
sections) — every occurrence was a *different* module reinventing the same check slightly
wrong. V2-B/V2-D must not become a seventh occurrence.

What does **not** exist yet and is genuinely new to V2: the Orb/workspace UI shell (V2-I), the
Privacy Boundary Engine (V2-C), Sentinel's actual detection engines (V2-D content beyond the
authority-reuse above), the Local Specialist Workforce's *domain* content (V2-E — the Stage T
workforce *mechanism* exists, the finance/legal/security *specialist knowledge* does not),
Offline Knowledge Packs (V2-F), Sovereign Identity (V2-G) beyond today's session-cookie auth,
Sovereign Recovery / Encrypted Life Image (V2-H).

---

## 1. Core module vocabulary

These are the primitives every other V2 stage document refers to by name. Defined once here
so B–I don't each invent slightly different terms for the same thing.

- **`MAINAI_ORB`** — the persistent, always-present conversational surface. Not a window; a
  system-level presence. Exactly one per device session, one logical identity per owner
  across devices (see V2-G).
- **`WORKSPACE`** — the owner's current set of open applications, windows, documents, and
  their arrangement. MainAI can read and manipulate it; the workspace itself has no
  authority of its own (`WORKSPACE MEMORY != EXECUTION AUTHORITY`, restated in §5).
- **`INTENT_OBJECT`** — a durable record of something the owner is trying to accomplish.
  Survives restart (this session's own multi-restart continuity work, applied to a new
  domain). Fields: `goal`, `state`, `priority`, `context`, `linked_files`, `linked_memories`,
  `agents`, `blockers`, `next_actions`, `risk`, `authority` (always a *reference* to a real,
  separately-granted authorization — never a field that itself grants anything, matching the
  `execution_envelopes` doctrine), `completion_definition`, `history`.
- **`TAKEOVER_STATE`** — whether the owner has manual control at this instant. Binary,
  authoritative, and **instantaneous**: any raw keyboard/mouse/voice input from the owner
  sets it immediately, without waiting for MainAI's own turn-taking logic. `USER INPUT >
  AGENT CONTROL` (§5) is enforced at the input-routing layer, not by MainAI choosing to yield.
- **`CONTEXT_STATE`** — what the owner is currently looking at / referring to with pronouns
  ("this", "that", "it") — reuses the personal-language resolution work from
  `docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md` (already-built entity resolution),
  extended to point at workspace objects, not just conversation entities.
- **`VISIBLE_SURFACE`** — any UI panel MainAI has opened on request ("show security", "show
  what you remember"). Surfaces are *revealed*, not navigated to — there is no persistent
  menu/dashboard the owner must learn (§P/V2-I).
- **`BACKGROUND_AGENT_TASK`** — a `WorkforceAssignment` (existing Stage T primitive) that the
  owner is not currently watching. Same authority rules as any other assignment — being
  "background" is a UI fact, never an authority fact.

## 2. Trust boundary map

```
OWNER (sovereign identity, V2-G)
  │
  ▼
GUARDIAN / TRUST KERNEL (V2-B) ── minimal, small, root authority
  │  - Vault boundary
  │  - network authority
  │  - agent authority ceiling
  │  - emergency containment
  │  - recovery entry
  │  - owner identity verification
  ▼
MAINAI (orb, executive reasoning, workspace control)
  │  - reuses execution_envelopes for every authority grant
  │  - reuses evidence_claim for every "proven"/"verified" claim
  │  - reuses kill_switch for defensive containment
  ▼
DEPARTMENTS (finance, legal, security/Sentinel, home, health, business, documents, ...)
  │  - existing Stage T "department" concept (app.workforce.department_evidence)
  ▼
AGENTS / SPECIALISTS / TOOLS / NETWORK
  - bounded per-assignment authority envelope (existing WorkforceAssignment fields)
  - never inherits department- or MainAI-level authority merely by association
```

**Non-negotiable direction of authority:** it only ever narrows going down this chain, never
widens. Nothing below Guardian can grant itself something Guardian didn't already delegate.
Nothing below MainAI can grant itself something MainAI didn't already delegate via a real
`execution_envelopes`-style explicit act. This is the same shape as the existing
`app.execution_envelopes` doctrine, just drawn as an org chart instead of a two-function API.

## 3. Threat model skeleton (expanded per-domain in V2-D/V2-I/V2-J)

**Assets to protect (ranked):**
1. Owner's physical safety / real-world consequences (financial, legal, medical decisions
   MainAI might influence).
2. Owner's private data at rest (Vault, local documents, conversation history).
3. Owner's private data in motion (network egress, any provider call).
4. Device/runtime integrity (MainAI's own execution environment).
5. Availability (owner must always be able to regain manual control and reach Guardian).

**Threat actors:**
- Malicious/compromised third-party content the owner opens (files, links, QR codes, email —
  V2-I's input-security pipeline).
- Network-based attackers (malware, exploits, ransomware — Sentinel's domain, V2-D).
- A compromised or misbehaving MainAI runtime itself (prompt injection, a poisoned local
  model, a supply-chain-compromised plugin) — this is the threat Guardian exists for; MainAI
  is explicitly **not** trusted as her own sole security authority (§V2-B).
- LifeAI (the central company) overreaching into private local data — this is what the
  Privacy Boundary Engine (V2-C) exists to make structurally, not just contractually,
  difficult.
- A malicious or coerced owner action (e.g. someone else physically forcing device unlock) —
  partially addressed by Sovereign Identity (V2-G) and device trust (V2-O), not fully
  solvable by software alone; V2-J's threat model should say so explicitly rather than
  overclaim.

**Explicitly out of scope for MainAI's own authority (restated from the founder's brief):**
no hack-back / offensive retaliation (§G). Any V2-D "defensive autonomy" design must draw this
line as a hard architectural wall, not a policy note that could be argued around later.

## 4. Security constitution (verbatim from the founder's brief, canonical copy)

This is the single canonical list. Every other V2 document references it by name rather than
re-copying it, so it never drifts into N slightly-different versions.

```
OWNER != SESSION
SESSION ACCESS != ROOT AUTHORITY
MODEL OUTPUT != AUTHORITY
NETWORK INPUT != TRUSTED INPUT
LINK OPENED != LOCAL ACCESS GRANTED
FILE CONTENT != INSTRUCTION AUTHORITY
LOCAL DATA != PROVIDER CONTEXT
VAULT READ != VAULT DISCLOSURE
DATA ACCESS != DISCLOSURE AUTHORITY
SPECIALIZATION != BROADER ACCESS
KNOWLEDGE INHERITANCE != AUTHORITY INHERITANCE
LOCAL MODEL != TRUSTED MODEL
PLUGIN INSTALLED != PLUGIN TRUSTED
DEVICE TRUSTED != DEVICE UNRESTRICTED
BACKUP EXISTS != BACKUP RESTORES
CLOUD STORAGE != CLOUD TRUST
RECOVERY != BACKDOOR
SECURITY FAILURE -> REDUCE AUTHORITY
MAINAI MAY ISOLATE HERSELF TO PROTECT OWNER
```

Plus the ones already proven as real, tested code this session (added here because they are
just as load-bearing and should not be lost when V2 formalizes the rest):

```
EVIDENCE EXISTS != EVIDENCE SUPPORTS CLAIM      (app.evidence_claim)
DETECTED BLOCKER == ENFORCED BLOCKER            (app.mainai_startup_readiness)
UNKNOWN SAFETY STATE != SAFE                    (app.mainai_startup_readiness)
```

## 5. Cross-cutting invariants specific to the Orb/workspace model

```
WORKSPACE MEMORY != EXECUTION AUTHORITY
USER INPUT > AGENT CONTROL                      (instantaneous, not negotiated)
AGENT SPECIALIZATION != BROADER ACCESS
PAST COMPETENCE != CURRENT COMPETENCE
OFFLINE != BLIND CONFIDENCE
NEW DETECTION RULE != TRUSTED RULE
```

## 6. Document map (this program)

| Stage | Document | Covers |
|---|---|---|
| V2-A | `MAINAI_V2_ARCHITECTURE_MAP.md` (this file) | Vocabulary, trust boundaries, threat model skeleton, constitution |
| V2-B | `MAINAI_V2_GUARDIAN_TRUST_KERNEL.md` | Guardian design, authority boundaries |
| V2-C | `MAINAI_V2_PRIVACY_BOUNDARY_ENGINE.md` | Privacy pipeline, schemas, telemetry modes |
| V2-D | `MAINAI_V2_SENTINEL_SECURITY.md` | Sentinel domains, security event mesh, defensive autonomy |
| V2-E | `MAINAI_V2_LOCAL_WORKFORCE.md` | Specialist departments, agent contract, competence states |
| V2-F | `MAINAI_V2_OFFLINE_KNOWLEDGE_PACKS.md` | Knowledge pack format, versioning, provenance |
| V2-G | `MAINAI_V2_SOVEREIGN_IDENTITY.md` | Identity, key hierarchy |
| V2-H | `MAINAI_V2_SOVEREIGN_RECOVERY.md` | Reset levels, Encrypted Life Image, restore |
| V2-I | `MAINAI_V2_ORB_OPERATING_SHELL.md` | Orb/workspace/intent UI architecture |
| V2-J | `MAINAI_V2_IMPLEMENTATION_PLAN.md` | Dependency graph, phases, what can build now vs. after #245 |

## 7. What this stage deliberately does NOT decide

- Exact on-device ML/model choices for local specialists (V2-E's concern, and partly a
  hardware-availability question, not an architecture question).
- Exact cryptographic primitives for the key hierarchy (V2-G names the *shape*; algorithm
  selection is an implementation-phase decision requiring current best-practice review at
  build time, not baked into an architecture doc that will age).
- Whether Sentinel ships as a separate binary/service or a MainAI subsystem — a real
  packaging decision V2-J's dependency graph should surface as an open question, not silently
  assume.
