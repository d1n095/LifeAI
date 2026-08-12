# Life — Requirement Traceability (Provisional)

**Status: PROVISIONAL — part of the Bootstrap Map, not the final requirement traceability.**
Sourced only from the repo/docs corpus and this mandate; the founder's external corpus
(`~/Documents/mainai_intake/`) has not yet been ingested. This matrix will be redone, not just
extended, once `docs/LIFE_SOURCE_FOUNDATION_BOOTSTRAP.md` ships and the full founder corpus is
in the system — see that document.

**Scope note:** this is a themed traceability matrix — each row is a real, sourced requirement
theme extracted from an actual document or the founder's actual words, not a mechanical
per-sentence extraction. Given the volume of existing material (~10,000 lines across
`docs/`+`backend/docs/`), a full atomic `requirement_id`-per-sentence pass was not attempted in
this session; this is the canonical first layer, meant to be extended additively, never
replaced. See `docs/LIFE_CANONICAL_ARCHITECTURE.md`'s "Known gaps" section.

**Status values used below:** ACTIVE, IMPLEMENTED, PARTIAL, MISSING, SUPERSEDED, CONFLICTING,
IDEA, FUTURE, UNKNOWN — exactly the founder's mandated set.

## Source authority key

- **FR** = Founder Requirement/Decision (this mandate, `MAINAI_CONTEXT_BUNDLE.md`, or a recorded
  founder approval in `docs/BRANCH_REGISTRY.md`)
