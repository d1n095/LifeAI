# Founder Notes — Current-Status & Priority Overlay (2026-11-09)

**Status: documentation only. This document adds current-status and priority *references* on top
of the existing Founder notes. It does NOT rewrite or delete any historical record — the original
notes remain authoritative for their own time and are preserved verbatim.** `first_cycle_go=false`.

This is Mission 1 of the GitHub-first Founder directive. Per that directive, the earlier
local-patch workflow is withdrawn: this correction is made **directly in GitHub**, as an isolated
documentation branch and draft PR, using the existing repository documentation plus PR #368 and
PR #369. It preserves history and adds clear current-status and priority references rather than
editing the historical notes' content.

---

## 1. What "the Founder notes" are, and what this overlay corrects

There is no single `FOUNDER_NOTES.md` file. The Founder-facing knowledge notes are a small set of
documents, each historically accurate for when it was written:

| Note | Written as | What it still is | What this overlay clarifies |
|---|---|---|---|
| `docs/FOUNDER_KNOWLEDGE_BOOTSTRAP.md` | A **design proposal** (mid-2026), "väntar på godkännande" | Still the canonical design for Founder Vault / Context Bundle / decisions & handovers | Its §6/§8 open questions (UserAI RLS isolation, `founder` role, vault storage location) are **still open**; much of the *memory* substrate it anticipated has since been built (see §2) |
| `docs/LIFE_FOUNDER_MEMORY.md` | The **built** founder-memory foundation (migration 0049) | Accurate and current | Reinforced, not corrected — it is the live mechanism for capability #2/#8 memory |
| PR #368 docs (`FOUNDER_REQUIREMENTS_DELTA_2026-11-08`, `FOUNDER_KNOWLEDGE_INGESTION_READINESS_2026-11-08`, `BLUEPRINT_SOURCE_READINESS_GAP_2026-11-08`, `DISASTER_RECOVERY_DECISION_PACKAGE_2026-11-08`) | Continuity/ingestion/DR readiness (2026-11-08) | Current | Carried forward; the ingestion matrix (78 passing tests; ChatGPT importer top gap → PR #369) is the authority for capability #1 |

**The one substantive ambiguity corrected:** the bootstrap note (`FOUNDER_KNOWLEDGE_BOOTSTRAP.md`)
reads as if the memory/knowledge foundation were entirely unbuilt ("väntar på godkännande … inget
implementeras"). That was true when written. It is **no longer** true: the memory substrate it
scoped has since been built as real modules — `app.capability_reality` (0048),
`app.founder_memory` (0049), `app.founder_memory_signals` (0053), `app.inspectable_memory`,
`app.memory_threads`. What remains genuinely unbuilt from that note is narrower: the enforced
session-start **Context Bundle** (`scripts/build_context_bundle.py`) and the §8.3 architecture
decisions (UserAI RLS isolation, the `founder` DB role, vault storage location). This overlay
records that delta **as a navigation note, without altering the historical text.**

## 2. Current status of the Founder-notes subject matter (source-grounded)

| Topic from the notes | Then (as written) | Now (2026-11-09, verified) |
|---|---|---|
| Founder memory (decisions/corrections/preferences/goals) | "Grundarminne" gap, designed-not-built | **Built**: `app.founder_memory` (0049) + candidate-signal staging (0053). IMPLEMENTED+TESTED. |
| Capability self-model (what's real vs configured) | Not yet designed | **Built**: `app.capability_reality` (0048). IMPLEMENTED+TESTED. |
| Context Bundle (enforced session-start context) | Design only | **Still design only** — `scripts/build_context_bundle.py` unwritten. Recommended next glue step. |
| Decisions/handovers as versioned objects | Design only | Partially realized as docs (`docs/HANDOFF_*`, PR #368); no `founder_vault/` tree yet. |
| UserAI/MainAI hierarchy & RLS isolation | Open architecture question | **Still open** (bootstrap §8.3). Not a first-start blocker. |
| Founder as final authority | Principle | **Structural**: envelopes/spend/egress/approval gates enforce it. |

## 3. Priority reference (what to do, in order)

The single operational priority and its ordering live in
`docs/LIFE_OPERATIONAL_PRIORITY_2026-11-09.md` and
`docs/LIFE_FIRST_START_READINESS_MATRIX_2026-11-09.md` (this package). In one line for the Founder
notes' readers:

> **Priority: activate the foundations that already exist (merge → wire → prove one Founder-gated
> cycle), not build new ones. Nothing is merged yet; `first_cycle_go=false`.**

## 4. Preservation guarantee

- No historical note's content is deleted or rewritten by this package.
- The two primary notes (`FOUNDER_KNOWLEDGE_BOOTSTRAP.md`, `LIFE_FOUNDER_MEMORY.md`) receive only
  a short, clearly-dated **navigation banner** at the very top, pointing here. Everything below
  the banner is unchanged. This is "clarify, don't erase."
- The retailer/city-map + employee-wellbeing concept is preserved as
  `docs/FUTURE_BLUEPRINT_INPUT_GEO_RETAILER_INTELLIGENCE.md` (`FUTURE_BLUEPRINT_INPUT`), not as an
  implementation task.

---

*Package companions: `LIFE_OPERATIONAL_PRIORITY_2026-11-09.md` (Mission 2),*
*`LIFE_FIRST_START_READINESS_MATRIX_2026-11-09.md` (Mission 4),*
*`FUTURE_BLUEPRINT_INPUT_GEO_RETAILER_INTELLIGENCE.md` (Mission 3).*
