# LifeAI project-knowledge inventory — model and Pass-1 extraction

Founder directive, 2026-11-15 ("Execution Authority, Integration and Complete Project
Knowledge"), Priority 3: "PR #424 captures part of the future vision, but it is not the
complete requirements inventory... Use the existing memory, project understanding, source
provenance and work-candidate architecture to design a unified, source-traceable project
model. Extract actual individual requirements, decisions, dependencies and unresolved
questions. Classify implementation status and identify contradictory or superseded
requirements. Do not replace the repository's existing knowledge foundations with another
parallel database. Produce measurable coverage against the actual source corpus. Keep the
future modules deferred."

**This document does not propose a new model.** The model the directive asks for already
exists, purpose-built for exactly this, in `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`'s
§4.2/§6.4 architecture. What follows is (1) the exact mapping from that existing
architecture onto the directive's four record types, and (2) a real, executable,
re-runnable extraction pass over the actual docs corpus, with measured coverage — not a
toy example.

## 1. The existing pipeline (no parallel database)

```
MemorySourceUnit  -->  KnowledgeClaim  -->  InterpretationProposal  -->  ProjectEntity  -->  WorkCandidate
(raw ingested doc)     (atomic, cited      (candidate signal:          (authoritative,         (should this
                        extracted claim)     "this claim might be       status-tracked,          become real,
                                              a requirement/            SUPERSEDABLE)            governed work?)
                                              decision/dependency/
                                              open_question")
```

Every arrow above is an EXISTING, already-reviewed model/service, cited exactly:

- `app/models/project_entities.py`'s `InterpretationProposal` — `source_claim_id` (owner-
  anchored FK to `knowledge_claims`), `proposed_entity_type`, `classifier_strategy`,
  `classifier_confidence`, `classifier_reasoning`, `status`, `promoted_to_entity_id`,
  `provenance` (JSONB). **This is the directive's "atomic requirement/decision/dependency/
  open-question" record shape, already built.**
- `app/models/project_entities.py`'s `ProjectEntity` — `entity_type`, `title`, `summary`,
  `status`, `derived_from_claim_id` (source-traceable back to the original claim),
  `supersedes_entity_id` (**the directive's "identify ... superseded requirements", already
  a real column**), `authority`/`basis`/`confidence`.
- `app/models/work_candidate.py`'s `WorkCandidate` — `source_entity_id`, `title`,
  `rationale`, `dependencies` (JSONB list — **the directive's "dependencies", already a real
  field**), `priority`, `status`, `provenance`.
- Governance invariant, quoted directly from `ProjectEntity`'s own module docstring:
  **"SIGNAL PRODUCER != TRUTH WRITER"** — `ProjectEntity` is "never created directly; the
  only path here is `app.project_entities.service.promote_interpretation_proposal()`,
  which always requires the caller's own explicit authority/basis." This document and the
  extraction pass below are signal producers. They create no `ProjectEntity` rows. Nothing
  here bypasses that governance.

**Conclusion: "design a unified, source-traceable project model" is already satisfied by
code that exists today.** The actual, honest gap is that almost nothing has been run
THROUGH this pipeline yet for the ~110 planning/architecture documents this repository
already contains — not that the pipeline is missing.

## 2. Pass-1 extraction: what was actually done

`backend/scripts/mainai/extract_project_knowledge_inventory_pass1.py` (new, this PR) scans
the real docs corpus with eight named, auditable regex classifiers (never free-text LLM
interpretation — every record is independently re-checkable by re-running the script) and
emits `InterpretationProposal`-SHAPED records (`proposed_entity_type`, `classifier_strategy`,
inferred status, source `file:line`, `text`) to
`docs/LIFE_PROJECT_KNOWLEDGE_INVENTORY_PASS1.json`.

**This is reconnaissance, not ingestion.** It writes no database row of any kind. Creating
a real `KnowledgeClaim` first requires the source document to exist as a real
`MemorySourceUnit` (Library ingestion) — a separate, substantial, NOT-done-here step, and
this sandbox has neither live Postgres nor AI-provider access to perform it. This script's
output is (a) something a human/the founder can review directly today, and (b) a coverage
baseline a future, real claim-extraction pass over these same documents can be checked
against once they are actually ingested.

### Corpus scope (explicit)

Every `docs/*.md` file plus `CLAUDE.md` and `README.md` — **111 files, 19,943 lines**.
Deliberately excludes `docs/BRANCH_REGISTRY.md`: that file is a continuously-changing,
session-authored PR-tracking log, not stable requirements-source content: including it would
make the script's own re-run output non-stable in a way that defeats "measurable coverage."
(It was tested with `BRANCH_REGISTRY.md` included: 112 files / 28,644 lines / 188 records —
noted here for transparency, not used as the headline number, for the reason above.)

