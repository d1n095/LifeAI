# Founder Knowledge Pack — Master Index
**Version:** 1.1
**Created:** 2026-07-19
**Supersedes:** FKP v1.0 (externally supplied, 2026-07-19), corrected per the founder's explicit instruction to fix the codebase conflation, review-overlay integration, and manifest issues that FKP v1.0's own audit (`FKP_V1_AUDIT.md`, carried forward in this folder) identified.
**Location:** `fkp/` in `d1n095/LifeAI`, branch `claude/fkp-v1.1` (docs-only, based on the deploy branch `claude/det-kommer-mer-879lcm`, independent of the code branch `claude/founder-only-launch`).

---

## What changed from v1.0

See `CHANGELOG_FROM_V1.md` in this folder for the full file-by-file diff. In short: FKP v1.0 conflated LifeAI with an unrelated repository (`savings-story-scanner`/My Money Master) in its architecture/status documents; that material is now correctly relabeled as historical in `10_HISTORICAL_DOMAIN_MATERIAL/`, and the architecture/status documents were rewritten from direct inspection of the real LifeAI codebase. The founder-supplied review overlay and the external conversation-record package ("samtalsregistret") are now integrated into the package structure rather than kept as separate loose layers.

## Package structure

```
00_MASTER_INDEX/
  MASTER_INDEX.md            ← this file
  READING_ORDER.md           ← where to start
  CHANGELOG_FROM_V1.md        ← what changed and why, file by file
  FKP_V1_AUDIT.md             ← the review overlay's audit that drove most v1.1 corrections
  PACKAGE_MANIFEST.json       ← machine-readable inventory with checksums (generated last)
  SHA256SUMS.txt              ← checksums for all v1.1 files (generated last)

01_FOUNDER_VISION/
  FOUNDER_CONSTITUTION.md
  MASTER_PRODUCT_VISION.md
  NON_NEGOTIABLE_PRINCIPLES.md
  DOMAIN_AND_REQUIREMENT_MAP.md   ← from the external conversation register

02_DECISIONS/
  DECISION_REGISTER.md
  REJECTED_ALTERNATIVES.md
  OPEN_DECISIONS.md
  OD_02_03_04_17_ANALYSIS.md      ← alternatives/risk/cost/dependency analysis, no decision made
  CANDIDATE_REQUIREMENTS.md       ← requirement/design candidates, not decisions

03_ARCHITECTURE/
  CURRENT_ARCHITECTURE.md         ← verified LifeAI current state
  BUILT_VS_DESIGNED.md            ← verified LifeAI built/designed split
  TARGET_ARCHITECTURE.md          ← designed target state, with stack-translation caveat
  AI_RESOURCE_ORCHESTRATION.md    ← AI provider routing, quotas, scheduling
  ADAPTIVE_WORK_ORCHESTRATION.md  ← task graph, agent autonomy, approval gates

04_PRODUCT_AND_MODULES/
  MODULE_REGISTER.md              ← LifeAI modules with status and location

05_SECURITY_AND_TRUST/
  SECURITY_REQUIREMENTS.md        ← binding security rules, Trust Engine, data zones

06_PROJECT_STATUS/
  CURRENT_STATUS.md               ← verified status as of 2026-07-19
  IMPLEMENTED_AND_VERIFIED.md     ← what is actually built and code-verified
  NEXT_RECOMMENDED_STEPS.md       ← recommended action order

07_CONFLICTS_AND_GAPS/
  CONFLICT_REGISTER.md            ← contradictions, resolved where possible
  MISSING_INFORMATION.md          ← what cannot be determined without real verification
  STALE_INFORMATION.md            ← superseded documents, including v1.0 items superseded by v1.1

08_HANDOVER/
  CLAUDE_CODE_HANDOVER.md         ← operating rules + where everything is
  SESSION_BOOTSTRAP.md            ← quick context for any new agent session
  AGENT_CONTEXT_RULES.md          ← Conversation Context Resolver + H-01 amendment

09_DEPENDENCY_STAIRCASE/
  DEPENDENCY_STAIRCASE.md         ← prioritized: founder-login → knowledge import → project memory
  SOURCE_PHASE_GATES.md           ← original Phase 0–7 gate structure, carried forward

10_HISTORICAL_DOMAIN_MATERIAL/
  README.md                       ← why this folder exists
  CURRENT_ARCHITECTURE_SAVINGS_STORY_SCANNER.md
  BUILT_VS_DESIGNED_SAVINGS_STORY_SCANNER.md
  MODULE_REGISTER_SAVINGS_STORY_SCANNER.md
```

## Source document authority levels

| Level | Meaning | Examples |
|-------|---------|---------|
| CANONICAL | Founding document; cannot be overridden by any session | FOUNDER_CONSTITUTION, NON_NEGOTIABLE_PRINCIPLES |
| CONFIRMED | Explicitly decided; binding | DECISION_REGISTER entries |
| BUILT / CI-GREEN | Implemented and passing automated checks | Founder-only launch, core LifeAI product surface |
| DESIGNED | Designed but not yet built or applied | UPGRADE_26 v2 (with stack caveat), Target Architecture, Orchestration docs |
| UNDER ANALYSIS | Alternatives compared, recommendation given, no decision made | OD-02/03/04/17 |
| DEFERRED | Explicit future candidate, not scheduled | Life City, LifeWeb, Founder UserAI as separate identity |
| HISTORICAL | Real, verified, but describes a different/unrelated product | Everything in `10_HISTORICAL_DOMAIN_MATERIAL/` |
| SUPERSEDED | Older version replaced by a later one | See `07_CONFLICTS_AND_GAPS/STALE_INFORMATION.md` |
| UNVERIFIED | Cannot determine from available material in this session | See `07_CONFLICTS_AND_GAPS/MISSING_INFORMATION.md` |
