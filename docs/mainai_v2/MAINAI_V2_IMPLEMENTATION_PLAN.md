# MainAI V2 — Implementation Plan, Dependency Graph, Phasing (Stage V2-J)

**Status:** design-only synthesis. Does not modify, rebase, or depend on PR #245 / candidate
SHA `818dfb732da47901eb5ae06ffdd9c829fe00c4c5`. This document ties together V2-A through V2-I
(all in this directory) into one buildable sequence.

---

## 1. Dependency graph

```
V2-A  Architecture Map (vocabulary, trust chain, constitution)
  │
  ├──▶ V2-B  Guardian / Trust Kernel ──────────────┐
  │                                                  │
  ├──▶ V2-G  Sovereign Identity ────────────────────┤  (Guardian needs a real identity
  │         (key hierarchy, RootAuthorityProof)      │   primitive to verify against;
  │                                                  │   Identity needs Guardian as the
  │                                                  │   thing it's authenticating INTO)
  │                                                  ▼
  │                                        V2-B + V2-G together =
  │                                        the actual root-of-trust, required
  │                                        before anything below can be "real"
  │                                        rather than "designed"
  │
  ├──▶ V2-C  Privacy Boundary Engine (extends existing app.egress_policy)
  │
  ├──▶ V2-D  Sentinel (event mesh + defensive autonomy)
  │         requires: V2-B (defensive-autonomy pre-authorization is a Guardian-issued
  │                    scope object, per V2-D's own design) + existing execution_envelopes
  │
  ├──▶ V2-E  Local Workforce (extends existing app.workforce)
  │         requires: V2-F (knowledge_pack_bindings field references packs that must
  │                    have a real format first)
  │
  ├──▶ V2-F  Offline Knowledge Packs
  │
  └──▶ V2-H  Sovereign Recovery (Encrypted Life Image, Fast Restore)
            requires: V2-G (key hierarchy IS the recovery key hierarchy — same keys)
                      + the ALREADY-PROVEN same-device restart durability pattern
                        (tonight's 10-subprocess campaign) for the same-device half;
                        genuinely new work only for the cross-device half

V2-I  Orb Operating Shell
  requires: V2-A §1 vocabulary (direct) + V2-E (invisible routing dispatches to
            Local Workforce specialists) + V2-D (VISIBLE_SURFACE "show security"
            reveals Sentinel's incident view) + V2-H (VISIBLE_SURFACE "show recovery")
  — i.e. V2-I is the LAST thing that can be real, since it's the surface over everything else.
```

**Reading order for a human reviewing this program:** A → B → G → C → D → E → F → H → I → J
(this document). That is also, not coincidentally, close to the correct *build* order — root
of trust first, then the systems that need it, then the shell that surfaces them all.

## 2. What already exists and needs zero new build (confirmed by direct code reading across all forks)

- `app.execution_envelopes` — authority proposal/authorization split. V2-B's Guardian sits
  as a precondition on top of this, does not replace it.
- `app.evidence_claim` — the shared evidence-truth gate (subject-exact-match, real outcome
  required), independently re-verified 6/8 scenarios tonight, currently under independent
  certification via PR #245.
- `app.workforce.kill_switch` / `workforce_authority_epoch` — the DB-backed, race-proof
  defensive-containment primitive V2-B's Guardian and V2-D's defensive autonomy both
  generalize rather than reimplement.
- `app.workforce` (Stage T) — `WorkforceAssignment`'s authority-envelope fields, which V2-E's
  Agent Contract maps onto directly (4 new columns identified, no redesign).
  `app.workforce.department_evidence` — the department concept V2-E's specialist domains
  extend.
  `app.workforce.injection` — `looks_like_prompt_injection`/`scrub_authority_mutations`,
  which V2-D's input-security pipeline generalizes from "agent output" to "arbitrary
  file/link content."
