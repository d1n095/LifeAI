# MainAI V1 Readiness

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
| Task decomposition | **PROVEN** | `POST /api/mainai/goals/{id}/plan` → `propose_plan_via_ai()` → `create_plan()`, real AI-driven multi-task creation. | Confirm/add a max-task-count bound (Deliverable 2); verify idempotency and crash-recovery mid-decomposition (both flagged, unverified this pass). | BLOCKER for the idempotency check specifically; task-count bound is IMPORTANT POST-V1 unless the founder wants it sooner. |
| Authority derivation | **PROVEN** | `authorize_execution_scope()`, `grant_task_approval()`, `authorize_provider_spend()` — all real founder-facing routes; AI can only narrow, never widen (verified via #166's plan-scope-narrowing and #190's prompt-injection regression). | None. | — |
| Planning | **PROVEN** | `plan_with_provider()`/`plan_founder_request()`, real, extensively tested this session (#181, #196). | None for V1 scope. | — |
| Provider use | **PROVEN, with 2 tracked follow-ups** | Reservation/settlement/spend-authorization chain real and tested (#178-181, #196). | #182 Window B core closed (#196, this session); one non-blocking completeness note (`provider_request_may_have_left` never set by real adapters, conservative-safe default). | IMPORTANT POST-V1 (the completeness note); not a blocker. |
| Execution | **PROVEN** | `run_driver()`/Operator capability chain, extensively tested (#183-186, #191, #192). | Phase 3 (`_require_context()` freshness) still open per the correction-pass queue — see below. | BLOCKER until Phase 3 merges (queued, in progress as of this document). |
| Verification | **PROVEN** | `verification_evaluate` step, real `run_focused_test` integration, checkpointed. | None for V1 scope. | — |
| Repair | **PROVEN** | Full gap→repair→re-verification→unlock chain, see Part B above. | None — the `multiplication_repair` narrowness is a cost optimization, not a correctness gap. | — |
| Dependency progression | **PROVEN** | `_detect_cycle()`, `recompute_task_readiness()` (6 real callers). | None for V1 scope. | — |
| Provider-spend truth | **PROVEN** | Reservation/settlement/ledger chain, real two-thread concurrency tests (#178, #180, #196). | #182 Window B follow-up (concurrent-first-call edge, already tracked on #196's thread). | IMPORTANT POST-V1. |
| Egress/Vault | **PARTIAL** | V1-V3 closed (real default-deny gate + ledger), V4 mostly closed. V5 (classification schema), V8 (provenance linkage) have implementation-ready decisions now (`docs/LIFE_VAULT_V4_V5_V7_V8_DESIGN_MEMOS.md` + its Deliverable-7 addendum) but no schema built yet. | V5/V8 schema implementation — explicitly NOT required for MainAI V1's execution-autonomy scope (Vault classification governs external-provider disclosure of Life Vault content, a mostly-separate concern from the autonomous-development-loop V1 is about) — do not let this block V1 unless the founder wants it bundled. | OPTIONAL for V1 proper; IMPORTANT for the broader Life Vault initiative on its own timeline. |
| Restart recovery | **PARTIAL** | Recovery-authority chain proven for crash/takeover cases (#165, #177, #183, #191). Full process/session-boundary restart (Phase 5) not yet proven — #195 only proved the Python-object boundary. | Phase 5 (restart-soak v3) — queued, in progress. | BLOCKER until Phase 5 merges. |
| Lease takeover | **PROVEN** | `claim_supervisor_goal_lease()`, real two-thread concurrency test (#179), second-worker scope-narrowing proven (#191). | None. | — |
| Cancellation | **PARTIAL** | Cancel-after-ACCEPT, cancel-after-verify, cancel-vs-finalize all proven with real (if #194's sequenced-not-simultaneous caveat noted) tests (#192-194). Mid-decomposition cancellation NOT traced this pass (flagged in Deliverable 2). | Trace mid-decomposition cancellation; strengthen #194 to genuine unscheduled concurrency (Phase 4, queued). | BLOCKER until Phase 4 merges; mid-decomposition cancellation is IMPORTANT POST-V1 unless found to be a real gap. |
| Finalization | **PROVEN** | Canonical `record_final_report` chain (#168, #193), idempotent, respects prior cancellation. | None. | — |
| Idle/stopping | **PROVEN** | Confirmed via #187's/#195's "later tick does nothing" checks — genuine re-invocation, not stale-state assertion. | None. | — |
| Observability/audit | **PROVEN** | Checkpoints, `WorkTraceEvent`, `ProviderSpendUsageEvent`, `mainai_recovery_events`, `provider_disclosure_events` all real and append-only/RLS-protected. | None for V1 scope. | — |
| Self-improvement safety | **PARTIAL, design complete** | Contract fully specified in `docs/MAINAI_SELF_IMPROVEMENT_ACCEPTANCE.md` (this workstream). No run has actually been executed yet. | Execute the first bounded run per that contract, once Phases 1-5 close. | BLOCKER — this is the actual gating milestone, not a code gap. |

### V1 blocker summary (the short version)

**Real blockers, all already in motion**: Phases 1-5 of the correction pass (#182 Window B —
core closed via #196, follow-up tracked; #183 heal identity; Phase 3 `_require_context`
freshness; Phase 4 genuine cancel/finalize concurrency; Phase 5 restart-soak v3), plus the
task-decomposition idempotency/crash-recovery checks flagged in Deliverable 2, plus actually
running the first self-improvement milestone once those land.

**Not blockers, explicitly scoped out of V1**: Vault V5/V8 schema implementation (separate
initiative, its own timeline), the `multiplication_repair` recipe's narrowness (cost
optimization only), the provider-adapter `provider_request_may_have_left` completeness gap
(conservative-safe already).