### Measured coverage (Pass 1 only — see §4 for what Pass 2+ would add)

| Classifier (named, auditable) | What it finds | Hits |
|---|---|---|
| `superseded_marker` | "supersedes"/"superseded"/"ersätter" | 91 |
| `checkbox_done` | `- [x]` completed items | 27 |
| `checkbox_pending` | `- [ ]` open items | 25 |
| `open_question_marker` | "öppna fråg"/"open question"/TODO/FIXME | 5 |
| `status_declaration_marker` | `**Status: ...**` explicit declarations | 6 |
| `build_block_marker` | "Byggspärr" (build-block/dependency) | 3 |
| `decision_marker` | "Beslutat" | 1 |
| **Total** | | **158** |

By proposed entity type: **98 decisions, 52 requirements, 5 open questions, 3 dependencies.**

This is a lower bound by construction — eight narrow, high-precision patterns over one pass,
not a claim of exhaustiveness. §4 names the concrete next passes.

## 3. Contradictory/superseded requirements actually found (cited, not asserted)

Sampled directly from the 91 `superseded_marker` hits in
`docs/LIFE_PROJECT_KNOWLEDGE_INVENTORY_PASS1.json` — these are the corpus's OWN explicit
supersession statements, not an inference this document makes independently:

- `docs/AUTH_THREAT_MODEL.md:1` — "Hotmodell och designbeslut — cookie-baserad session
  (ersätter JWT i localStorage)" — a real, named architectural supersession.
- `docs/AUTH_THREAT_MODEL.md:3` — "Ersätter `docs/SECURITY_BLOCKERS.md` post 2."
- `docs/CLAUDE_AUTO_RECOVERY_AUTHORITY_LANE.md:3` — "**SUPERSEDED.** #165 has merged onto
  integration tip."
- `docs/FOUNDER_KNOWLEDGE_BOOTSTRAP.md:228-229` — an explicit `status:`/`supersedes:`
  metadata pair, i.e. this document already models supersession as structured data in at
  least one place, independently of this inventory effort.

Already-documented contradictions from this session's own prior work (not re-derived here,
cross-referenced for completeness): the Blueprint `#378/#379` vs `#387-#401` overlap and the
`0069` migration-revision collision (`#408`/`#411`/`#412`) — both in `docs/
BRANCH_REGISTRY.md`, both excluded from this file's corpus scope per §2 but real,
cited, already-tracked examples of exactly what this directive asks the inventory to surface.

## 4. What Pass 1 deliberately does not cover (named, not hidden)

- **Development history / PR bodies.** The directive names this explicitly ("across
  existing files and development history"). This pass covers files only. PR titles/bodies
  (192+ open PRs, hundreds more merged) are a real, large, separate corpus — a dedicated
  future pass, likely via `app.github_evidence`'s already-existing repo-wide PR/issue read
  capability (see `docs/LIFE_SAFE_PLANNER.md`/the GitHub evidence work from earlier in this
  session), not a new capability.
- **Full-text/semantic requirement extraction.** Pass 1 is marker-based (checkbox/decision/
  dependency/open-question SHAPED lines). A real `KnowledgeClaim`-extraction pass (once
  these documents are ingested as `MemorySourceUnit` rows) would find requirement-shaped
  prose Pass 1's narrow patterns miss — this is the natural Pass 2, using the EXISTING
  ingestion pipeline, not a new one.
- **Promotion to real `ProjectEntity`/`WorkCandidate` rows.** Explicitly out of scope here
  (§1's governance invariant) and requires founder/human review per record, same as any
  other `InterpretationProposal`.

## 5. Future modules stay deferred

PR #424's three product areas (Life Innovation & Patent Intelligence, ElevenLabs research,
multi-coin Life Compute & Mining) are **not** promoted into any `ProjectEntity` row by this
work, and are not present in the Pass-1 output above (that document is itself excluded from
nothing — it simply predates this scan and contains no marker-matching lines of its own
besides its own `**Status:**` declaration, which Pass 1 DOES pick up, correctly, as a single
`decision`/`declared` record). They remain exactly as deferred as PR #424 already stated.

## Se även

- `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §4.2/§6.4 — the architecture this document
  maps onto, not replaces.
- `docs/LIFE_PROJECT_KNOWLEDGE_INVENTORY_PASS1.json` — the full 158-record Pass-1 output.
- `backend/scripts/mainai/extract_project_knowledge_inventory_pass1.py` — the reproducible
  extraction script; re-run any time the corpus changes.
- `docs/BRANCH_REGISTRY.md` — this session's own PR-tracking log (excluded from this
  document's corpus scope per §2, but the place where this session's own decisions,
  collisions, and supersessions are already tracked).
