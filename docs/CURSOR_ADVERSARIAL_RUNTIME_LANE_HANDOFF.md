# Cursor adversarial runtime lane — final handoff (2026-08-20)

**Status:** `CURSOR ADVERSARIAL LANE COMPLETE` (handoff ready).  
**Not claimed:** `LIFE CONTROLLED AUTONOMY COMPLETE`.

This document freezes evidence for Claude/MainAI. Refresh SHAs against GitHub before acting —  
`YOUR LAST REPORT != CURRENT REALITY`.

---

## A. Exact integration SHA (at handoff write)

Refresh command: `gh api repos/d1n095/LifeAI/commits/claude/det-kommer-mer-879lcm --jq .sha`

At last refresh: **`8641ea8`** — `Skip conflict-candidate lessons at plan-time apply (#130)`.

Migration head on tip: **`0046_multi_agent_work_coordination`** (no Cursor PR in this lane added Alembic).

---

## B. Cursor PRs — drive to clean end

| PR | Branch | Intent | Notes |
|---|---|---|---|
| **#130** | `cursor/lesson-apply-skip-conflict-candidates` | Skip conflict-candidate lessons at apply | **MERGED** @ `8641ea8` |
| **#131** | `cursor/waiting-external-cancelable` | Cancel `waiting_external`; cancel ≠ clock | Update onto tip after #130; dual unit runs (flake + pass) |
| **#132** | `cursor/agent-scope-lease-expire-tick` | Stale lease expire + skip-stale conflicts | Update onto tip; units green on prior head |
| **#133** | `cursor/retain-after-storage-key-reference` | Retain after durable DB reference | Race test aligned with new semantics; update onto tip |
| **#134** | `cursor/verification-exhausted-record-lesson` | Exhausted structured verification → lesson | Based on tip; closes writer edge |

Dependency-safe merge order: **#131 → #132 → #133 → #134** (update only after tip moves).

