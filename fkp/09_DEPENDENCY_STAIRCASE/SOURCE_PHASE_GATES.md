# Open Questions, Risks, and Phases — source phase-gate structure
**Source:** `09_OPEN_QUESTIONS_RISKS_AND_PHASES.md`, part of the `MainAI_Conversation_Knowledge_Pack_2026-07-19` ("samtalsregistret" — the external conversation-record package integrated into FKP v1.1 per the founder's instruction). Carried forward with no content changes.
**Relation to `DEPENDENCY_STAIRCASE.md` in this folder:** that document re-sequences and makes concrete the founder's specific near-term priority (knowledge import + project memory) within the Phase 1→2 boundary below, without altering the exit criteria for Phases 3–7 given here.

---

## 1. Blocking questions for the founder foundation

1. What exact immutable identifier defines the founder: verified email, database UUID, external identity, or a combination?
2. Will founder access require passkey/MFA before production use?
3. Where will private Founder Vault originals be stored and encrypted?
4. What is the exact ownership/RLS migration for `documents`, `projects`, and `tasks`?
5. Which backup mechanism is available on the free services, and how will restoration be tested?
6. What commit currently contains the unpushed founder-only implementation?
7. Which environment variables are still missing from Render?
8. What is the rollback procedure for the first combined-container deployment?

*(v1.1 note: items 1, 3, 4, 6, 7, 8 above have resolutions or analysis as of this pass — see `02_DECISIONS/OPEN_DECISIONS.md`.)*

## 2. Important non-blocking architecture questions

- Which knowledge is founder-private, UserAI-private, project-shared, organization-shared, or public?
- Can Founder UserAI write proposed knowledge directly, or only submit it for MainAI approval?
- What is the first local model and what diagnostic threshold permits it to handle real tasks?
- Which parts of project intelligence belong in Git versus encrypted storage/database?
- How long are raw chats and browsing histories retained?
- What page content may LifeWeb observe, and how is consent displayed?
- What map data source can legally support Life City?
- What content rights are required for YouTube/video ingestion and generated podcasts?

## 3. Principal risks

### Identity collapse
MainAI and UserAI accidentally share authority or memory. Mitigation: explicit identity/scope enforcement, tests, and visible active identity.

### False continuity
An AI reconstructs context incorrectly and treats inference as the founder's exact words. Mitigation: provenance and confidence per item.

### Shared-data leakage
Existing shared tables expose future users' information. Mitigation: ownership model, RLS migration, adversarial tenancy tests.

### Tool overreach
An agent interprets a mode or task as permission to deploy, purchase, message, or delete. Mitigation: intersection-based permissions and approval queue.

### Prompt/document injection
Imported web pages or files instruct agents to ignore system rules. Mitigation: treat content as untrusted data and isolate tool control instructions.

### Evaluation gaming
An agent learns or edits its own test. Mitigation: hidden variants and independent evaluator.

### Free-tier fragility
Sleeping services, quotas, paused projects, and unavailable capacity appear as application failure. Mitigation: visible degraded mode, health checks, alerts, and runbooks.

### Knowledge sprawl
Conflicting ZIPs and summaries become competing truths. Mitigation: originals, checksums, manifest, canonical decision ledger, and supersession links.

## 4. Phase gates

### Phase 0 — Preserve knowledge
Exit criteria: project constitution; master index and source manifest; decision/open-question registers; active-state and handover format; no silent conflict resolution.
*(v1.1 status: DONE — this FKP v1.1 package.)*

### Phase 1 — Founder-only production foundation
Exit criteria: public registration unavailable in UI and backend; exactly one provisioned founder identity; founder checks on protected routes; password reset/email/session controls verified; RLS/ownership tests green; production secrets configured securely; migrations, health, backup, restore, rollback tested; explicit founder deploy approval.
*(v1.1 status: BUILT and CI-green on `claude/founder-only-launch`; not yet merged or deployed — the "explicit founder deploy approval" criterion is the one remaining open item.)*

### Phase 2 — Project intelligence and continuity
Exit criteria: topic cards and decision ledger; current + 3–5 + 15-message context assembly; correction and provenance handling; context-carrying responses; Swedish continuity evaluation.
*(v1.1 status: NOT STARTED — this is "project memory," Step 5 in `DEPENDENCY_STAIRCASE.md`.)*

### Phase 3 — Founder Control Room and agent orchestration
Exit criteria: capability registry; task graph and approval queue; agent diagnostics; cost/resource scheduler; verified handovers and safe-stop behavior.
*(v1.1 status: NOT STARTED — Step 6 in `DEPENDENCY_STAIRCASE.md`.)*

### Phase 4 — Founder UserAI
Exit criteria: separate identity, permissions, and memory; delegation boundary to MainAI; user-controlled memory; explanation and support adaptation.
*(v1.1 status: NOT STARTED — deferred per `DEPENDENCY_STAIRCASE.md`.)*

### Phase 5 — Source Hub and Life Library
Exit criteria: document/chat/media import; provenance and rights; Trust Engine per claim; universal search and generated-product traceability.
*(v1.1 status: PARTIALLY STARTED — "knowledge import v1," Step 4 in `DEPENDENCY_STAIRCASE.md`, covers a first slice of this; full Source Hub/Life Library remains deferred.)*

### Phase 6 — LifeWeb, voice, and multimodal assistance
Exit criteria: extension security model; precise inspected-page navigation; password-vault boundary; voice/orb modes; sensitive-action approvals.
*(v1.1 status: NOT STARTED — deferred.)*

### Phase 7 — Future users and Life City
Exit criteria: isolated UserAI template; public onboarding decision; city/map licensing and privacy model; legal/security review; scalable tenancy and support operations.
*(v1.1 status: NOT STARTED — deferred.)*
