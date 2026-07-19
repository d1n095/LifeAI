# Next Recommended Steps — 2026-07-19
**Source:** This FKP v1.1 pass, informed by `09_DEPENDENCY_STAIRCASE/DEPENDENCY_STAIRCASE.md`.
**Replaces:** FKP v1.0's version, which pointed toward UPGRADE_26 v2 and the Lovable prompt series as the immediate next steps — those are designed for a different stack and are not the actual next step for LifeAI (see `03_ARCHITECTURE/TARGET_ARCHITECTURE.md` caveat).
**Note:** This is a recommendation, ranked by dependency order, not an instruction to proceed without founder approval where approval is required. See `09_DEPENDENCY_STAIRCASE/DEPENDENCY_STAIRCASE.md` for full detail on every step below.

---

1. **Founder decision: merge `claude/founder-only-launch` and (separately) approve a deploy.** This is the single largest unblocking action available right now — everything else in the staircase assumes founder-only auth is live. Both the merge and the deploy are founder-approval actions per standing instructions; neither has been done in this session and neither should be done without that explicit approval.
2. **Founder account hardening (OD-02): add TOTP.** Recommended before the account accumulates anything more valuable than login access — i.e., before step 4 below. Analysis only exists today (`02_DECISIONS/OD_02_03_04_17_ANALYSIS.md`); not implemented.
3. **Add `owner_id` to `documents`/`projects` (OD-04 recommendation).** Cheap now, painful later. Do this as part of, not before, step 4 — no reason to migrate empty/near-empty tables in isolation.
4. **Knowledge import v1.** Extend the existing RAG ingest pipeline (`backend/app/rag/`) into a founder-knowledge intake path with provenance tracking, starting with this very FKP v1.1 package as the first real import candidate. This is where a Founder Vault storage decision (OD-03) becomes concretely necessary for anything sensitive.
5. **Project memory v1.** Build the Conversation Context Resolver's core (current message + last 3–5 verbatim + last 15 as timeline, correction/supersession handling) on top of LifeAI's existing `conversations`/`messages` tables, plus a minimal decision ledger.
6. **Founder Control Room / agent orchestration foundation.** Only once 4–5 are stable — this is where `03_ARCHITECTURE/AI_RESOURCE_ORCHESTRATION.md` and `ADAPTIVE_WORK_ORCHESTRATION.md` start becoming real code instead of design documents.
7. **Everything after that (Founder UserAI, Source Hub/Life Library expansion, LifeWeb, future users/Life City)** stays deferred, unchanged from the source phase-gate structure — see `09_DEPENDENCY_STAIRCASE/DEPENDENCY_STAIRCASE.md` Phases 4–7.

None of steps 2–7 has been started. Step 1 requires the founder's explicit go-ahead and is not something this session will do unprompted.