Prior Cursor merges this lane (#115–#129): recovery strand, proposal dismiss, lesson paths, JobLock, plan_insertion lessons, media pause, cancel cascade, goal finalize, corpus retry, documents RLS, review verdict, agents base, capability finalize.

---

## C. Migrations

- Tip Alembic head: **0046**.
- #131–#134: **no new migrations**.
- Claude open PRs (#113/#110/#60): no file overlap with Cursor PR paths listed in §B (verified empty intersection on mainai_execution/worker/agent_coordination/library_import/references/project_memory/executor).

---

## D. Runtime capabilities proven (selected)

| Capability | Classification |
|---|---|
| ImportJob claim/lease/worker drain | VERIFIED_PRODUCTION_ACTIVE |
| MainAI `task_execution` → verify → finalize | VERIFIED_PRODUCTION_ACTIVE |
| `waiting_ci` + `MainAITaskWait` poll/resume | VERIFIED_PRODUCTION_ACTIVE |
| `retryable_failed` + `next_retry_at` worker tick | VERIFIED_PRODUCTION_ACTIVE |
| Lesson **apply** at `create_plan` / conflict tick | VERIFIED_PRODUCTION_REACHABLE |
| Lesson **writer** from exhausted structured verification | VERIFIED_PRODUCTION_REACHABLE once #134 merges |
| `AgentScopeLease` TTL expire tick | VERIFIED_PRODUCTION_REACHABLE once #132 merges |
| Retain-after-reference storage claim | VERIFIED_PRODUCTION_REACHABLE once #133 merges |
| Library upload → durable ImportJob | VERIFIED_PRODUCTION_ACTIVE |
| Documents `/api/documents/upload` indexing | MANUAL path uses BackgroundTasks — durability gap |

---

## E. Runtime edges still missing (cat.1 / EXPECTED_BUT_MISSING)

| Edge | Classification | Owner |
|---|---|---|
| eligible MainAI work → **Supervisor production entry** | IMPLEMENTED_BUT_UNCALLED | Cursor-discovered; wiring TBD (not Claude cognition) |
| lesson applied → **effectiveness feedback** | EXPECTED_BUT_MISSING | Future / MainAI learning |
| documents upload → **durable ImportJob** (survive process death) | EXPECTED_BUT_MISSING | Cursor follow-up OK after handoff if scheduled |
| child `waiting_*` → **goal status `waiting` rollup** | EXPECTED_BUT_MISSING | MainAI execution |
| `AgentTask` ↔ `MainAITask` auto-bridge | EXPECTED_BUT_MISSING / design | Dual planes; do not invent |

---

## F. Intentional / manual / future

| Edge | Classification |
|---|---|
| `waiting_external` cancel-only; no producer/poll | SCHEMA_AHEAD + INTENTIONALLY incomplete; cancel = escape hatch |
| `reviewed_approved` → `prepare_github_pr` via founder API | INTENTIONALLY_MANUAL |
| `attempt_auto_merge` always blocked | INTENTIONALLY_MANUAL / incomplete client |
| strategy_*/work_intelligence actuators | Mostly TEST_ONLY / future foundations (docs: no auto-route) |
| EngineeringLesson seed scripts | MANUAL |

---

## G. Claude-owned edges (Cursor read-only)

```
claims → interpretation → project_entities → justified knowledge → goal/work
```

Open Claude PRs (refresh): #113 founder-memory signal staging, #110 corpus-trial problem_learning, #60 architecture recovery (frozen design).

Cursor must **not** build a competing knowledge→goal bridge.

---

## H. Highest-risk unresolved defects

1. **Supervisor / Safe Planner / gap / driver** — full chain implemented, **zero** production router/worker callers → autonomy illusion. Severity: P0 autonomy. Safe to fix now only with explicit founder trigger design (not cognition bridge).
2. **Documents BackgroundTasks indexing** — process death loses work after Document commit. Severity: P1 durability. Safe narrow fix: ImportJob pattern.
3. **Goal status lie** — tasks `waiting_ci` while goal stays `running`; `MainAIGoalStatus.waiting` unwired. Severity: P2 honesty.
4. **Dual AgentTask / MainAITask** — approved agent work does not satisfy MainAI tasks. Severity: P1 architecture; do not invent semantics.
5. **Lesson effectiveness feedback** — writer (#134) + apply exist; “did lesson help?” missing. Severity: P2 learning loop.

---

## I. Attack criteria for Claude’s integrated chain

### Cognition bridge attacks

- unsupported inference → goal  
- contradictory / stale / superseded / duplicate evidence  
- insufficient authority / cross-owner evidence  
- inferred intent as explicit founder decision  
- goal without justified evidence  
- hidden manual translation  
- test-only bridge presented as runtime capability  

### Composed execution attacks

- duplicate workers / lease fencing  
- crash between job terminal and task finalize  
- stale leases without reaper (#132)  
- retry after side effect  
- missing capability at dispatch  
- provider failure honesty (pause vs fail)  
- approval missing  
- parent/child terminal divergence  

### Learning attacks

- exception/`str(exc)` must never become EngineeringLesson  
- broad `applies_to` must not over-quarantine (#130)  
- exhausted structured verification only (#134)

---

## J. Debugging / search / tool lessons

### Defect classes → search method that found them

| Defect class | Why earlier audits missed | Better formulation |
|---|---|---|
| State without driver | Grep existence of enum/helper | `Status.X` writers + readers + worker tick registration |
| Waiting without wake | Docs claim “wait” | producer → `MainAITaskWait` → poll → resume |
| Helper without caller | “Module looks complete” | `def f` → production callers excluding `tests/` |
| Retain before reference | Race tests passed with retain under lock | Crash after retain, before DB reference |
| Fire-and-forget | Fixed RLS on BackgroundTasks | `request commits → process dies → what retries?` |
| Dual CI conclusions | One run fail / one pass | Check **all** check-runs on commit SHA; flake ≠ product fail |
| Stale PR base | Assumed tip | Compare `baseRefOid` to tip SHA every time |

### Tool pitfalls

- `gh pr checks` “pending” while job already completed elsewhere on same PR (duplicate workflow runs).  
- `mergeStateStatus: UNSTABLE` with both FAILURE and SUCCESS on same check name → inspect per-run.  
- Detached worktree → `gh pr merge` “not on any branch” → run from branch worktree.  
- `EMPTY RESULT != PROOF OF ABSENCE` (rg truncated / wrong worktree tip).

### Heuristic for MainAI debugger

`CODE EXISTS != CAPABILITY EXISTS`  
`STATE EXISTS != DRIVER EXISTS`  
`API RESPONSE != COMPLETE REALITY`

---

## K. Areas not deeply traversed

- Full outbox/event-type inventory beyond jobs/waits/storage_deletion  
- Full RLS/IDOR matrix  
- Live GitHub write + CI with `github_write_enabled`  
- E2E corpus → claims → (Claude) → goals  
- intelligence_governance trees beyond adapter touchpoints  
- Recovery→lesson (only verification-exhausted path in #134)  
- Orphan blob GC beyond retain/reference  

---

## L. Explicit boundary

**Cursor has stopped writing Claude-owned architecture** (claims→interpretation→knowledge→goal).

Future Cursor role: adversarial re-validation after Claude’s composed implementation lands.

---

## Missing-runtime graph (revalidated intent)

```
eligible MainAI work → [MISSING production entry] → run_supervisor
run_supervisor → [PRESENT] → Safe Planner → driver → gap/verify
AgentTask → [NO auto bridge] → MainAITask
reviewed_approved → [MANUAL prepare_github_pr] → PR proposal/open
documents upload → [BackgroundTasks ONLY] → index_document
verification_failed structured + exhausted → [#134] → EngineeringLesson
EngineeringLesson → [PRESENT apply/conflict] → plan verification_plan
lesson applied → [MISSING] → effectiveness feedback
waiting_ci → [PRESENT poll/resume]
waiting_external → [NO producer; cancel escape only]
task waiting_* → [MISSING] → goal.status waiting rollup
strategy/work_intelligence → [mostly no actuator]
knowledge → [CLAUDE] → justified work
```

---

## Four maps (frozen snapshot)

### SYSTEM DEFECTS (this lane fixed / in flight)

Fixed #115–#130; in flight #131–#134 as above. Residual: Supervisor uncalled, documents BackgroundTasks, goal waiting rollup, AgentTask↔MainAITask, lesson effectiveness.

### AUTONOMY BLOCKERS

Supervisor entry; knowledge→goal (Claude); AgentTask sink; strategy actuators; waiting_external clock; documents durability.

### DEEPLY TRAVERSED

Worker ticks (import/mainai/CI/retry/recovery/replan/finalize/lessons); MainAI cancel/cascade; plan_insertion vs create_plan; library+media gates; JobLock; corpus review retry; documents RLS; agents review parse; capability finalize; lease TTL; retain/reference; verification→lesson writer design.

### NOT YET DEEPLY TRAVERSED

See §K.
