# Stale Information
**Sources:** Comparison across all source documents by date and content, plus this FKP v1.1 pass's own findings.

---

## Confirmed superseded documents (carried forward from FKP v1.0)

| Document | Superseded by | Reason |
|----------|--------------|--------|
| Original `UPGRADE_26_main_ai_foundation.sql` | `UPGRADE_26_v2__main_ai_foundation.sql` | Multiple review rounds found design flaws; v2 is canonical within the source material's own history — though neither is applied to LifeAI, see `07_CONFLICTS_AND_GAPS/CONFLICT_REGISTER.md` CONFLICT-04 |
| `LifeOS_Master(1)` stub files (Architecture.md, Vision.md, etc.) | Later handoff packages | Very sparse placeholders (43–328 bytes); later packages have full content |
| `REQUIREMENT_REGISTRY.csv` (from LIFE_OS_CLAUDE_HANDOFF_PACKAGE_3_) | `REQUIREMENTS_LEDGER.md` | CSV is older; ledger is actively maintained |
| `MY_MONEY_MASTER_SPEC.md` | `MY_MONEY_MASTER_SPEC_V3.md` | V3 is 246KB vs 12KB; clearly later and more complete. Both describe `savings-story-scanner`/My Money Master, now filed in `10_HISTORICAL_DOMAIN_MATERIAL/` |

## New in v1.1 — FKP v1.0 documents superseded by this package

| Document (FKP v1.0) | Superseded by (FKP v1.1) | Reason |
|----------|--------------|--------|
| `03_ARCHITECTURE/CURRENT_ARCHITECTURE.md` | This package's `03_ARCHITECTURE/CURRENT_ARCHITECTURE.md` | Described `savings-story-scanner` as LifeAI's verified state — see CONFLICT-01 |
| `03_ARCHITECTURE/BUILT_VS_DESIGNED.md` | This package's `03_ARCHITECTURE/BUILT_VS_DESIGNED.md` | Same reason |
| `04_PRODUCT_AND_MODULES/MODULE_REGISTER.md` (Tier 2) | This package's `04_PRODUCT_AND_MODULES/MODULE_REGISTER.md` | Same reason; old Tier 2 content preserved, correctly relabeled, in `10_HISTORICAL_DOMAIN_MATERIAL/` |
| `06_PROJECT_STATUS/CURRENT_STATUS.md`, `IMPLEMENTED_AND_VERIFIED.md`, `NEXT_RECOMMENDED_STEPS.md` | This package's versions of the same three files | Reported/unverified status replaced by directly-verified status; next steps pointed at UPGRADE_26 v2/Lovable prompts, now points at the actual dependency staircase |
| `08_HANDOVER/SESSION_BOOTSTRAP.md` | This package's `08_HANDOVER/SESSION_BOOTSTRAP.md` (the review overlay's `CORRECTED_SESSION_BOOTSTRAP.md`, adopted per the audit's own K-03 recommendation) | Told agents the project was "formerly My Money Master/savings-story-scanner" and gave TanStack/Supabase patterns as binding |
| `00_MASTER_INDEX/PACKAGE_MANIFEST.json`, `SHA256SUMS.txt` | This package's regenerated versions | Self-inconsistent (listed files not present in the archive) — audit finding K-04 |

FKP v1.0's `01_FOUNDER_VISION/`, `02_DECISIONS/` (mostly), `05_SECURITY_AND_TRUST/`, `08_HANDOVER/AGENT_CONTEXT_RULES.md`, and `03_ARCHITECTURE/TARGET_ARCHITECTURE.md`/`AI_RESOURCE_ORCHESTRATION.md`/`ADAPTIVE_WORK_ORCHESTRATION.md` are **not** superseded — they were stack-agnostic vision/design content and are carried forward with at most a provenance-header update. See `00_MASTER_INDEX/CHANGELOG_FROM_V1.md` for the complete file-by-file diff.

## Potentially outdated but not confirmed superseded

| Document | Concern | Status |
|----------|---------|--------|
| `PROJECT_STATUS.md` (Life_Dev_Platform_Claude_Package) | Reports specific build step status from earlier in the project; may not reflect current state | Use as history only; describes `savings-story-scanner`, now historical material regardless |
| `MASTER_BACKLOG.md` | Backlog items may be completed, in progress, or reprioritized; no date on individual items | Use as starting point only; describes `savings-story-scanner` |
| All `MainAI_LifeOS_chat_handover_2026-07-17` files | Created 2026-07-17 before the 2026-07-19 session's decisions | Superseded by the 2026-07-19 conversation knowledge pack and FKP v1.0/v1.1 for most decisions |

## Attribution notes
From `04_DECISIONS_CORRECTIONS_AND_OPEN_ITEMS.md` (MainAI_chat_handover): prior handover files show confusion between which AI (Claude, Gemini, Kimi, ChatGPT) produced which specific architectural critique. Attribution notes in prior handovers are rough color, not verified fact. Do not use attribution as authority for any decision. This applies equally to any attribution claims inside FKP v1.0 itself and to this v1.1 package's own review-and-fix history — every claim in this package that matters is backed by a file path, commit hash, or CI run number, not by "an AI said so."
