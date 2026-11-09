# Session-Start Context Assembly — Dependency-Verified Design (2026-11-09)

**Status: design only. No implementation, migration, or script is authorized or performed by this
document. Implementation remains deferred until prerequisites and an owner are confirmed.**
`first_cycle_go=false`.

This is Mission 2 of the 2026-11-09 GitHub-first directive. It **supersedes, as the active plan,**
the older Context Bundle design in `docs/FOUNDER_KNOWLEDGE_BOOTSTRAP.md` §3, which depends on a
`founder_vault/index.yaml` tree that **does not exist on default `64c07d5b`**. The directive is
explicit: *map the existing sources, determine the smallest safe way to assemble a versioned,
source-grounded session context from existing canonical data, and check whether an enforced
session-start contract can reuse current APIs/services instead of introducing a second Founder
Vault or memory system.* The answer below is: **yes — reuse, do not introduce a second store.**

---

## 1. Canonical sources that already exist (dependency-verified against `64c07d5b`)

Every function below was confirmed present in the default tree on 2026-11-09.

| Source | Module | Verified public API to read | What it contributes to session context |
|---|---|---|---|
| Founder memory | `app.founder_memory.service` | `list_current_founder_memory(db, owner_id)` (active-only), `list_founder_memory`, `get_founder_memory` | Decisions/corrections/preferences/goals the Founder actually stated (authority-tagged) |
| Inspectable memory | `app.inspectable_memory.service` | `list_inspectable_memory(...)`, `get_inspectable_memory_history(...)`, `list_truth_claim_violations(db, owner_id)` | Founder-facing projection over memory + **truth-claim receipts** (SAID/STORED checked vs reality) |
| Active context | `app.active_context.service` | `create_context_set(...)`, `refresh_context(...)`, `current_members(db, owner_id, context_set_id)`, `pin_object`/`suppress_object` | A **durable, owner-scoped, append-only "what matters right now" set** with anchor + members + events |
| Memory threads | `app.memory_threads.service` | `create_thread`/`add_member`/`add_relationship` | Links facts across tables without collapsing them (provenance-preserving) |
| Executive continuity | `app.mainai_executive.continuity` | `load_continuity_checkpoint(...)`, `build_checkpoint_from_cycle(...)`, `resume_summary(checkpoint)` | **Durable session checkpoints** + a ready-made resume summary from the last cycle |
| Capability reality | `app.capability_reality.service` | `list_capability_records`, `list_capability_gaps`, `get_capability_reality` | What is verified/planned/unknown — honest status for prioritization |
| Startup readiness | `app.mainai_startup_readiness` | `ReadinessLevel` (`BLOCKED`→`READY_FOR_SERIOUS_AUTONOMOUS_RUN`), `CheckStatus` | Machine-checkable "may I act, and how far" level |
| Project status (docs) | `LIFEAI_STATUS.md` (PR #331), this package | read-only | Narrative project-wide status for grounding |

**Conclusion of the mapping:** the durable, versioned substrate the older design wanted to build
(`founder_vault/` + `index.yaml` + checksums + a generator script) **already exists**, split across
`active_context` (the live, event-audited "current set"), `mainai_executive.continuity` (the
durable per-session checkpoint), and the memory/capability layers (the source-grounded facts).
Introducing a second Founder Vault or memory store would **duplicate** these and violate the
project's "do not build duplicate subsystems" rule.

---

## 2. Smallest safe design — a read-only assembler + a fail-closed contract

Two thin pieces, **both reusing the above; neither a new store**:

### 2.1 `SessionContextBundle` — a pure, read-only projection (no new table)
A read-only assembler (service function, not a script, not a migration) that, given
`(db, owner_id)`, returns an immutable, ordered, **source-attributed** bundle:

1. Readiness level (`mainai_startup_readiness`) — first, because it bounds everything else.
2. Last continuity checkpoint + `resume_summary(...)` (`mainai_executive.continuity`).
3. Current active-context members (`active_context.current_members(...)`), already pinned/curated.
4. Current founder memory (`list_current_founder_memory`) — active, authority-tagged.
5. Open truth-claim violations (`inspectable_memory.list_truth_claim_violations`) — so a session
   never acts on memory known to contradict reality.
6. Capability gaps (`capability_reality.list_capability_gaps`) — what is NOT verified.
7. Project-status pointer (`LIFEAI_STATUS.md` + this readiness package).

Every entry carries its **source module + row id + authority/basis** (reusing the existing
provenance vocabularies), so the bundle is *source-grounded by construction* — nothing is
synthesized or inferred, matching `founder_memory`/`capability_reality`'s "never infer" doctrine.

**Versioning comes for free from the sources**, not from a new checksum file: `active_context`
events are append-only and audited; `continuity` checkpoints are durable and timestamped;
`founder_memory` is append-only with supersession. The bundle records *which* checkpoint/
context-set/version it read, so it is reproducible without a parallel `index.yaml`.

### 2.2 Enforced session-start contract — reuse the existing fail-closed pattern
A session that cannot assemble a consistent bundle **must not act on the Founder's behalf** until
it can — the same fail-closed doctrine already used in `app/main.py`
(`_check_smtp_configured`/`_check_redis_reachable`) and expressed as levels by
`mainai_startup_readiness`. Concretely: assembly failure, or an unresolved truth-claim violation
touching a fact the session would rely on, downgrades the effective `ReadinessLevel` to `BLOCKED`.
**No new enforcement mechanism is introduced** — it is a new *caller* of the existing readiness
levels.

### 2.3 What is deliberately NOT built
- No `founder_vault/` tree, no `index.yaml`, no `scripts/build_context_bundle.py`, no checksum
  generator — all made redundant by §1.
- No second memory/knowledge store, no new migration (the assembler is pure reads).
- No automatic promotion of anything to `authority="founder"` (that remains a reviewed act).

---

## 3. Executable acceptance criteria (for whoever implements it later)

Written now so implementation can be verified against them without re-deriving intent. Each is a
testable assertion against a local Postgres with synthetic data (the project's standard harness).

1. **Source-grounded:** every item in an assembled bundle resolves to an existing row in one of
   the §1 sources (module + id); a synthetic assembler run asserts no item lacks a source ref.
2. **Active-only founder memory:** a superseded/disputed `founder_memory_notes` row never appears
   in the bundle (assert via a superseded note fixture).
3. **Truth-aware:** given a recorded truth-claim violation
   (`inspectable_memory.list_truth_claim_violations` non-empty), the bundle surfaces it AND the
   contract downgrades effective readiness to `BLOCKED` (assert both).
4. **Continuity reuse:** with a saved checkpoint, the bundle's resume section equals
   `resume_summary(load_continuity_checkpoint(...))` (assert equality — proves no parallel store).
5. **No new store:** the implementation adds no migration and writes to no new table; a schema-diff
   / `list_migrations` check shows zero new tables attributable to this feature.
6. **Owner isolation:** another owner's memory/context/continuity never leaks into a bundle
   (RLS/owner-scoped read test, same pattern as existing per-owner tests).
7. **Fail-closed:** if any source read raises, the assembler returns `BLOCKED` with the failing
   source named (never a partial bundle presented as complete).
8. **Deterministic ordering:** the bundle's section order is fixed (§2.1 order) so a session's
   starting context is reproducible.

---

## 4. Dependencies & deferral

| Prerequisite | State | Note |
|---|---|---|
| §1 source modules on default | **Present** (verified `64c07d5b`) | No merge needed to *read* them |
| An owner for the assembler | **Unconfirmed** | Deferred per directive ("owner confirmed") |
| First-run authorization | `first_cycle_go=false` | The *enforcement* only matters once a session acts for the Founder |
| `LIFEAI_STATUS.md` (PR #331) on default | Unmerged (PR #331 open) | Bundle can point to it; not a hard dep for the memory/context sections |

**Implementation remains deferred.** This document is the dependency-verified design + acceptance
criteria the directive asked for; it does not authorize writing the assembler. When an owner and
first-run prerequisites are confirmed, the assembler is a small, reviewed, read-only PR validated
against §3 — reusing existing services, introducing no second vault or memory system.

---

*Companion documents: `LIFE_OPERATIONAL_PRIORITY_2026-11-09.md`,*
*`LIFE_FIRST_START_READINESS_MATRIX_2026-11-09.md` (FS-4 points here).*
