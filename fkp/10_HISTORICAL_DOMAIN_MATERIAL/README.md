# Historical / Domain Material — NOT LifeAI's Current State
**Why this folder exists:** FKP v1.0's `03_ARCHITECTURE/CURRENT_ARCHITECTURE.md`, `BUILT_VS_DESIGNED.md`, and `04_PRODUCT_AND_MODULES/MODULE_REGISTER.md` (Tier 2) described the codebase at `d1n095/savings-story-scanner` ("My Money Master") as if it were LifeAI's verified current state. It is not. `d1n095/LifeAI` and `d1n095/savings-story-scanner` are real, distinct, unrelated repositories — see `07_CONFLICTS_AND_GAPS/CONFLICT_REGISTER.md` CONFLICT-01 for how this was resolved and `02_DECISIONS/DECISION_REGISTER.md` D-29.

**What this material still is:** legitimate domain and product knowledge about a related-but-separate product (a salary/finance/scheduling app called "My Money Master"). It is preserved here, correctly labeled, per the review overlay's own recommendation (`FKP_V1_AUDIT.md` finding K-01: "flytta savings-story-scanner-beskrivningen till 'historiskt/annat produktunderlag'"). Nothing in this folder should be read as a claim about LifeAI's code.

**What was NOT copied here:** the actual raw source code (`.tsx`, `.sql`, `.ts` files) from `FKP_v1_RAW_SOURCE_ORIGINALS.zip` was deliberately **not** brought into this repository — only these descriptive/inventory documents, which is what the audit itself recommended. If the raw code is ever needed, it exists in the original FKP v1 raw-source archive outside this repository, not here.

**Files in this folder:**
- `CURRENT_ARCHITECTURE_SAVINGS_STORY_SCANNER.md` — FKP v1.0's architecture description of `savings-story-scanner`, unchanged, relabeled.
- `BUILT_VS_DESIGNED_SAVINGS_STORY_SCANNER.md` — FKP v1.0's built/designed table for that product, unchanged, relabeled.
- `MODULE_REGISTER_SAVINGS_STORY_SCANNER.md` — FKP v1.0's Tier 2 module table (salary/OB engine, calendar, planning, finance score, schedule OCR, dashboard routes), unchanged, relabeled.
