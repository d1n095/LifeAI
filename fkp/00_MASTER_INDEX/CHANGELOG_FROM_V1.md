# Changelog: FKP v1.0 → FKP v1.1
**Trigger:** Founder instruction, 2026-07-19: "Skapa därefter FKP v1.1 som docs-only: korrekt LifeAI-inventering, relabel gamla `savings-story-scanner` som historiskt material, rätta manifest/checksummor och integrera review-overlay samt samtalsregistret."
**Method:** FKP v1.0's curated archive, the founder-supplied review overlay (`FKP_v1_REVIEW_OVERLAY.zip`), and the raw-source "conversation register" (`MainAI_Conversation_Knowledge_Pack_2026-07-19`) were all read in full and reconciled against direct inspection of `d1n095/LifeAI`. This package (`fkp/` in the repository, branch `claude/fkp-v1.1`) is the result — docs-only, no code changes.

---

## Per-file disposition

| FKP v1.0 file | FKP v1.1 disposition |
|---|---|
| `00_MASTER_INDEX/MASTER_INDEX.md` | Rewritten — new inventory, new structure notes |
| `00_MASTER_INDEX/READING_ORDER.md` | Rewritten — updated pointers |
| `00_MASTER_INDEX/PACKAGE_MANIFEST.json` | Regenerated last, lists only files actually in this package (fixes K-04) |
| `00_MASTER_INDEX/SHA256SUMS.txt` | Regenerated last, same fix |
| `01_FOUNDER_VISION/FOUNDER_CONSTITUTION.md` | Carried forward unchanged (provenance header only) |
| `01_FOUNDER_VISION/MASTER_PRODUCT_VISION.md` | Carried forward, Step 1 section updated to reflect it shipped |
| `01_FOUNDER_VISION/NON_NEGOTIABLE_PRINCIPLES.md` | Carried forward, one gate added (merge/deploy of founder-only-launch) |
| *(new)* `01_FOUNDER_VISION/DOMAIN_AND_REQUIREMENT_MAP.md` | New — the "samtalsregistret" domain/requirement map, integrated per instruction |
| `02_DECISIONS/DECISION_REGISTER.md` | Carried forward, D-27–D-30 added |
| `02_DECISIONS/OPEN_DECISIONS.md` | Updated — resolution status added per item; OD-02/03/04/17 point to new analysis doc |
| `02_DECISIONS/REJECTED_ALTERNATIVES.md` | Carried forward, one entry added (auth-scope over-broadening, self-corrected) |
| *(new)* `02_DECISIONS/OD_02_03_04_17_ANALYSIS.md` | New — the alternatives/risk/cost/dependency analysis the founder asked for |
| *(new)* `02_DECISIONS/CANDIDATE_REQUIREMENTS.md` | New — renamed/relocated from the review overlay's `NEW_REQUIREMENTS_CAPTURE.md` |
| `03_ARCHITECTURE/CURRENT_ARCHITECTURE.md` | **Rewritten** — was `savings-story-scanner`, now real LifeAI facts (K-01) |
| `03_ARCHITECTURE/BUILT_VS_DESIGNED.md` | **Rewritten** — same reason |
| `03_ARCHITECTURE/TARGET_ARCHITECTURE.md` | Carried forward, stack-translation caveat added |
| `03_ARCHITECTURE/AI_RESOURCE_ORCHESTRATION.md` | Carried forward, H-01 pointer note added |
| `03_ARCHITECTURE/ADAPTIVE_WORK_ORCHESTRATION.md` | Carried forward, H-01 pointer note added |
| `04_PRODUCT_AND_MODULES/MODULE_REGISTER.md` | **Rewritten** — Tier 2 was `savings-story-scanner`, now real LifeAI modules |
| `05_SECURITY_AND_TRUST/SECURITY_REQUIREMENTS.md` | Carried forward, RLS-pattern section corrected (Supabase syntax ≠ LifeAI's `app/rls.py`) |
| `06_PROJECT_STATUS/CURRENT_STATUS.md` | **Rewritten** — verified facts, not reported-only |
| `06_PROJECT_STATUS/IMPLEMENTED_AND_VERIFIED.md` | **Rewritten** — same |
| `06_PROJECT_STATUS/NEXT_RECOMMENDED_STEPS.md` | **Rewritten** — points to the dependency staircase instead of UPGRADE_26 v2/Lovable (K-02) |
| `07_CONFLICTS_AND_GAPS/CONFLICT_REGISTER.md` | Updated — CONFLICT-01/02/03/04/05 resolved or scoped; CONFLICT-06 (manifest) and the H-02 status-field note added |
| `07_CONFLICTS_AND_GAPS/MISSING_INFORMATION.md` | Updated — resolved items removed, genuine remaining gaps kept |
| `07_CONFLICTS_AND_GAPS/STALE_INFORMATION.md` | Updated — this pass's own supersessions logged |
| `08_HANDOVER/CLAUDE_CODE_HANDOVER.md` | Rewritten pointer table for the v1.1 structure |
| `08_HANDOVER/SESSION_BOOTSTRAP.md` | **Replaced** with the review overlay's `CORRECTED_SESSION_BOOTSTRAP.md`, further updated (K-03) |
| `08_HANDOVER/AGENT_CONTEXT_RULES.md` | Carried forward, H-01 amendment section added |
| *(new)* `09_DEPENDENCY_STAIRCASE/DEPENDENCY_STAIRCASE.md` | New — the prioritized founder-login → knowledge-import → project-memory staircase the founder asked for |
| *(new)* `09_DEPENDENCY_STAIRCASE/SOURCE_PHASE_GATES.md` | New — the "samtalsregistret"'s Phase 0–7 structure, carried forward, integrated per instruction |
| *(new)* `10_HISTORICAL_DOMAIN_MATERIAL/*` | New — old `savings-story-scanner` architecture/module content, relabeled as historical (K-01) |
| *(new)* `00_MASTER_INDEX/FKP_V1_AUDIT.md` | New — the review overlay's audit report, carried forward, integrated per instruction |

## What is explicitly NOT in this package

- The raw `.docx`/`.png`/nested-zip binary originals from FKP v1.0's `99_RAW_ORIGINALS/` — not copied into this repository. They remain in the original FKP v1 archives outside the repo. Only descriptive/inventory documents were brought in.
- Any actual source code from `savings-story-scanner` (`.tsx`, `.sql`, `.ts` files) — deliberately excluded per the audit's own recommendation. Only architecture/module *descriptions* were relabeled and kept, in `10_HISTORICAL_DOMAIN_MATERIAL/`.
- Any implementation of OD-02/03/04/17 — analysis only, per the founder's explicit instruction.
- Any code changes at all — this is a docs-only branch (`claude/fkp-v1.1`), independent of the `claude/founder-only-launch` code branch.