- **FC** = Founder Correction (an explicit founder reversal of a prior AI decision, found in
  `docs/BRANCH_REGISTRY.md`'s recorded history)
- **AI** = AI-authored architecture proposal (a prior Claude Code session's design document,
  not founder-authored, accepted only by continuation unless otherwise noted)
- **CODE** = Implemented Code Reality (verified this session by reading the actual file)

---

## 1. Product / vision

| Requirement | Source | Status | Notes |
|---|---|---|---|
| Life is a plaform/OS for the founder's life, MainAI is its intelligence, not the system itself | FR (this mandate §0) | ACTIVE | Directly matches `MAINAI_ARCHITECTURE.md` §1's existing "System Core / MainAI / External models" principle (AI-authored, but already congruent) |
| MainAI is Vice VD / "my brother, my eye, my hands," always subordinate to founder's final decision | FR (`MAINAI_CONTEXT_BUNDLE.md`) | ACTIVE | Matches `mainai_execution`'s approval-gate pattern (`approval.py`, replan cannot inherit prior approval — proven in V0.3 hardening pass) |
| Products: Life OS (core), My Money Master (finance), 4thepeople AB (physical products) | FR (`MAINAI_CONTEXT_BUNDLE.md`) | IDEA/FUTURE | No code or schema relationship exists between these; My Money Master referenced as `savings-story-scanner-main` in founder's external material, not in this repo |
| Three-layer system: Memory (sorted/indexed) / Stations (autonomous processes) / MainAI (orchestrator) | FR (`MAINAI_CONTEXT_BUNDLE.md`) | PARTIAL | "Memory" = today's RAG+provenance layer (real); "Stations" concept (named autonomous processing units) does not exist as a named abstraction — closest is `mainai_jobs`' `job_kind` dispatch, which is unnamed/untyped as "stations" |
| GitHub is the source of truth | FR (`MAINAI_CONTEXT_BUNDLE.md`) | IMPLEMENTED | `app/integrations/github_client.py`, all MainAI code changes go through real branches/commits/PRs, verified extensively in V0.1-V0.3 |
| Gemini Flash for everyday work, Ollama locally for repetitive tasks, expensive AI only when required | FR (`MAINAI_CONTEXT_BUNDLE.md`) | PARTIAL | Provider abstraction supports this (6 providers incl. Ollama) but no automatic cost/complexity-based routing exists (`AI_ORCHESTRATION_ENGINE.md`'s routing design is architecture-only) |

## 2. Life Core / AI-independence

| Requirement | Source | Status | Notes |
|---|---|---|---|
| Life must function without MainAI/LifeAI and without external AI/LLM | FR (this mandate §0) | PARTIAL | True today for storage/auth/RLS/audit/jobs/RAG-search; false for chat/embedding/planning — see Canonical Architecture §F/§G for the exact boundary |
| AI can control/monitor Life via defined capabilities, but the kernel must work independently | FR (this mandate §0) | MISSING | No capability registry exists (§H of Canonical Architecture) |
| "MainAI är systemets intelligens, inte en extern tjänst" — three-layer System Core/MainAI/External | AI (`MAINAI_ARCHITECTURE.md` §1) | PARTIAL | Pre-existing AI-authored doc, already congruent with founder's new mandate; implementation gap identical to the row above |
| Local MainAI Capability Layer (local reasoning/model fallback) | AI (`MAINAI_ARCHITECTURE.md` §1 target) | FUTURE | Explicitly designed, explicitly not built; Ollama provider exists but is not wired as a fallback-of-last-resort |

## 3. Source Vault / provenance

| Requirement | Source | Status | Notes |
|---|---|---|---|
| Original uploaded files must never be altered/replaced/rewritten/deleted by MainAI; must be structurally enforced by storage/DB permissions, not just prompt policy | FR (this mandate §9) | PARTIAL | `LocalFilesystemStorage` is content-addressed and dedup'd (tamper-evident by construction — you can't "edit" a hash-named blob in place, only replace the whole thing), and `mainai_app`'s DB privileges are already narrowly scoped (S1A's `SECURITY DEFINER`-gated provenance functions) — but there is no single named "Source Vault" invariant and no explicit test asserting `mainai_app` lacks UPDATE/DELETE on the *blob storage layer itself* the way it's proven for `memory_source_units` |
| MainAI runtime should lack normal UPDATE/DELETE on canonical source originals | FR (this mandate §9) | PARTIAL | True for `memory_source_units`/`document_source_units` (proven, tested, `REVOKE UPDATE, DELETE FROM mainai_app`); NOT verified for `documents.storage_key`'s underlying blob file permissions at the OS/filesystem level |
| Immutable, versioned original text snapshot per source | AI (`MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §1.1, §4.8) | IMPLEMENTED | `KnowledgeVersion` (never mutated) + `memory_source_units.content_text`/`content_hash`, S1A fully built |

## 4. Memory architecture

| Requirement | Source | Status | Notes |
|---|---|---|---|
| Two-axis memory model: class (working/episodic/semantic/structured) × scope (user/project/org/shared) | AI (`MEMORY_ARCHITECTURE.md` §1) | PARTIAL | Only `scope=USER` is real today (single-founder system); `MemoryRecord`/`Provenance`/`TrustRecord` types described are a design layer over today's tables, not built as their own schema |
| Dual memory: deterministic memory vs. AI-derived "Life Memory" | FR (this mandate §10) | PARTIAL | Deterministic side (filenames, checksums, timestamps, message IDs/sequence, dedup) is real and extensive (S1A/S1B); "Life Memory" (concepts/goals/decisions/contradictions) has real precedent in `KnowledgeClaim`/`project_entities` (P4, designed not built) but no unified "Life Memory" layer exists as such |
| `founder_memory_notes` — founder memory, only explicit statements, never inferred psychological state | AI (`MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §4.3, P6) | MISSING (designed) | Directly matches founder's §19 requirement almost exactly; not built. `app/context/resolver.py`'s explicit "never infer emotional state" rule (tested: `test_never_infers_emotional_or_psychological_state`) is the closest real precedent and should be the base this builds on |
| Memory Threads — first-class objects linking conversations/messages/documents/decisions/PRs/lessons/goals, many-to-many, mergeable/splittable/versioned | FR (this mandate §11) | MISSING | No equivalent exists anywhere. Closest partial precedents: `SourceRelationship`/`ClaimRelationship` (document/claim-level graph edges, real) and `conversation_segments` (designed, not built, S2). Neither is a cross-source "thread" abstraction |
| Conversations/messages as first-class memory sources | AI (`docs/CORE dsgn`, `MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §6.11, S1C/S2) | PARTIAL | S1B (message ordering) built; S1C (`message_source_units`) and S2 (`conversation_segments`) explicitly NOT built — this is the exact blocker for the founder's ChatGPT-export requirement (§13 below) |

## 5. Ingestion / large imports

| Requirement | Source | Status | Notes |
|---|---|---|---|
| ~2GB ChatGPT export must be uploadable and processed as a real background job | FR (this mandate, closing paragraph + §13-14) | PARTIAL | Durable job/worker substrate exists and is proven (`mainai_jobs`, V0.1-V0.3); large-file streaming upload exists (`docs/LARGE_FILE_UPLOAD_PLAN.md`, status per that doc's own PR-split — verify current merge state before assuming complete); ZIP hardening (nested/encrypted/zip-bomb) is built and tested (P2). What's missing: a ChatGPT-export-specific parser/importer (conversation/message/attachment structure → `messages`+`memory_source_units`) does not exist |
| Each individual message (user's and AI's) isolated but understood with surrounding context, linked across chats | FR (this mandate §13) | MISSING | Directly requires S1C+S2 (message-as-source, conversation segmentation) — both explicitly designed, explicitly not built. This is the single most direct code dependency between the founder's stated next priority and today's actual gap |
| Ingestion must be streaming/bounded/resumable/checkpointed/idempotent/restart-safe | FR (this mandate §14) | IMPLEMENTED (pattern) | This is exactly what the durable worker (PR #6) and `mainai_jobs` runtime already guarantee — proven via V0.2's 7 required crash-recovery demos. Applying it to a ChatGPT-export importer is "use the existing pattern," not "invent a new one" |
| If external AI is down, raw ingestion continues, semantic queue waits | FR (this mandate §14) | PARTIAL | The `awaiting_provider`/`blocked_provider` status split (`MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §4.7) is exactly this design, and P1 (provider verification) is built — but the full six-state status model's separation needs re-verification against the current `IndexStatus` enum (flagged UNCLEAR in Canonical Architecture §G) |
| Archive security: zip-slip, symlink escape, decompression bombs, nested archives, path collisions, huge files, millions of tiny files | FR (this mandate §15) | IMPLEMENTED | `app/rag/zip_import.py` — verified in `docs/KNOWLEDGE_IMPORT_SECURITY.md`, nested budget (P2, shared not per-level), magic bytes, `MAX_FILES`/byte caps, capacity-tested |

