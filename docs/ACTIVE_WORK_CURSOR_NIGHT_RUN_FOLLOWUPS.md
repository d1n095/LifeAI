# ACTIVE WORK — Cursor: close the night-run review follow-ups before expanding autonomy further

**Owner:** Cursor
**Handed off by:** Claude (independent red-team review), founder-approved
**Integration tip at handoff:** `claude/det-kommer-mer-879lcm` @ `ff07be8` (#191 merged)
**Depends:** none of these are blocked on each other except by the stated order below

## Why this doc exists

Claude's independent night review (`docs/SECURITY_TEST_QUALITY_AUDIT_165_187.md`) is complete.
It found **no test in #165–191 that passes without its own fix** — the architecture is sound —
but it found two concrete, non-blocking gaps on already-merged PRs (#182, #183), and two
already-known cancel-boundary items Cursor itself left open. **Do not reopen the proven
invariants in #188–191 without concrete contradictory evidence** — this doc is only about the
four items below.

## Required order

```
1. #182 Window B (provider crash — ambiguous invocation)
2. #183 heal/idempotency identity tightening
3. cancel after provider-plan / before Safe Planner effect
4. cancel after verify / before finalize
5. only then: process-restart autonomy soak
```

---

## 1. #182 provider crash Window B — ambiguous invocation must not be treated as "unused"

Core rule: **UNKNOWN EXTERNAL EFFECT != NO EXTERNAL EFFECT.**

Current gap (Claude's PR #182 comment): the `except Exception` handler around
`selected_adapter.propose()` in `app/provider_planning/service.py` releases the spend
reservation on **any** exception, including a client-side timeout where the provider may have
already received or even completed the request. Releasing + retrying (post-#182, this allocates
a fresh `:aN` source_ref) can re-invoke the provider for a call whose outcome is genuinely
unknown, not just known-failed.

Trace the exact try/except boundaries around: reservation → `adapter.propose()` → response
parse → settle/release. Classify failures into at least:

- **A. Provably pre-invoke** — no request could have left the process. Release may be safe.
- **B. Definitely invoked / response received** — settle truthfully (this path likely already
  works correctly for clean success/failure responses).
- **C. Ambiguous invocation** — a network/provider exception after the request may have crossed
  the process boundary. Do **not** release as if unspent. Do **not** automatically re-invoke.

Use the existing `ProviderSpendUsageStatus` state model if it already has room for this
distinction (e.g. a status that means "outcome unknown, held pending manual/founder
reconciliation") rather than inventing a new status unless genuinely necessary.

**Required regression** (negative control MUST fail on pre-fix behavior, matching every other
test this session):
1. reservation succeeds
2. adapter invocation begins
3. synthetic exception represents an ambiguous post-send failure (e.g. raised from inside a
   monkeypatched adapter, after it internally marks "request sent")
4. retry with the same `source_ref`

Expected: no second adapter invoke; the reservation is not silently treated as unused; spend
accounting stays conservative/truthful; the recovery disposition is deterministic (not "depends
on timing").

Also add a genuine **two-thread `threading.Barrier`** concurrency test for the first-reserve
race on this exact crash/retry `source_ref` path if one doesn't already exist — Claude's audit
found the current `preexisting`-row check is a plain SELECT before `reserve_provider_spend_call()`
resolves, with only a sequential (not real-concurrent) test covering it today.

---

## 2. #183 heal/idempotency identity tightening

Current heal semantics correctly detect "disk already equals requested after-hash → record
durable audit without rewriting." Claude's finding: this is **broader than it should be** — it
doesn't verify the heal belongs to the *same intended consequential operation*, only that the
bytes happen to match.

**Attack scenario**: Worker/action X writes content H to path P and crashes before audit.
Later, action Y — a **different** semantic operation, different `idempotency_key` — happens to
also request writing H to P. Y must NOT silently "heal" as if it had already executed X's work.

Audit the binding between: `idempotency_key`, job/task, path, requested after-hash,
capability/action, lease generation/current authority (see PR #191's already-merged test for
the authority half of this — that part is proven, don't re-litigate it).

**Required**: heal only when durable/current-operation identity proves equivalence, not merely
`disk content == after_hash`.

Add:
- same-operation positive control (the existing behavior should still work)
- different-idempotency-key negative control (the new case this PR closes)
- different task/job negative control, if structurally reachable
- re-run PR #191's takeover positive/negative controls and confirm they stay green — this
  change must not regress that already-proven property

Prefer the minimal identity-binding fix over a new broad schema addition.

---

## 3 & 4. Remaining founder-cancel boundaries (after 1 and 2 merge)

**A. Cancel after provider-plan complete, before Safe Planner/Driver effect.** Founder cancels
between "provider planning returned ACCEPTED" and the first actual `Operator` effect. Required:
zero future filesystem/remote effect from that point on — matches the existing `refuse_if_cancelled`
pattern from #185, applied to this specific window if it isn't already covered.

**B. Cancel after verify complete, before finalize.** Founder cancels after a task's
verification has already passed but before `_finalize_task_outcome`/`record_final_report`
commits. Required: the founder's cancellation must not be overwritten by a late success rollup
— a genuine race between the cancel write and the finalize write on the same goal/task row.

Then: a real **cancel-vs-finalize DB race** test (two-thread/real-concurrency, matching this
session's established gold standard from #178–180 — not a sequential same-session test).

---

## 5. Only after 1–4 merge: process-restart autonomy soak

Required invariant: **PROCESS MEMORY != AUTHORITY.**

- Persist goal/task/job/envelope/worktree/spend state (already durable by construction if 1–4
  are done correctly).
- Destroy the `Worker` instance entirely.
- Construct an **entirely fresh** `Worker`/runtime — no Python object may carry prior authority
  across this boundary.
- Continue from durable DB state only; current authority is rebuilt fresh (matches the pattern
  already verified for `OperatorContext`'s single production constructor — see
  `docs/SECURITY_TEST_QUALITY_AUDIT_165_187.md`'s attack-area-5 note).
- Complete remaining tasks, finalize, and prove later idle ticks do nothing (same "real
  re-invocation, not stale-state assertion" bar #187's soak test already met).

Use exact-head CI and negative controls throughout, same discipline as every PR this cycle.
Keep PRs narrow — one concern per PR, matching `CLAUDE.md`'s standing rule.
