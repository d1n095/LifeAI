# Security/authority test-quality audit — PRs #165-#187

Per the founder's night-run directive's third job: for each authority/security PR in this
range, "would this test still pass if the fix were removed?" — verified empirically where
practical (real git worktrees + real databases, not just diff reading), and via close code
reading with explicit reasoning where an empirical re-run wasn't the highest-value use of time.
Reused the three-check protocol (`docs/BRANCH_REGISTRY.md`'s own reference point, established
against PR #181) throughout.

**Method note**: "Not independently re-run" below means the claim was verified via direct code
reading (tracing the exact production function the test calls, confirming the assertions target
the changed behavior, and reasoning about what pre-fix code would have done) rather than an
actual worktree+DB re-run — not that it went unchecked. Every row was read in full diff, not
summarized from a PR body. Full per-PR rationale for #182-185 is on those PRs' own GitHub
comments (posted during this audit); this document is the consolidated summary.

| PR | Claimed invariant | Test exercises changed path? | Pre-fix negative control | False-confidence risk | Action |
|---|---|---|---|---|---|
| 165 | `execute_takeover()` must decline V0.1 dispatch for any EVER_GOVERNED goal (recovery never bypasses authority) | YES — calls `execute_takeover()` directly, asserts `new_job is None`, dead job `status=failed`, event sequence | Not independently re-run; strong static evidence (test's own docstring states the pre-fix failure mode, matches the code's actual gate placement) | LOW | none |
| 166 | `provider_spend_authorized` reflects live grant only; plan-derived capability ceiling enforced at Driver→Operator boundary | YES — `_require_capability`/`narrow_task_scope_from_accepted_development_plan` called directly | Not independently re-run | LOW | none — "empty ceiling permissive for legacy callers" is intentional, consistent with #177's later governed-only narrowing (verified prior session: exactly one production `OperatorContext` constructor, always governed) |
| 167 | First composed autonomous milestone; per-goal shared-worktree write authority verified via current active `supervisor_goal_lease` | Historical — PR body documents a real negative control against an earlier revision ("Job B overwrites Job A's marker", proven invalid, removed) | Not re-verified this session | LOW-MEDIUM | not independently re-checked this session; #187's clean audit (same production entry points) increases confidence indirectly |
| 168 | Completion gate must call canonical `record_final_report`, idempotent on replay, never overrides a cancelled goal | YES — `_finalize_task_outcome`/`record_final_report` called directly; idempotent-replay and cancelled-goal-not-overridden cases both present | Not independently re-run | LOW | none |
| 169 | Isolate migration-0061 downgrade probe to its own DB | test-infra only | N/A | N/A | out of scope |
| 170 | Egress default-deny gate + disclosure ledger foundation | Foundation PR; call-site tests live in 171-175 | covered indirectly via #171's empirical negative control | LOW | none |
| 171 | V1 — chat gated via `owner_id`; NEVER_EGRESS content denied before any provider call; retry after denial denied again | YES — real `client.post("/api/chat")`, provider-call tracked via monkeypatch (`chat_calls == []`, not just a status code) | **EMPIRICALLY RE-RUN**: all 3 relevant tests fail on pre-171 tip (`5a80310`), isolated worktree+DB | LOW — directly proven, not read | none |
| 172 | V2 (half) — document/media embedding gated via `embed_with_policy()` | YES — same non-invocation-tracking pattern as #171 | Not empirically re-run (same family as #171, which was) | LOW | none |
| 173 | Test-suite boot sequence mirrors production RLS/privilege steps | test-infra fix, not itself a security regression test | N/A | LOW | none |
| 174 | V2 (closes) — query embedding gated, graceful text-match fallback preserved | YES — 2 tests, same pattern, explicit degradation check | Not empirically re-run | LOW | none |
| 175 | V4 (partial) — 3 more `chat_with_fallback()` callers gated | YES — real production functions called directly | Not empirically re-run | LOW | none |
| 176 | `verification.py`/`transcription.py` swept, confirmed zero egress relevance | docs-only | **independently verified via direct grep** — probe text is literally `"ping"`; transcription always mock | LOW | none |
| 177 | Effect-time envelope authority + governed empty-capability fail-closed | YES (verified prior session) | **independently verified prior session** — exactly one production `OperatorContext` constructor, always governed | LOW | none |
| 178 | Two workers racing last-unit provider-spend budget: exactly one reserves | YES — own work, real 2-thread/2-connection race | **own negative control**: removed `.with_for_update()`, confirmed fail, restored, confirmed pass, clean diff | LOW | none |
| 179 | Two workers racing `supervisor_goal_lease` claim: exactly one wins | YES — same pattern | same discipline as 178 | LOW | none |
| 180 | Spend revoke racing reserve: revocation always wins | YES — same pattern | same discipline | LOW | none |
| 181 | Supervisor fail-fast on stale tick-start spend boolean (Outcome B — defense-in-depth, not the authoritative fence) | YES — **fully empirically verified**, `run_supervisor()`→`_invoke_live_gap()`→`_live_provider_spend_authorized()` genuinely exercised; extra bare-probe run beyond the test's own asserts confirmed non-vacuous failure | **CONFIRMED FAIL pre-fix (`77d3f1e`), CONFIRMED PASS post-fix (`fec0764`)**, both empirically run | LOW | none — verification comment posted |
| 182 | Refuse provider re-invoke after crash before spend settle (Window A) | YES — `reserve_provider_spend_call()`/`plan_with_provider()` exercised directly; confirmed single production `.propose()` call site | plausible from code read | **MEDIUM** — 2 real adjacent gaps found (not blocking): (1) "Window B" — any exception during `adapter.propose()`, including a client-side timeout, still releases the hold and permits a fresh-ref retry despite an unknown (not known-failed) outcome; (2) `preexisting`-row check is a plain SELECT before reserve resolves — two truly-simultaneous first-time callers could both read `None` and both proceed (no `threading.Barrier` test exists for this case) | tracked as follow-up on the PR; not overstated (PR's own test docstring correctly scopes Window B out) |
| 183 | Heal Operator write after crash before durable audit | YES — `write_file()` exercised directly, lease/cancel checks confirmed to still run before the heal block | negative control reproduced | **MEDIUM** — heal condition doesn't check `idempotency_key` or verify an incomplete prior audit exists; fires for ANY caller whose target content happens to already match on-disk content, silently succeeding instead of raising the before-hash mismatch for a genuinely stale-view caller. Not an authority bypass (on-disk state ends up correct), but broader than the PR's own "same idempotency key" claim | tracked as follow-up |
| 184 | Enforce job lease expiry at Operator write effect time | YES — `_require_context()` confirmed as the single choke point across all 5 mutating capabilities via grep; 6 read-only functions correctly excluded | plausible from code read | LOW-MEDIUM — real improvement; gap explicitly quantified as **non-zero** per the founder's own instruction: plain SELECT (no `FOR UPDATE`) on `mainai_jobs`, real wall-clock gap between check and write, no genuine two-thread test exists for this specific race | no revert needed; narrow window documented, not assumed away |
| 185 | Refuse future Operator effects after mid-flight founder cancel | YES — `refuse_if_cancelled=True` threaded to all 5 mutating entry points, correctly excluded from the read-only `checkpoint_operator_progress`; Driver between-steps re-check confirmed to call the real `_invoke_operator` | plausible from code read | LOW-MEDIUM — the founder's exact question ("is authority-check + filesystem mutation atomic, and how wide if not") answered precisely: **not atomic, same narrow non-zero window as #184** (identical choke point). Driver-level check closes the between-steps gap genuinely; the within-a-step gap is unclosed | no revert needed; residual window documented explicitly rather than implied zero |
| 186 | Out-of-scope plan paths fail closed as `CandidateValidationError`, not an uncaught tick abort | YES — exception type narrowed from bare `Exception` to `CandidateValidationError`, new out-of-scope `create_file` case added | not independently re-run; trivially decisive from the diff itself (unambiguous type narrowing) | LOW | none |
| 187 | Composed autonomy soak is genuinely production-shaped, no hidden bridges | YES — **fully verified via dedicated 6-pattern adversarial audit**; includes its own adversarial out-of-scope-path negative control; "later tick does nothing" confirmed as genuine re-invocation, not stale-state assertion | N/A (hidden-bridge audit, not a fix/no-fix question) | LOW | none — audit comment posted |

## Overall pattern

The Vault-gate family (#171/#172/#174/#175) and the concurrency-race family (#178/#179/#180)
share a consistent, deliberate test-writing discipline: track provider/adapter non-invocation
via a list append inside a monkeypatched wrapper (never just a status code), and use genuine
two-thread/two-connection races (`threading.Barrier`) rather than sequential same-session calls
wherever real concurrency is the actual claim. Everywhere this was spot-checked empirically
(#171, #178-181), it held up.

The two real *process* catches in this range were both caught **before** merge, by the founder's
own review, not after: #181's original version (regression test that passed without its own
fix — corrected via the Outcome B reframe, independently re-verified above) and #177's initial
breadth (narrowed to a governed-only conditional before merge). Post-merge, this audit found no
case of a test that merely restates an already-covered invariant while claiming to close a new
one. It did find two genuine, non-blocking follow-up gaps (#182 Window B / concurrent-first-call
race, #183's over-broad heal condition) and two correctly-quantified (not eliminated) TOCTOU
windows (#184, #185) — all four already posted as PR comments, none rising to "reopen this
PR"/"revert" severity.
