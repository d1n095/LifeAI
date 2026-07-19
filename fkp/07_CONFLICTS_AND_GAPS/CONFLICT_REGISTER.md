# Conflict Register
**Rule:** Conflicts are documented with both versions and sources. Nothing is resolved silently — every resolution below cites the verification that resolved it.

---

## CONFLICT-01: Repository identity — RESOLVED
**Version A:** `d1n095/savings-story-scanner` — analyzed and verified in a prior session (clone, `289420342b`, 2026-07-16)
**Version B:** `d1n095/LifeAI` — named as target in `01_EXECUTIVE_CONTEXT_AND_CURRENT_STATE.md` and founder instructions
**Resolution:** Version B. Directly confirmed by inspecting `d1n095/LifeAI` in this session: Next.js/FastAPI stack, `Dockerfile.combined`, `render.yaml`, CI workflow, Alembic migrations — none of which match `savings-story-scanner`'s TanStack/Bun/Supabase stack. The two repositories are real, distinct, and unrelated. See D-29 in `02_DECISIONS/DECISION_REGISTER.md` and `03_ARCHITECTURE/CURRENT_ARCHITECTURE.md`.
**Consequence for FKP v1.0:** Its `CURRENT_ARCHITECTURE.md`, `BUILT_VS_DESIGNED.md`, `MODULE_REGISTER.md` Tier 2, `IMPLEMENTED_AND_VERIFIED.md`, and `SESSION_BOOTSTRAP.md` all described Version A's codebase as if it were LifeAI's verified state. That material is preserved, correctly relabeled, in `10_HISTORICAL_DOMAIN_MATERIAL/`.

## CONFLICT-02: Attribution of `extensions.digest()` fix — carried forward, no new evidence
**Version A:** `03_AI_TRACKS_AND_ATTRIBUTION.md` (MainAI_chat_handover_2026-07-17) attributes the fix and local test to Gemini.
**Version B:** A prior Claude chat session — the fix was made, local PG16 was started, and migration was applied in that conversation.
**Status:** Unchanged from FKP v1.0. Version B is treated as correct (directly observed at the time); this FKP v1.1 pass had no way to independently re-verify a prior session's chat log, so this remains a carried-forward, not re-verified, resolution. Low-stakes — does not affect any LifeAI code, since UPGRADE_26 v2 (where this fix lives) has not been applied to LifeAI.

## CONFLICT-03: Migration count (9 vs 11) — historical, not applicable to LifeAI
**Version A:** Earlier summary documents mention "9 existing migrations" (`savings-story-scanner`/My Money Master).
**Version B:** Direct file listing showed 9 files in one location, 11 in a nested archive copy including 2 additional Fas A migrations.
**Resolution:** This question is about `savings-story-scanner`'s migration count, not LifeAI's. It is moot for LifeAI, which has its own independent Alembic chain (`0001_baseline_schema` → `0004_pgvector_document_chunks`, 4 migrations on the deploy branch; a 5th, `0005_founder_role`, exists on the unmerged `claude/founder-only-launch`). Filed as historical/not-applicable rather than resolved, since resolving the original question would require re-inspecting `savings-story-scanner`, which is out of scope for LifeAI work.

## CONFLICT-04: Canonical data model (original UPGRADE_26 vs v2) — carried forward, with LifeAI-specific caveat added
**Version A:** Original `UPGRADE_26_main_ai_foundation.sql` — 11 tables including `ai_memory_items`, `ai_decisions`, `ai_tasks`, `ai_handovers`, `ai_document_chunks`.
**Version B:** UPGRADE_26 v2 — 10 tables; `ai_memory_items` replaced by `raw_material` + `knowledge_objects`; `ai_decisions` retired (folded into `knowledge_objects`); tasks/handovers/chunks deferred.
**Resolution (unchanged from v1.0):** Version B wins over Version A within the source material's own history.
**New in v1.1:** Neither version has been applied to, or even directly compared against, LifeAI's actual current schema (`backend/app/models/`). Both were designed against Supabase/Postgres-RLS assumptions that don't hold for LifeAI's SQLAlchemy/Alembic stack. See `03_ARCHITECTURE/TARGET_ARCHITECTURE.md`'s caveat and `02_DECISIONS/DECISION_REGISTER.md` D-17. This is now also tracked as a dependency-staircase concern: Step 4 ("knowledge import v1") in `09_DEPENDENCY_STAIRCASE/DEPENDENCY_STAIRCASE.md` explicitly recommends extending LifeAI's existing `document_chunk` model incrementally rather than applying UPGRADE_26 v2 wholesale.

## CONFLICT-05: Test result reporting (TCRIT) — carried forward, now explicitly scoped as not applicable
**Version A:** Some summaries in earlier handover files reported all tests as PASS.
**Version B:** Corrected test file: 26 PASS / 0 FAIL / 1 INCONCLUSIVE (TCRIT).
**Resolution:** Version B remains authoritative for UPGRADE_26 v2's own local test run. **New in v1.1:** since UPGRADE_26 v2 is not applied to LifeAI (CONFLICT-04), TCRIT's inconclusive result does not block anything in LifeAI today — it would need to be re-run against whatever adapted design eventually results from comparing UPGRADE_26 v2 to LifeAI's real schema, not assumed resolved by this old test run. See OD-09 in `02_DECISIONS/OPEN_DECISIONS.md`.

## CONFLICT-06 (new in v1.1): FKP v1.0's own manifest integrity
**Version A:** `PACKAGE_MANIFEST.json` and `SHA256SUMS.txt` in FKP v1.0 listed 22 files under `99_RAW_ORIGINALS/` that were not actually present in the curated ZIP archive delivered; the checksums for the manifest/checksum files themselves did not verify against the archive.
**Version B:** The 24 actual curated Markdown files in FKP v1.0's archive verified correctly against their own listed checksums.
**Resolution:** This was flagged by the review overlay's own audit (finding K-04, `FKP_V1_AUDIT.md`) before this FKP v1.1 pass began. Fixed in this package by generating `00_MASTER_INDEX/PACKAGE_MANIFEST.json` and `SHA256SUMS.txt` only after all v1.1 content files were finalized, listing only files actually present in this package — see `00_MASTER_INDEX/CHANGELOG_FROM_V1.md`.

## H-02 status-field correction (carried forward from the review overlay, not a numbered conflict but binding going forward)
The review overlay (`FKP_V1_AUDIT.md` §H-02) correctly identified that FKP v1.0 blurred "confirmed decision," "current MVP operational choice," and "idea/candidate" into one undifferentiated status vocabulary. FKP v1.1 uses the overlay's corrected status vocabulary throughout: observation → idea → requirement candidate → hypothesis → solution alternative → compared → tested → recommended → founder-decided → implemented → verified. Documents in this package use CONFIRMED/DESIGNED/BUILT/CI-GREEN/DEFERRED/UNDER ANALYSIS explicitly rather than a single blanket "decided" label.
