# MainAI V1 Readiness

**Completion-run status (2026-08-30):** Cursor Stages 0A–0E and Stages 1–6 are **MERGED** on
`claude/det-kommer-mer-879lcm` (#196/#198/#199/#200/#201, #202, #203, #204, #205, #206, #207).
Evidence reports: `docs/MAINAI_LONG_AUTONOMY_RUN_REPORT.md`,
`docs/MAINAI_FIRST_SELF_IMPROVEMENT_RUN_REPORT.md`, `docs/MAINAI_GOAL_INTAKE_PATH_A_REPORT.md`,
plus the Stage 1–2 tests named in `docs/ACTIVE_WORK_CURSOR_MAINAI_V1_COMPLETION_RUN.md`. All
independently reviewed post-merge (Claude PR comments on #205/#206) — #205's self-improvement
run has one important caveat worth reading in the table below: it proved the mechanism safe
end-to-end but ran against a disposable worktree mirror, not the real checkout, so MainAI's
actual codebase did not change as a result.

The body below is Claude's own design-lane draft (PR #197), now merged forward past #207's
own landed-on-tip snapshot (which was itself adapted from an earlier version of this same
document) — this copy is the current one: includes four real bugs #197 found and fixed during
this session's V1 blocker sweep (`run_driver()` mid-run-takeover crash, an
`authorize_execution_scope()` TOCTOU race, an unbounded automatic-replan loop, and a real
approval-gate bypass via `POST /api/mainai/jobs`), all `git stash`-negative-control verified,
plus Part D below (a founder-defined Personal Intent & Executive Reasoning classification, with
its three companion architecture docs — also on this branch, so Part D's own references
resolve here, unlike #207's now-superseded trimmed copy). Path B execution-scope bridge code
remains #197's implementation lane — this doc tracks readiness, not that code drop.

---

Long-run authenticity spec (Deliverable 3), gap/repair production-loop audit (Deliverable 4),
and the full V1 blocker matrix (Deliverable 5). Companion to
`docs/MAINAI_V1_GOAL_TO_AUTONOMY.md` (goal intake / decomposition) and
`docs/MAINAI_SELF_IMPROVEMENT_ACCEPTANCE.md` (first self-improvement run's safety contract).
Every claim below has a file:line citation, gathered via dedicated read-only investigation
passes plus this session's own applied PR-review history — not aspiration.

---

## Part A — Long-Run Authenticity Spec

A rigorous, reusable acceptance spec for the 8-12 task self-directed autonomy run and every
composed-autonomy soak between now and then. Written from this session's actual, applied
review discipline across #167/#187 (clean) and #195 (real gap found) — every rule below was
already load-bearing in a real PR review this session, not derived from first principles.

### Core distinction: fake PROVIDER vs fake AUTHORITY

The one acceptable fake boundary in any soak/long-run test is the external PROVIDER call
itself (`RegistryPlanningAdapter`'s `.propose()`). Everything downstream of "the provider
responded with X" must run through real production code.

### ALLOWED

- **Fake provider adapter** — a test double for `.propose()`, returning scripted responses.
  `plan_with_provider()` itself — including its real spend-reservation fence — must remain
  untouched and genuinely execute.
- **Durable fixture repository** — a real git repo on disk the real Operator/Driver genuinely
  reads/writes. Pre-seeding initial content is fine.
- **Founder authorization setup** — calling `authorize_execution_scope()`,
  `authorize_provider_spend()`, `grant_task_approval()` directly in test setup, provided each
  call goes through the real function (not a bypass), and nothing later WIDENS what these
  established.
- **Fault injection** — a fake adapter error, a monkeypatched exception, discarding a Python
  object to simulate a crash — provided everything AFTER the fault is handled by real recovery
  code.

### FORBIDDEN

- **Test selecting the next task.** Selection belongs to `run_supervisor()`/
  `eligible_authorized_goals()`/readiness recomputation. A test may *drive* the tick loop but
  never itself pick which task advances.
- **Manual `PlanCandidate` construction after runtime starts.** Every candidate must originate
  from the real planner path, never hand-built and injected.
- **Manual task creation after runtime starts.** All `MainAITask` rows created once the
  experiment begins must come from real decomposition code, not `db.add(MainAITask(...))`.
- **Manual repair-task creation.** Must come from the real gap→repair mechanism (see Part B),
  never hand-inserted.
- **Manual status mutation.** No `task.status = X` from test code once the experiment begins,
  for any transition.
- **Manual dependency unlock.** A dependent task's readiness must be a real consequence of the
  upstream task's real completion.
- **Manual final report.** `record_final_report()` must be reached via the real completion
  gate, never called directly by test code.
- **Passing stale runtime authority objects across a restart.** No shared `Session`, no
  carried-over `OperatorContext`/`WorkBinding`/`PlanCandidate`/envelope/spend-authorization ORM
  instance may cross a restart boundary.
- **Bypassing Worker/Supervisor with direct Driver orchestration.** `run_driver()` must never
  be the harness's own top-level orchestration mechanism; the real entry point is
  `Worker.run_once()`.

### Machine-checkable evidence checklist

1. `grep -n "\.status = " <test_file>` — every hit outside a docstring is a red flag unless
   provably pre-"runtime starts."
2. `grep -n "PlanCandidate(" <test_file>` — every hit must be inside a fake adapter's response
   construction, never assigned directly into the harness's own driving code.
3. `grep -n "record_final_report\|_finalize_task_outcome" <test_file>` — any DIRECT call from
   test code (not a monkeypatch-wrapper used only to count invocations) is a red flag.
4. `grep -n "run_driver(" <test_file>` — confirm every call is a spy-wrapper around the real
   function (the `_spy_run_driver` pattern from #192), never the harness's own top-level
   replacement for `Worker.run_once()`.
5. For a restart step: `grep -n "Session(\|sessionmaker(\|superuser_db" <test_file>`, manually
   trace whether the same session object name appears both before and after the restart
   marker.
6. Confirm the audit trail (checkpoints, `WorkTraceEvent`, `ProviderSpendUsageEvent`,
   `mainai_recovery_events`) shows a plausible real progression matching the test's own
   narrative, not just an end-state assertion.

### Retroactive application

- **#167/#187** pass cleanly — independently re-verified this session against exactly these
  criteria.
- **#195** partially passes — the Worker-object-destruction proof is genuine, but the SAME
  session (`superuser_db`) drives both `worker_a` and `worker_b`, so the session-boundary half
  of "process memory != authority" is unproven. Any restart-soak v3 must close this specific
  gap.

---

## Part B — Gap/repair production-loop audit

Traced end to end: verification failure → gap evidence → `LifeProblem` → repair child task →
binding → approval/readiness → execution → source re-verification → downstream unlock.

**Headline finding**: this chain is substantially production-real, more mature than the
initial commissioning framing assumed. The one genuinely narrow/hardcoded piece is a single
deterministic fast-path recipe (`multiplication_repair`), not the loop's general mechanism.

| Link | Status | Evidence |
|---|---|---|
| 1. Verification failure detection | **Production-real** | Inside `run_supervisor()`, whenever `run_driver()` returns anything other than `"COMPLETE"` (including `VERIFICATION_REQUIRED`, `FAILED_NONRETRYABLE`, `CAPABILITY_MISSING`), `_invoke_live_gap()` is called unconditionally (`development_supervisor/service.py:1370-1385`). A second call site exists for `CAPABILITY_MISSING` during planning (`:1223`). |
| 2. Gap evidence | **Production-real** | `handle_live_gap_signal()` (`app/autonomous_gap/service.py:1098`) dispatches to `gap_from_verification_required()`/`gap_from_capability_missing()`, built generically from the actual failing task (`task.description`, `task.verification_plan`, `task.risk_level`) — not hardcoded. `record_gap()` (`:307`) writes the durable evidence row unconditionally. |
| 3. `LifeProblem` | **Production-real** | `record_gap()` creates it; `gap_problem_for_child_task()`/`is_gap_generated_child()` read it back. Model at `app/models/problem_learning.py:13`. |
| 4. Repair child task | **Production-real, one narrow piece** | `generate_child_task_for_gap()` (`:438`) → `propose_child_task_spec()` (`:422`) builds a generic `InsertedTaskSpec` from the gap — not hardcoded. `insert_plan_tasks()` is the only mutation path, lease-fenced via `require_live_gap_lease(..., for_update=True)`. See below for `multiplication_repair`. |
| 5. Binding | **Production-real** | `derive_work_binding_for_gap_child()` (`:834`), called from `_augment_bindings_with_gap_children()` at 4 separate real call sites inside `run_supervisor()`. Intersects `allowed_paths` with the CURRENT `scope.allowed_paths` (re-derived, not copied stale from gap-creation time). |
| 6. Approval/readiness | **Production-real, deliberately different gate** | `propose_child_task_spec()` sets `approval_required=False`, but `assess_gap_authority()` (`:368`) is a thorough, fresh, deterministic re-check on every gap: stale-instruction-hash detection for founder corrections, repository/path/capability subset checks, a **hard** risk ceiling (`risk_level == high` always forces `EXTERNAL_REVIEW_REQUIRED` regardless of envelope), unresolved-ambiguity checks. Substitutes "stays strictly inside already-granted authority, reverified live" for "needs a new human click." |
| 7. Execution | **Production-real** | Repair tasks flow through the identical `WorkBinding`/`Worker`/`Supervisor`/`Driver` path as ordinary tasks — no special-cased execution branch. |
| 8/9. Source re-verification & downstream unlock | **Production-real** | `resume_source_after_repair()` (`:897`), called from `development_supervisor/service.py:1437` immediately after a gap-child task completes (guarded by `is_gap_generated_child()`). No separate re-verification mechanism exists or is needed — the original source task simply resets to `ready` and is naturally re-selected, re-running its own verification through ordinary execution. |

### The `multiplication_repair` question, resolved precisely

`_structured_repair_recipe_for_gap()` (`:636`) — its own docstring states verbatim:
*"`multiplication_repair` proves the live WorkBinding/PlanCandidate handoff for the bounded
calculator fixture — it is NOT general arbitrary-code repair."* It returns this recipe **only**
if the goal's `original_instruction` contains "calculator" AND ("multiplication"/"multiply"),
AND `allowed_paths` includes exactly `calculator.py`+`test_calculator.py`. **No other named
recipe exists anywhere in the codebase** (confirmed via grep). For every other real-world gap,
`recipe_candidate` stays `None` and `plan_founder_request(..., candidate=None)` falls through
to provider-assisted planning (`development_supervisor/service.py:1126-1136`) — **the loop
does generalize**, it just needs a live provider call for anything beyond this one demo
shortcut. This is not a blocker: it means gap/repair works end-to-end for arbitrary gaps
provided provider assistance is authorized; `multiplication_repair` is purely a
zero-provider-cost fast path for one test scenario, not a limitation on the general mechanism.

### Smallest architecture delta needed

**None identified as a blocker.** The chain is already production-shaped end to end. The one
worthwhile (non-blocking) addition: a SECOND named deterministic recipe covering a slightly
more general class of gap (e.g. "add a missing focused test for an already-passing function"),
to reduce first-real-repair reliance on live provider calls — not required for V1, a nice-to-have
for cost/latency, not a correctness gap.

---

## Part C — V1 Blocker Map

STATUS legend: **PROVEN** (empirically verified, real production path, tested this session or
earlier with a genuine negative control) / **PARTIAL** (production path exists but a specific
sub-case is unverified or a known gap remains) / **UNPROVEN** (mechanism likely exists but has
not been empirically checked) / **BLOCKED** (a real, open gap stands in the way).

| Category | Status | Evidence (production path + test/PR) | Remaining work | V1 classification |
|---|---|---|---|---|
| Goal intake | **PROVEN** | Two real HTTP routes (`POST /api/mainai/goals`, `WorkCandidate` authorization path) → `create_goal()`. See `docs/MAINAI_V1_GOAL_TO_AUTONOMY.md`. | None. | — |
| Task decomposition | **PROVEN, one bug fixed this session** | `POST /api/mainai/goals/{id}/plan` → `propose_plan_via_ai()` → `create_plan()`, real AI-driven multi-task creation. Fully atomic (single transaction, confirmed by direct trace — no reachable partial-decomposition state). Concurrent-decomposition race (unhandled `IntegrityError` on the loser) found, fixed (goal-row lock + `populate_existing=True`), and regression-tested this session (`test_two_genuinely_concurrent_create_plan_calls_for_the_same_goal_never_raise_unhandled_integrity_error`). | Confirm/add a max-task-count bound (Deliverable 2) — a nice-to-have, not urgent; no cancellation check inside decomposition itself (narrow exposure window, single synchronous request). | IMPORTANT POST-V1 for both remaining items; neither is a blocker. |
| Authority derivation | **PROVEN, two real gaps found and closed this session** | `authorize_execution_scope()`, `grant_task_approval()`, `authorize_provider_spend()` — all real founder-facing routes; AI can only narrow, never widen (verified via #166's plan-scope-narrowing and #190's prompt-injection regression). Path B (direct `POST /api/mainai/execution/goals`) previously had no route to create an execution-scope proposal at all (found via adversarial re-verification, `docs/MAINAI_V1_GOAL_TO_AUTONOMY.md`'s gap #0) — **closed this session**: new founder-invoked bridge route (`POST /api/execution-envelopes/goals/{goal_id}/propose`), full production-shaped E2E proof plus 4 negative tests (`test_path_b_execution_scope_bridge.py`), and a related concurrent-proposal-creation race found and fixed in the same underlying function (`propose_execution_scope()`, `INSERT ... ON CONFLICT DO NOTHING`, two-thread regression test). **Second gap, front-half V1 blocker sweep**: `require_task_approval()` was checked ONLY inside `dispatch_ready_task()` — the generic, founder-gated `POST /api/mainai/jobs` route calls `app.jobs.service.create_job()` directly with `job_type="task_execution"`, a real, live, entirely separate door to a durable `mainai_jobs` row that never called `dispatch_ready_task()` and therefore never checked approval at all. Closed: `_validate_task_execution_input_refs()` (the ONE check that door's own call path gets) now also calls `require_task_approval()`, raising `InvalidInputRefsError` if unapproved — three-check verified (`git stash` confirms the pre-fix call succeeds silently with zero approval check; post-fix it's refused). | None. | — |
| Planning | **PROVEN** | `plan_with_provider()`/`plan_founder_request()`, real, extensively tested this session (#181, #196). | None for V1 scope. | — |
| Provider use | **PROVEN, with 2 tracked follow-ups** | Reservation/settlement/spend-authorization chain real and tested (#178-181, #196). | #182 Window B core closed (#196, this session); one non-blocking completeness note (`provider_request_may_have_left` never set by real adapters, conservative-safe default). | IMPORTANT POST-V1 (the completeness note); not a blocker. |
| Execution | **PROVEN** | `run_driver()`/Operator capability chain, extensively tested (#183-186, #191, #192). Phase 3 (`_require_context()` freshness) closed via #199, independently verified this session. | None. | — |
| Verification | **PROVEN** | `verification_evaluate` step, real `run_focused_test` integration, checkpointed. | None for V1 scope. | — |
| Repair | **PROVEN** | Full gap→repair→re-verification→unlock chain, see Part B above. | None — the `multiplication_repair` narrowness is a cost optimization, not a correctness gap. | — |
| Dependency progression | **PROVEN** | `_detect_cycle()`, `recompute_task_readiness()` (6 real callers). | None for V1 scope. | — |
| Provider-spend truth | **PROVEN** | Reservation/settlement/ledger chain, real two-thread concurrency tests (#178, #180, #196). | #182 Window B follow-up (concurrent-first-call edge, already tracked on #196's thread). | IMPORTANT POST-V1. |
| Egress/Vault | **PARTIAL** | V1-V3 closed (real default-deny gate + ledger), V4 mostly closed. V5 (classification schema), V8 (provenance linkage) have implementation-ready decisions now (`docs/LIFE_VAULT_V4_V5_V7_V8_DESIGN_MEMOS.md` + its Deliverable-7 addendum) but no schema built yet. | V5/V8 schema implementation — explicitly NOT required for MainAI V1's execution-autonomy scope (Vault classification governs external-provider disclosure of Life Vault content, a mostly-separate concern from the autonomous-development-loop V1 is about) — do not let this block V1 unless the founder wants it bundled. | OPTIONAL for V1 proper; IMPORTANT for the broader Life Vault initiative on its own timeline. |
| Restart recovery | **PROVEN** | Restart-soak v3 (Phase 5) merged and independently verified this session (#201) — genuine NEW session + NEW Worker process boundary, not just the Python-object boundary #195 proved. | None. | — |
| Lease takeover | **PROVEN, hardened further this session** | `claim_supervisor_goal_lease()`, real two-thread concurrency test (#179), second-worker scope-narrowing proven (#191). Stage 2 (#203, merged) added real `supervisor_goal_leases` expiry → reclaim → zero-FS-effect → goal-continuation proof. This session additionally found and closed a real observability gap: `run_driver()` previously crashed uncaught on a mid-run authority transition instead of a clean `DriverResult` — fixed via `OperatorAuthorityTransitionError` + a `STALE_AUTHORITY` classification, with an empirical proof that the winning worker's next tick resumes cleanly from checkpoint. Also found and closed a genuine first-governance TOCTOU race in `authorize_execution_scope()` (docstring claimed a goal-row lock the code never took) — see the branch registry's own entry on PR #197 for both fixes' full detail. | None outstanding from this pass. | — |
| Cancellation | **PROVEN** | Cancel-after-ACCEPT, cancel-after-verify, cancel-vs-finalize all proven; Phase 4 (#200, merged) closed the genuine unscheduled-concurrency gap #194 left open (40-trial real-race regression). Mid-decomposition cancellation remains a real but narrow-window gap (single synchronous HTTP request, not a multi-tick loop). | Mid-decomposition cancellation check — still open, still narrow. | IMPORTANT POST-V1 (confirmed real but narrow, not urgent). |
| Finalization | **PROVEN** | Canonical `record_final_report` chain (#168, #193), idempotent, respects prior cancellation. | None. | — |
| Idle/stopping | **PROVEN** | Confirmed via #187's/#195's "later tick does nothing" checks — genuine re-invocation, not stale-state assertion. | None. | — |
| Observability/audit | **PROVEN** | Checkpoints, `WorkTraceEvent`, `ProviderSpendUsageEvent`, `mainai_recovery_events`, `provider_disclosure_events` all real and append-only/RLS-protected. | None for V1 scope. | — |
| Self-improvement safety | **PROVEN, mechanism-only — see caveat** | First bounded run executed and merged (#205, "Stage 4"): real Worker→Supervisor spine, narrow envelope (`authorized_paths` = one test file, no `delete_file`/commit/push capabilities), `remote_write_authorized=False` asserted, independently reviewed post-merge (Claude, PR #205 comment). | **Important caveat, not a blocker, but must not be misread**: the run edited a disposable worktree mirror seeded to match production, NOT the real checkout — production `backend/tests/backend/mainai/test_egress_policy.py` is unchanged by this run. This PROVES the mechanism is safe end-to-end; it does NOT mean MainAI's real codebase actually improved yet. Applying the same diff to production remains a separate, still-open action. See `docs/MAINAI_INSPECTABLE_MEMORY_CONTRACT.md` for why this is exactly the SAID-vs-VERIFIED distinction that document exists to make explicit. | — (mechanism proven; whether/when to run milestone 2 against a real checkout is a founder decision, not a V1 gap). |

### V1 blocker summary (the short version)

**Correction-pass Phases 1-5 all closed and independently verified**: #196/#198/#199/#200/#201.
Stage 1-3 of the MAINAI V1 COMPLETION RUN (Cursor) also merged: #202 (gap/repair live loop),
#203 (Stage 2 lease-expiry takeover), #204 (Stage 3 long-run soak). Task-decomposition
idempotency/crash-recovery/Path-B-authority questions flagged in Deliverable 2 are all resolved
(concurrent-decomposition race fixed, atomicity confirmed, Path B bridge built and proven).
This session additionally found and closed four further real gaps beyond what any Cursor
Stage PR covered: the `run_driver()` mid-run-takeover crash (`OperatorAuthorityTransitionError`),
the `authorize_execution_scope()` first-governance TOCTOU race, an unbounded automatic
replan loop (`MAX_AUTO_REPLANS`), and a real approval-gate bypass (`POST /api/mainai/jobs`
could dispatch an approval-required task with zero approval check, via a second door
`dispatch_ready_task()`'s own gate never covered) — all empirically proven via `git stash`
negative controls, see PR #197. Stage 4 (#205, first bounded self-improvement) and Stage 5 (#206, Path A goal
intake) have also since merged and been independently reviewed (Claude, both PR comments) —
**no open code blockers remain for V1's execution-autonomy scope.** The one item worth a
founder decision, not a code gap: #205 proved the self-improvement mechanism is safe but did
not actually change MainAI's real codebase (disposable-mirror caveat, table above) — whether a
milestone-2 run against the real checkout happens next is a founder call, not something this
readiness matrix blocks on.

**Not blockers, explicitly scoped out of V1**: Vault V5/V8 schema implementation (separate
initiative, its own timeline), the `multiplication_repair` recipe's narrowness (cost
optimization only), the provider-adapter `provider_request_may_have_left` completeness gap
(conservative-safe already), and the entire new Personal Intent & Executive Reasoning /
Long-Horizon Planning / Memory Truth workstream — see Part D below for why and for the one
piece of it (memory truth) that IS required now regardless.

---

## Part D — Personal Intent & Executive Reasoning: V1 classification

Founder-defined new capability (2026-08-30), specified in three companion documents:
`docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md`, `docs/MAINAI_INSPECTABLE_MEMORY_
CONTRACT.md`, `docs/MAINAI_LONG_HORIZON_PLANNING.md`. Per the founder's own explicit
instruction, this is **not** automatically a V1 blocker — V1 is about the execution-autonomy
chain (Parts A-C above) being safe and complete; this workstream is about reducing founder
instruction-effort over time, a genuinely separate capability axis layered on top of an
already-safe V1.

**V1 minimum required from this workstream: none.** Every mechanism specified across all three
documents is additive to the already-shipped goal→plan→execute→verify→finalize chain — none
of it is required for that chain to be correct or complete.

**Required NOW regardless of V1/V1.1/V2 sequencing** — the founder's own explicit exception:
the **memory truth invariant** (`docs/MAINAI_INSPECTABLE_MEMORY_CONTRACT.md` §2's
`MemoryTruthState` vocabulary and §5.1's "build claims from returned rows, never from
requests" calling convention). Reasoning, restated from that document: every producer this
workstream will build (resolved references, generated candidates, conversational lessons)
needs somewhere truthful to land before it's built, or the very first thing built to reduce
founder effort recreates the exact SAID/STORED confusion the founder is asking to prevent.
This is cheap to establish now (a vocabulary + a calling convention, not new infrastructure)
and expensive to retrofit once call sites exist that don't follow it. Tonight's own four real
bugs (§ the readiness table's "run_driver() mid-run-takeover", "authorize_execution_scope()
TOCTOU", two test-fixture fixes, and the unbounded-replan loop) are concrete, already-happened
instances of exactly this failure class, in code that predates this document — not a
speculative risk.

**V1.1** (see each document's own §8/§7/§6 for full reasoning, summarized here):
- `ConversationalInterpretationProposal` + resolution flow (personal-intent doc §1) — reuses
  `project_entities`' proven pattern verbatim, no new authority surface.
- Conversational lesson recording (personal-intent doc §5) — one new caller into an
  already-fully-built `EngineeringLesson` system.
- `WorkCandidate.priority` NOW/NEAR/LATER/OPTIONAL/BLOCKED vocabulary widening (personal-intent
  doc §2.3 / long-horizon doc §4.1) — a CHECK constraint change, cheap and additive.
- Additional replan trigger sources beyond task failure (long-horizon doc §3.2) — reuses the
  existing, tested `trigger_replan()` mechanism unchanged.
- NEAR-radius executive scan (long-horizon doc §4).

**V2:**
- Full executive look-around orchestrator (personal-intent doc §2) — real integration work
  across three subsystems, needs its own bounded-generation tuning before trustworthy at scale.
- `authority_kind` generalization out of `safe_planner` into a shared classifier (personal-
  intent doc §3) — a real refactor of a working module, not just a new caller.
- MID/LONG-radius horizon scanning (long-horizon doc §4) — new multi-hop traversal logic,
  needs correctness/performance validation before running automatically.
- Inspectable-memory UI surface and background verification job scheduling (memory-contract
  doc §6.3/§5.2) — the `MemoryTruthState` vocabulary itself ships now (see above); the UI and
  automated verification around it are deferrable.

**Standing invariant this workstream must never weaken, at any tier**: LONG-horizon items and
executive-scan-generated `WorkCandidate` rows carry exactly zero authority — every path from
this workstream's own output to a real effect still passes through the SAME
`authorize_execution_scope()`/`grant_task_approval()`/`authorize_work_candidate()` gates Parts
A-C already prove are real and tested. Nothing in Part D introduces a new way to acquire
execution authority.
