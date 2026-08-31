# MainAI Local Intelligence School

**Principle:** External APIs (Claude/OpenAI/Grok/Gemini/…) are **teachers / critics /
examiners**, not MainAI’s permanent brain.

Package: `backend/app/mainai_school/`

## Learning cycle

LOCAL ATTEMPT → (optional) TEACHER CRITIQUE → DISTILL PRINCIPLE → PRACTICE →
INDEPENDENT EXAM → COMPETENCE EVIDENCE → LOCAL-FIRST NEXT TIME.

## Honest learning levels

| Level | Meaning |
|---|---|
| 1 Memory | facts, corrections, lessons |
| 2 Playbook | procedures / routing rules |
| 3 Retrieval | aliases, packages, context construction |
| 4 Specialization | agent profiles / selection |
| 5 Weight training | **only when real training ran** |

`TRAINING DATA CREATED ≠ MODEL TRAINED ≠ IMPROVED ≠ SAFE`.

## Reuses (no parallel universe)

- `EngineeringLesson` via distill → `record_lesson`
- `capability_reality` for school competence provenance
- `founder_memory` for curriculum notes
- workforce performance (later wire)

**No new Alembic migration** in this foundation.

## Invariants

See `INVARIANTS` in `types.py` — especially:

- EXTERNAL MODEL ≠ MAINAI / AUTHORITY / REQUIRED PATH
- TEACHER RESPONSE ≠ VERIFIED TRUTH
- ONE EXAM PASS ≠ PERMANENT COMPETENCE
- LEARNING ≠ AUTHORITY WIDENING

## Offline

`audit_offline_capabilities()` — MainAI must remain meaningful without APIs.
Provider invoke remains disabled until independent Claude gates say otherwise.

## Also in this package

- `specialization.py` — local specialist lifecycle (gap → teach → exam → probation → verified)
- `self_learn.py` — self-teaching without APIs + failure-layer classification
- `teachers.py` — domain-specific teacher scoring, disagreement (no blind majority), peer lessons
- `memory_tiers.py` — HOT/WARM/COLD **policy only** (summaries never replace provenance)
- `metrics.py` — EXTERNAL DEPENDENCY RATIO by domain

## Reuse map (do not invent parallel stores)

| Concern | Canonical | School action |
|---|---|---|
| Lessons | `mainai_execution.lessons` / `EngineeringLesson` | already via distill |
| Competence | `capability_reality` | already (`school.*` keys) |
| Agent gradebook | `workforce.performance` | wire later — no second ledger |
| Durable HOT/WARM/COLD | `app.memory_tiers` (when on tip) | bridge classifier; no second store |
| Pred→outcome | `prediction_learning` (frontier) | wire later for calibration |
| Founder teaching | `record_lesson_from_founder_correction` / `founder_language` | wire after language slice on tip |
| Self-improve ROI | `self_improvement_roi` / bounded SI spine | wire — school is curriculum, not SI editor |
| Executive local-first | `mainai_executive` (#235) | **thin adapter** after merge |

**No school Alembic migration** until a query cannot be answered from lessons + capability events + performance rollups + JSON provenance.