## 6. Goal/Dream/Dependency graph

| Requirement | Source | Status | Notes |
|---|---|---|---|
| Understand user goals that aren't currently achievable (e.g. "build a hotel," insufficient capital) without forgetting the goal; `BLOCKED_BY_RESOURCE` | FR (this mandate §16) | MISSING | No concept of blocked-but-remembered life goals exists. `mainai_goals`/`mainai_plans`/`mainai_tasks` are MainAI's own *engineering* work items (build a feature, fix a bug), not the founder's personal/life goals — these are two different graphs that happen to share naming, an important distinction to keep straight when building this |
| While blocked, identify other actionable work | FR (this mandate §16) | MISSING | No equivalent; MainAI's own task scheduler (temporal FIFO, V0.3-proven) is the nearest *mechanism* precedent but operates on engineering tasks, not life goals |

## 7. Learning / problem-solution memory

| Requirement | Source | Status | Notes |
|---|---|---|---|
| Learn from founder corrections, successes, failures, defects, experiments, outcomes | FR (this mandate §17) | PARTIAL | `engineering_lessons` (V0.1, extended through V0.3) is real, tested, durable, with conflict detection — but scoped to MainAI's own engineering work, not general life problem-solving |
| problem → attempted solution → result → verified solution → applicability → exceptions → lesson | FR (this mandate §17) | PARTIAL | `EngineeringLesson`'s schema (problem/root_cause/fix/general_rule, `applies_to` tags, severity/confidence) already matches this shape closely — extending its *scope* beyond engineering lessons is more plausible than building a parallel system |
| Reduce external AI dependency over time: local-first, escalate to external only when necessary, extract reusable knowledge from external answers | FR (this mandate §18) | PARTIAL | The pattern is stated as a goal in `MAINAI_ARCHITECTURE.md` §1's target design; no automatic "try local knowledge first, only then call external AI" routing exists in code today |