- `app.mainai_school` — the exam/competence machinery V2-E's competence-state STALE
  transition and V2-F's knowledge-pack exam-suite linkage both reuse (with the
  already-known, already-being-fixed "fake exam" gap from tonight's campaign noted as a
  dependency to close, not re-litigate).
- `app.egress_policy` — a real, working default-deny egress gate V2-C extends (adds
  semantic classify/minimize/generalize stages) rather than replaces.
- `docs/AUTH_THREAT_MODEL.md` / today's session-cookie auth — stays completely untouched;
  V2-G's Sovereign Identity is layered strictly above it (`SESSION ACCESS != ROOT
  AUTHORITY`), never modifies it.
- Tonight's proven same-device, multi-restart durability pattern (10 genuine subprocess
  restarts, zero fidelity decay, real append-only checkpoints) — the actual mechanism V2-H's
  Fast Restore and V2-I's Intent Object persistence both build on for the same-device case.

**This matters for phasing:** roughly half of V2's total design surface is "extend a
proven, tested V1 primitive," not "build from zero." The genuinely new engineering is
concentrated in: Guardian's own small kernel, Sentinel's detection engines, the Privacy
Boundary Engine's semantic minimization logic, Sovereign Identity's key hierarchy and
`RootAuthorityProof`, Offline Knowledge Packs' format + distribution, Sovereign Recovery's
cross-device half, and the Orb's actual OS-level input/window-control integration.

## 3. Implementation phases

**Phase 0 — Foundations (can start immediately, zero runtime risk):**
- Finalize V2-A's constitution as the canonical, single-source-of-truth doc (already done).
- Schema design for V2-E's 4 new `WorkforceAgentProfile`/`WorkforceAssignment` columns
  (`domain`, `vault_policy`, `require_local_model`, `knowledge_pack_bindings`) — additive
  migration, reviewable and mergeable independent of everything else, since it only ADDS
  nullable columns nothing yet reads.
- Offline Knowledge Pack schema (V2-F) as a standalone Python dataclass module + JSON
  Schema, with unit tests against the schema itself — no runtime wiring.

**Phase 1 — Root of trust (Guardian + Sovereign Identity, V2-B + V2-G):**
- This MUST land before anything in Phase 2+ can be considered "real" rather than
  "designed," because Sentinel's defensive autonomy, Recovery's key hierarchy, and the
  Orb's takeover-state enforcement all cite Guardian/Identity primitives that don't exist
  yet as code.
- Concretely: the `RootAuthorityProof` challenge-response mechanism (replacing the weak
  founder-ack denylist+regex check found tonight) is the single highest-value first build
  — it closes a REAL, already-identified gap in currently-shipping code (the
  `clear_kill_switch_for_recovery()` ack check), not just a V2 aspiration.
- Guardian's generalized containment scope (built on `workforce_authority_epoch`) ships
  second, since it depends on `RootAuthorityProof` for its own "who can invoke recovery"
  question.

**Phase 2 — Privacy Boundary Engine (V2-C):**
- Extends `app.egress_policy`. Independent of Phase 1 except for using Guardian's audit
  trail conventions. Can build in parallel with Phase 1 once Phase 0's schema work lands.

**Phase 3 — Sentinel foundation (V2-D):**
- Event mesh schema + correlation engine can build without Guardian (pure data pipeline).
- Defensive autonomy's actual *execution* (the pre-authorized action list) requires Phase 1
  complete, since every defensive action is Guardian-scoped by design.

**Phase 4 — Local Workforce domain content (V2-E) + Knowledge Packs (V2-F):**
- The mechanism (Stage T) already exists; this phase is populating real specialist
  contracts and real knowledge packs (starting with 1-2 domains, not all 11, to prove the
  pattern before scaling — Swedish Consumer Law + one security domain are good first picks
  given existing project focus).

**Phase 5 — Sovereign Recovery (V2-H):**
- Same-device half reuses proven infrastructure — low risk, can start once V2-G's key
  hierarchy exists (Phase 1).
- Cross-device half (Recovery Capsule, progressive hydration) is genuinely new and should
  be its own sub-phase with its own adversarial testing pass (data loss during hydration is
  a severe, hard-to-detect failure mode — matches this session's own "prove it doesn't
  decay under real restarts" standard, extended to cross-device).

**Phase 6 — Orb Operating Shell (V2-I):**
- Deliberately last. It's the integration surface over everything else, and its own
  hardest problem (OS-level TAKEOVER_STATE input pre-emption) is platform-specific
  engineering, not architecture — should not block earlier phases' progress.
- Start with the Intent Object persistence layer (reuses proven durability pattern,
  low risk) before the actual window-control/input-hook integration (platform-specific,
  higher risk, should be prototyped per-OS before committing to one approach).

## 4. What can be built before PR #245's independent certification completes

Per the founder's explicit instruction, nothing in this V2 lane may modify existing
runtime authority/security paths yet. Concretely safe to build NOW, fully isolated:

- All architecture docs (done, this stage).
- V2-F's Knowledge Pack schema as a standalone module with its own tests (no runtime import
  from anything in the workforce/authority path).
- V2-E's schema migration (additive-only, nullable columns, zero existing code reads them
  yet — genuinely inert until wired).
- Sentinel's event-mesh data model and correlation-engine logic as a standalone module,
  tested against synthetic event fixtures, not wired to any real event source yet.
- Prototype/spike code for V2-G's key-wrapping flow, in isolation, with its own test suite
  proving the wrap/unwrap math — no integration with the real session/auth system.

## 5. What must wait until after #245's certification (and why)

- Anything that touches `app.evidence_claim`, `app.workforce.kill_switch`, or
  `app.execution_envelopes` in a way that changes their *behavior* (extending Guardian's
  containment generalization beyond a design doc into real code that calls these modules).
  Reason: these are exactly the modules #245 is being independently re-attacked on right
  now; landing V2 code that depends on their current shape risks needing rework if the
  independent examiner finds something #245's own builder (this session) missed — matches
  the whole night's own lesson that first-pass review, even careful review, misses real
  bugs about 1 time in 3 so far.
- `RootAuthorityProof` wiring into the REAL `clear_kill_switch_for_recovery()` call site —
  design and standalone-test it now, but the actual swap-in should happen after #245 lands
  cleanly, so the fix doesn't get entangled with whatever #245's independent re-attack finds.
- Any Guardian code that would gate the real `resolve_delegation()`/`apply_verification_decision()`
  call sites #245 just fixed.

## 6a. Whole-architecture egress-hazard search (2026-09-03, read-only, no refactor)

Real code search across the existing certified runtime, to answer "where must V2-C's
Privacy Boundary Engine eventually sit." This is a survey, not a refactor plan — nothing
below was changed.

**Already gated (no new hazard):** `app.providers.registry`, `app.provider_planning.service`,
`app.routers.chat/library/workbench`, `app.rag.ingest/media_import`,
`app.jobs.handlers.corpus_review` — all already route through the existing
`app.egress_policy` gate before any external AI provider call. V2-C's own design doc
(§ found while writing it) correctly identified this and designed to extend, not replace,
this gate. Confirmed by direct search: this is the single largest already-safe surface.

**Real AI-provider call sites** (the actual outbound network boundary, one layer below the
`egress_policy`-gated callers above): `app.providers.anthropic_provider` /
`openai_provider` / `gemini_provider` / `verification`, `app.agent_orchestration`,
`app.mainai_execution.execution_job` / `planner` / `lesson_conflicts`,
`app.mainai_runtime_contract`, `app.routers.agents`. These are the actual functions that
serialize a prompt/context payload onto the wire — the eventual integration point for
V2-C's semantic classify/minimize/generalize layer is here, upstream of `egress_policy`'s
own redaction step (matching V2-C's own "sits upstream of egress_policy" design).

**Genuinely unguarded hazard surface (no privacy-boundary-style gate exists today):**
- **File upload/storage**: `app.routers.documents`, `app.models.document`,
  `app.storage.references` — a document upload path has no equivalent
  classify/minimize/sanitize step; whatever a user uploads goes to storage as-is (correct
  for *local* storage, but this is exactly the boundary V2-C's pipeline would need to sit
  in front of before anything derived from an uploaded document could ever egress as a
  learning/telemetry signal).
- **Raw stack-trace exposure**: `app.routers.chat`, `app.project_entities.service`,
  `app.work_candidates.service`, `app.rag.claims`, `app.storage.references` all use
  `traceback.format_exc()`/`exc_info=True` — a stack trace can legitimately contain local
  file paths, usernames, and occasionally interpolated variable values. None of these are
  currently routed through any sanitization step before landing in logs. This is the
  single most concrete, near-term-actionable finding from this search: V2-C's
  `sanitize_text()` (already implemented, 22/22 tests passing) is a drop-in candidate for
  a log-formatter integration here — LOW effort, HIGH value, and does not touch any
  authority/security path (§4/§5's before/after-#245 split still applies: this would be
  new, additive logging-formatter code, not a modification of `execution_envelopes`/
  `evidence_claim`/`kill_switch`, so it qualifies as "safe to build now" under §4 — but is
  intentionally NOT built in this round, since it touches real, currently-running log
  output and deserves its own focused PR + review, not a side effect of this foundation
  round).
- **General logging** (39 files import `logging` directly): not exhaustively audited line
  by line in this pass — flagged as a systematic risk class (the same PII-in-logs pattern
  as the stack-trace finding above, just not yet traced to specific call sites) rather than
  claiming a false completeness here.

**Migration map (order Privacy Boundary integration should happen in, once V2-C exits the
foundation stage):** (1) log-formatter sanitization hook (smallest, safest, highest
immediate value) → (2) real AI-provider call sites, upstream of the existing
`egress_policy` gate → (3) file-upload-derived-signal paths → (4) full audit of the
remaining ~35 unaudited logging call sites.

## 6b. Sentinel future event-source map (2026-09-04, read-only, no wiring)

Real code search across the existing certified runtime, to answer "where would each of
Sentinel's 11 `app.sentinel.adapters` interface stubs eventually plug in." This is a survey
for a later wiring phase, not a wiring plan — nothing below was changed, and none of it
implies any of these sources is connected to `app.sentinel` today.

| Event source | Real file/module | Plausible `SecurityEventType`(s) | Plausible Guardian `ContainmentScope` |
|---|---|---|---|
| Agent/worker process spawn | `app.agent_coordination.adapters` (`asyncio.subprocess.Process`) | `PROCESS_STARTED`, `SCRIPT_EXECUTION` | `WORKFORCE` |
| Supervisor git shell-outs | `app.development_supervisor.production_entry` / `production_worktree` | `PROCESS_STARTED`, `SCRIPT_EXECUTION` | `WORKFORCE` |
| Execution-job worktree git ops | `app.mainai_execution.worktree` (`_run_git`) | `PROCESS_STARTED` | `WORKFORCE` |
| Test-run/tooling subprocesses | `app.mainai_execution.verify`, `app.project_memory`, `app.development_operator.service` | `SCRIPT_EXECUTION` | `WORKFORCE` |
| Document upload | `app.routers.documents`, `app.models.document`, `app.storage.references` | `UNTRUSTED_FILE_OPENED`, `MASS_FILE_READ`/`MASS_FILE_WRITE` (bulk) | `OWNER` or `VAULT` depending on destination |
| RAG ingest/media import | `app.rag.ingest`, `app.rag.media_import` | `UNTRUSTED_FILE_OPENED` | `OWNER` |
| Egress policy outcome | `app.egress_policy.service` (`enforce_egress_policy`) | `UNEXPECTED_EGRESS`, `NEW_OUTBOUND_DESTINATION` | `PROVIDER` / `NETWORK` |
| AI provider network calls | `app.providers.{anthropic,openai,gemini,deepseek,ollama,openrouter}_provider`, `registry`, `app.provider_planning.service` | `NEW_OUTBOUND_DESTINATION` (new provider/endpoint) | `PROVIDER` |
| Vault-adjacent data access | `app.routers.library`/`chat`, `app.rag.retrieve`, `app.workforce.context`/`broker`/`provider_worker` | `VAULT_ACCESS_ATTEMPT`, `CREDENTIAL_READ_ATTEMPT` | `VAULT` |
| Workforce authority/kill-switch | `app.workforce.kill_switch`, `authority`, `broker`, `activation_gates` | `AGENT_SCOPE_ESCALATION`, `PRIVILEGE_ESCALATION_ATTEMPT` | `WORKFORCE` |
| Guardian's own policy mutation | `app.guardian.service` (`apply_new_policy`) | `POLICY_CHANGE`, `SECURITY_SETTING_CHANGED` | `GLOBAL` / `OWNER` |
| Session/token revocation | `app.token_revocation`, `app.routers.auth`, `app.models.refresh_token` | `DEVICE_TRUST_CHANGED`, `RECOVERY_TRIGGER` | `OWNER` |

**Negative-space findings (deliberately NOT future Sentinel adapters):**
- `app.egress_policy` already does deny/never-egress-marker classification with its own
  disclosure ledger. A future Sentinel adapter here must *observe* its allow/deny outcome
  (an already-classified result, not a raw payload) rather than re-implement egress
  classification a second time — two independent classifiers over the same payload would
  drift out of sync exactly the way this session's recurring "evidence exists != evidence
  supports claim" bug class has shown elsewhere.
- There is no literal `vault` table or model-loading/plugin-loading mechanism in this
  codebase today — "Vault" is the RLS-protected database itself, and providers are static
  Python modules, not dynamically loaded. `MODEL_CHANGED`/`PLUGIN_CHANGED` therefore has no
  real hook to map yet; mapping it further now would be speculative, not evidence-based.
- RLS (`app.rls`) already enforces per-owner Vault access at the DB layer and fails closed.
  A `VAULT_ACCESS_ATTEMPT` adapter is still genuinely additive (RLS enforces but does not
  correlate or alert), not redundant.

The unaudited logging/stack-trace sanitization gap from §6a remains the connective tissue
for a possible future log-formatter-driven event source, but stays a separate, non-Sentinel
migration step (§6a's step 1) — not itself a Sentinel adapter.

## 6c. Sovereign Identity + Life Recovery future integration map (2026-09-05, read-only, no wiring)

Real code search across the existing certified runtime, to answer "where would
`app.sovereign_identity` and `app.life_recovery` eventually plug in." A survey for a later
wiring phase, not a wiring plan — nothing below was changed.

| Integration point | Real file/module | What it does today | Future connection |
|---|---|---|---|
| Login/session/logout | `app.routers.auth` (`login`, `refresh`, `logout`, `logout_all`), `app.models.refresh_token`, `app.models.revoked_access_token` | Cookie-based JWT sessions, rotation-family refresh tokens, revocation-on-replay | `app.sovereign_identity`: session creation would eventually call `evaluate_identity_assertion()` to establish a `ProofLevel` — today a successful login is binary, not graded; `logout_all` is the closest existing analogue to `revoke_device()`'s durable-revocation discipline, but operates on token rows, not a `DeviceRecord` |
| Device identity | *(none exists)* | No `device_id` field anywhere on `RefreshToken`, `User`, or any session table | Biggest current gap for `app.sovereign_identity.DeviceRecord`/`enroll_device()` — needs a stable client-generated device identifier that does not exist yet, not just a wiring step |
| Provider credentials | `app.config` (`openai_api_key`, `anthropic_api_key`, `google_api_key`, `deepseek_api_key`, `openrouter_api_key`) | Plaintext `str \| None` pydantic-settings fields sourced from env vars | **Existing gap, not future-integration**: no `KeyPurpose`-bound wrapping exists; `KeyPurpose` has no `PROVIDER_KEY` member yet — a real omission if this is ever addressed |
| Account export | `app.routers.account` (`GET /api/account/export`) → `app.account.export.export_account_data()` | Auth-gated plaintext JSON dump of account data | Direct current-state precursor to `app.life_recovery`'s Encrypted Life Image — today's export has no manifest, no component typing, no integrity hash, no encryption |
| Account erasure | `app.routers.account` (`DELETE /api/account`) → `app.account.erasure.erase_account_data()` | Irreversible data deletion | Today's only "destructive reset" analogue, but it deletes data rather than erasing keys — not the same shape as `SECURE_RESET`'s crypto-erase, since nothing is encrypted today for there to be a key to erase |
| File storage | `app.storage.local_fs` | Local, private-VPS-volume, SHA-256 content-addressed (`{hash[:2]}/{hash}`), no per-user encryption | Content-addressing means identical plaintext already collides at the storage-key level across owners today (dedup is intentional) — confirms a real design change, not just a wiring step, would be needed before `KeyPurpose.DOCUMENT_KEY` could apply per-owner. No Supabase storage usage anywhere in this codebase. |
| Vault / encryption at rest | `app.rls` (RLS on `conversations`, `document_chunks`, `documents`, etc.) | Enforces per-owner *access* at query time | **Existing gap, concrete**: zero at-rest encryption exists anywhere in this codebase outside the two new packages (confirmed by search) — RLS does nothing against a raw DB/disk compromise. Exactly the gap `KeyPurpose.VAULT_KEY` is designed for, but nothing currently calls it. RLS itself remains a real, independent, working control — a future Vault integration is additive, not a replacement. |
| User settings | *(none exists)* | No dedicated settings model (`find app -iname "*settings*"` empty) | `ComponentType.USER_SETTINGS` in Life Image has no current source table to read from yet |
| Agent/workforce state | `app.models.{workforce,workforce_ops,agent_coordination,agent_task}` | Ordinary RLS-protected Postgres rows, no snapshot/serialization format of their own | Future `ComponentType.AGENT_COMPETENCE_STATE`/`LOCAL_AGENTS` source |

**Negative-space finding**: RLS already does real, independent work at the access-control
layer — a future Vault encryption-at-rest integration point is additive on top of it, not a
sign RLS is insufficient for what it actually does.

## 6d. Operating Shell future integration map (2026-09-05, read-only, no wiring)

Real code search across the existing certified runtime, to answer "where would
`app.operating_shell` eventually plug in." A survey for a later wiring phase, not a wiring
plan — nothing below was changed.

| Integration point | Real file/module | What it does today | Future connection |
|---|---|---|---|
| Chat orchestration | `app.routers.chat` → `app.context.resolver` | The closest existing analogue to "MainAI as primary UI"; `app.context.resolver` is a **separate, pre-existing, unrelated** rule-based conversation-turn classifier (continuation/new_topic/correction/pronoun_reference — not an LLM call) | `operating_shell.context`/`delegation` — naming collision risk flagged below, not a functional conflict |
| Agent delegation | `app.workforce` (assignment/broker/kill-switch — already covered by Sentinel's and Guardian's own hazard searches) | Real specialist-agent assignment and authority control | `operating_shell.delegation` |
| Document readers | `app.rag.{ingest,extract,media_import}` | Real document parsing pipeline | `operating_shell.workspace`/`restore` (`WorkspaceDocument` references) |
| Filesystem ops beyond `local_fs` | `app.rag.zip_import`/`library_import`, `app.mainai_execution.worktree`/`verify`, `app.development_supervisor.production_worktree`, `app.project_memory` | Real file I/O, all execution/dev-tooling-scoped | `operating_shell.risk` — these would classify EXTERNAL_EFFECT/DESTRUCTIVE, never OBSERVATIONAL, if ever exposed as a workspace action |
| Session state | `app.routers.auth`, `app.models.refresh_token` | Unchanged from §6c's findings | `operating_shell.control`/workspace ownership scoping |

**Confirmed absent (no false completeness claimed)**: no browser automation library (Playwright/Selenium/Puppeteer) exists anywhere in this codebase — `WorkspaceDocument`'s browser-tab-reference field has no real backing yet. No window/desktop-automation concept exists at all. No frontend directory in this worktree to check for an existing "orb" concept.

**Naming collision (not a functional conflict, but a real future-reader risk)**: `app.context.resolver` (existing, chat-turn classification) and `app.operating_shell.context` (new, referring-expression resolution for "this"/"that"/"continue") are unrelated modules with confusingly similar names. Worth renaming or cross-referencing explicitly before either package grows further.

**MOST IMPORTANT FINDING — real architectural risk, not hypothetical.** `app.operating_shell.IntentObject` (new, in-memory-only, no DB backing) substantially overlaps an already-existing, DB-backed 3-tier pipeline:
- `app.models.life_intent.LifeIntent` — durable life intents with `title`/`intent_kind`/`state`/`classification_basis`/`authority`/`provenance`, linked to both `mainai_goal_id` and `memory_thread_id`; `LifeIntentBlocker` is a real blocker model with `category`/`status`/`description`/`basis`/`reference_kind` — a near field-for-field precedent for the new `IntentObject`'s `blockers`/`state`/`linked_*` fields.
- `app.models.work_candidate.WorkCandidate` — "a claim that structured understanding MIGHT be worth turning into real, governed work — never a claim it is authorized," with the only path to real work being `app.work_candidates.service.authorize_work_candidate()`. This is the EXACT "FUTURE PLAN != FUTURE AUTHORITY" distinction `IntentObject`'s own docstrings independently re-derived from scratch.
- `app.models.mainai_execution.MainAIGoal`/`MainAIPlan`/`MainAITask` — the actual execution-authority-bearing goal lifecycle.

This means a future wiring phase must make an explicit choice before connecting `IntentObject` to anything real: either (a) `IntentObject` becomes an in-memory/UI-facing *view* composed over `LifeIntent`+`WorkCandidate`+`MainAIGoal`, never a fourth independent source of truth, or (b) `IntentObject` is explicitly re-scoped to cover only what those three don't — e.g. purely conversational/workspace-session intents that never become governed work. Leaving this undecided risks two silently-diverging answers to "what is the user's goal" once both systems are live. Not resolved in this round — flagged here so it cannot be wired past accidentally.

## 6. Open design risks (stated honestly, not glossed over)

- **TAKEOVER_STATE's OS-level enforcement** genuinely differs in feasibility across
  platforms (Wayland restricts the class of global input observation this needs) — V2-I
  flags this, this document reiterates it as a real, unresolved engineering risk, not a
  solved problem.
- **Cross-device Sovereign Recovery** has no proof-of-durability yet, unlike the
  same-device case — this is the single largest "designed but not yet empirically hardened"
  gap in the whole V2 program and should get the same adversarial-testing rigor this
  session applied to same-device restart before anyone treats it as trustworthy.
- **Sentinel's actual detection engines** (malware scanning, exploit monitoring, etc.) are
  architecturally scoped here but are themselves large, specialized engineering efforts
  (potentially involving third-party security engines, not just LifeAI-original code) —
  this document deliberately does not pretend V2-D's design makes that engineering trivial.
- **Guardian's "remain small" constraint** is a real tension against Sentinel's correlation
  engine needing enough context to make good containment decisions fast — V2-B and V2-D's
  designs resolve this by keeping Guardian mechanical (no judgment calls, only checking
  pre-authorized scopes), but this boundary should be re-examined once real defensive
  scenarios are prototyped, since it's easy to erode "small" under real-world pressure to
  make Guardian "just a little smarter."
- **Knowledge Pack jurisdiction currency** (a pack claiming to be current Swedish law) is a
  genuine ongoing-maintenance problem, not a one-time build — V2-F's update-manifest design
  handles the mechanism, but the actual staffing/process for keeping packs current is a
  business/operations question outside this document's scope.
