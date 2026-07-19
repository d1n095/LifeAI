# Claude Code Handover
**Date:** 2026-07-19 (FKP v1.1)
**Package version:** Founder Knowledge Pack v1.1
**Status summary:** `claude/founder-only-launch` CI-green (run `29689918729`, commit `e9b4b76`), not merged, not deployed. FKP v1.1 complete. OD-02/03/04/17 analyzed, not implemented. Dependency staircase written.

---

## OPERATING RULES — READ BEFORE ANYTHING ELSE

1. Do not start over. A working application exists — inspect `03_ARCHITECTURE/CURRENT_ARCHITECTURE.md` and the actual repo before proposing anything.
2. Do not remove or rewrite working code without documented technical reason.
3. Verify before claiming. Re-verify anything labeled reported-only against actual code.
4. GitHub is source of truth — not this file, not chat history, not any AI's memory.
5. No API keys in frontend, Git, logs, or client bundles — ever.
6. No writes to main/deploy branch for security/auth/data-model changes without review.
7. AI provider independence: no domain logic hardcodes a specific vendor — see `backend/app/providers/`.
8. Do not proceed past any trust/approval gate without the required approval — see `01_FOUNDER_VISION/NON_NEGOTIABLE_PRINCIPLES.md`.

## STEP 1 — BEFORE ANYTHING ELSE (already resolved in v1.1, verify it still holds)

Repository identity is resolved: `d1n095/LifeAI`. It is not `d1n095/savings-story-scanner` — those are different, unrelated products. See `07_CONFLICTS_AND_GAPS/CONFLICT_REGISTER.md` CONFLICT-01. Confirm current branch/tip with `git status` / `git log` before proceeding; do not assume the tips recorded here (`f0a1975` deploy branch, `e9b4b76` founder-only-launch) are still current without checking.

## WHERE EVERYTHING IS IN THIS PACKAGE

| What you need | Where |
|--------------|-------|
| Vision and constitutional principles | `01_FOUNDER_VISION/` |
| Full domain/requirement scope (external conversation register) | `01_FOUNDER_VISION/DOMAIN_AND_REQUIREMENT_MAP.md` |
| All confirmed decisions, including what's newly shipped | `02_DECISIONS/DECISION_REGISTER.md` |
| Open questions and their resolution status | `02_DECISIONS/OPEN_DECISIONS.md` |
| OD-02/03/04/17 alternatives/risk/cost/dependency analysis | `02_DECISIONS/OD_02_03_04_17_ANALYSIS.md` |
| Requirement/design candidates (not decisions) | `02_DECISIONS/CANDIDATE_REQUIREMENTS.md` |
| Verified current LifeAI architecture | `03_ARCHITECTURE/CURRENT_ARCHITECTURE.md` |
| What's built vs what's only designed | `03_ARCHITECTURE/BUILT_VS_DESIGNED.md` |
| Long-term target architecture (with stack-translation caveat) | `03_ARCHITECTURE/TARGET_ARCHITECTURE.md` |
| AI orchestration design | `03_ARCHITECTURE/AI_RESOURCE_ORCHESTRATION.md` |
| Adaptive work orchestration design | `03_ARCHITECTURE/ADAPTIVE_WORK_ORCHESTRATION.md` |
| Module register | `04_PRODUCT_AND_MODULES/MODULE_REGISTER.md` |
| Security requirements | `05_SECURITY_AND_TRUST/SECURITY_REQUIREMENTS.md` |
| Current status, verified | `06_PROJECT_STATUS/CURRENT_STATUS.md` |
| Next recommended steps | `06_PROJECT_STATUS/NEXT_RECOMMENDED_STEPS.md` |
| Conflicts between documents, resolved where possible | `07_CONFLICTS_AND_GAPS/CONFLICT_REGISTER.md` |
| What can't be verified from documents | `07_CONFLICTS_AND_GAPS/MISSING_INFORMATION.md` |
| What's stale/superseded | `07_CONFLICTS_AND_GAPS/STALE_INFORMATION.md` |
| Session bootstrap for a new agent | `08_HANDOVER/SESSION_BOOTSTRAP.md` |
| Conversation Context Resolver (binding standard, with H-01 amendment) | `08_HANDOVER/AGENT_CONTEXT_RULES.md` |
| Prioritized dependency staircase (founder-login → knowledge import → project memory) | `09_DEPENDENCY_STAIRCASE/DEPENDENCY_STAIRCASE.md` |
| Original source phase-gate structure | `09_DEPENDENCY_STAIRCASE/SOURCE_PHASE_GATES.md` |
| Historical/unrelated-product material (`savings-story-scanner`), correctly relabeled | `10_HISTORICAL_DOMAIN_MATERIAL/` |
| What changed between FKP v1.0 and v1.1, file by file | `00_MASTER_INDEX/CHANGELOG_FROM_V1.md` |

## EXACT NEXT STEP

See `06_PROJECT_STATUS/NEXT_RECOMMENDED_STEPS.md` and `09_DEPENDENCY_STAIRCASE/DEPENDENCY_STAIRCASE.md`. Step 1 in the staircase (merge + deploy `claude/founder-only-launch`) requires explicit founder approval and should not be performed by an agent unprompted. Steps 2 onward (MFA, ownership column, knowledge import, project memory) can be worked on independently once approved to proceed, per the founder's standing instruction not to be asked to choose between options before analysis exists.