## 8. Founder memory / interaction model

| Requirement | Source | Status | Notes |
|---|---|---|---|
| Evidence-backed model of founder's communication/working-style preferences, explicit > inferred | FR (this mandate §19) | MISSING | See row in §4 above (`founder_memory_notes`) — same gap, same recommended foundation |

## 9. Founder HQ

| Requirement | Source | Status | Notes |
|---|---|---|---|
| Single control surface: system health, architecture, development, approvals, incidents, memory, capabilities, modules, infra, business, security, finances, roadmap | FR (this mandate §20) | MISSING | No unified surface exists. Real, working *pieces* exist independently: `/admin/jobs`, `/admin/agents`, `/admin/mainai-execution`, `/admin/memory`, `/library`, `/knowledge`, `/account` — Founder HQ is a genuine UI/IA consolidation project, not new backend capability, for most of its scope |

## 10. Business / Commerce

| Requirement | Source | Status | Notes |
|---|---||---|
| Business/Commerce workspace relates to Life Core, usable from HQ, later exposable as optional Life module | FR (this mandate §21) | MISSING | No code, schema, or design document relationship exists between this repo and any commerce/business system. `MAINAI_CONTEXT_BUNDLE.md`'s "My Money Master"/"4thepeople AB" are named as separate, unrelated products |

## 11. Prior roadmap items not superseded, still open

| Requirement | Source | Status | Notes |
|---|---|---|---|
| P4 — interpretation queue + relationship discovery + project map | AI (`MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §6.4) | MISSING (designed) | The largest remaining package in the pre-existing memory plan; blocks P5/P6/P7B |
| P7A/P7B — MainAI's own constitution (governance documents, two-gate activation) | AI (`MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §6.7) | MISSING (designed), P7A frozen per `docs/BRANCH_REGISTRY.md` | Directly relevant to this mandate's own docs (`LIFE_AI_INDEPENDENCE_CONSTITUTION.md`) — worth deliberately reconciling rather than building twice, see that document |
| S1C/S2/S3/S4/S5 (message-as-source, segmentation, generalized job queue, backfills) | AI (`MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §8) | MISSING (designed) | This is the exact, already-designed dependency chain for the ChatGPT-import requirement (§5/§13 above) — the founder's next priority already has a design waiting, not a blank page |
| Multi-tenant `Organization` entity | AI (`MAINAI_ARCHITECTURE.md` §4 target) | FUTURE, explicitly deferred | Explicitly not started "until a real second-tenant driver exists" — correct call, no reason to revisit given Life's single-founder framing |

## 12. Contradictions found (none blocking, all resolvable)

- **None of the founder's new mandate contradicts any existing document.** The closest thing to
  a tension: the founder's mandate frames "MainAI" as one layer among several (Life Core / Life
  App / Business Office / Domain Modules), while the existing codebase and every existing doc
  treats "MainAI" as effectively synonymous with the whole intelligence layer of the entire
  product. This isn't a real conflict — it's the founder's mandate being more precise about
  product structure than anything written before it — but it means `MainAI` as a name will
  eventually need to be understood as "the LifeAI capability that today happens to be the only
  thing built," not "the whole future intelligence layer," once Life App/Business Office
  materialize. No action required now; flagged so a future pass doesn't treat it as a surprise.
- **`app/rls.py`'s docstring says "the app connects as the table owner"** — already flagged as
  stale/incorrect in `MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §4.8's side-note (the app in fact
  connects as the non-owner `mainai_app` role). Not fixed in this pass (explicitly out of scope,
  no code changes this pass) — carried forward as a known, tiny, low-risk doc-only cleanup item.
