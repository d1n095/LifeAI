# Provider Spend Database Authority — `460e8ad8` (latest examined lineage)

- **Subject:** the PostgreSQL authorization boundary for provider spending on candidate head
  `460e8ad8cbd40b7fb0d62702f28c14412688e298`:
  - `provider_spend_authorizations` — Founder ceiling + `reserved_*`/`spent_*` counters,
    guarded by the BEFORE UPDATE/DELETE trigger `security_kernel_spend_guard`
    (migration 0095). Runtime keeps a column-scoped UPDATE grant on
    `(status, spent_*, reserved_*)` only; DELETE revoked.
  - `provider_spend_usage_events` — append-only ledger (migration 0060); runtime has
    INSERT + SELECT only; settle/release mutate it solely through SECURITY DEFINER
    functions under an internal GUC.
  - `settle_provider_spend_usage` / `release_provider_spend_usage` — SECURITY DEFINER; the
    only audited path that may lower a reservation hold.
- **Suite:** `probes/fc_spendauth_v1` — **10 passed** (`RESULTS_460e8ad.txt`). DB-backed via
  the target's own `tests.conftest` (real guard/triggers/grants/RLS/functions), loopback-only
  egress guard, no paid calls, no production mutation, target worktree unchanged.
  `first_cycle_go=false`.

## Scope / non-duplication
Does **not** re-run the previously completed SPEND-STOP gate (the fact that a STOP cascade
revokes spend authorizations — confirmed only as a precondition in SP5, not re-tested as the
finding) nor the provider-spend HTTP API test (`test_provider_spend_api.py`). None of the
spend-guard invariant messages exercised here is asserted anywhere in the product test tree
today (verified by grep across `tests/`), so this is genuinely unexamined DB-level surface.

## Examiner findings — all PASS

| ID | Property examined | Result |
|---|---|---|
| SP1 | Ceiling is a **DB backstop**: a direct runtime UPDATE pushing `spent+reserved` over `max_cost_usd` / `max_requests` is refused (`reservation exceeds Founder ceiling`), independent of the Python reserve checks | PASS |
| SP2 | Founder **bindings/ceilings are immutable** — dual layer: runtime is column-denied on `max_cost_usd` (no grant → permission denied); even the schema-owner writer is refused by the guard (`bindings and ceilings are immutable`) | PASS |
| SP3 | **Committed spend counters are monotonic**: `spent_*` may rise within ceiling but a decrease is refused (`committed spend counters cannot decrease`) | PASS |
| SP4 | **Runtime cannot directly release** a reservation (`runtime cannot directly release reserved spend; use the audited settlement function`); the SECURITY DEFINER `release_provider_spend_usage` path legitimately frees the hold | PASS — the privileged settlement path is the only release route |
| SP5 | After a **STOP** (which revokes the grant and emits **exactly one** `provider_spend_revoked` audit event), the runtime can neither resurrect the grant (`provider spend authority cannot be resurrected`) nor reserve against it via the service (`no live provider spend authorization`) — fail closed | PASS |
| SP6 | Spend authority + usage ledger are **append-only** — dual layer: runtime DELETE on the authority and UPDATE on the usage ledger are privilege-denied; the schema-owner writer is refused by the triggers (`spend authority history is immutable`; usage ledger `append-only`) outside the erasure/settle GUCs | PASS |
| SP7 | **RLS owner isolation**: an attacker authenticated as owner A cannot see or mutate owner B's grant — the targeted UPDATE matches **zero rows** and B's grant is unchanged | PASS — no cross-owner escalation via a forged `app.current_user_id` |
| SP8 | **Concurrent reservations cannot jointly exceed the ceiling**: two writers each add a hold that fits alone but not together; the authorization row lock serializes them and the guard refuses the overflowing one — exactly one commits, final `reserved ≤ max` | PASS — atomic under contention |
| SP9 | **Settle is atomic under rollback**: an aborted settle leaves the hold and usage row exactly as before (reserved intact, spent 0, usage `reserved`); a committed settle moves the hold `reserved → spent` and marks the usage row `settled` | PASS |
| SP10 | **Replay/idempotency**: a repeated reserve under one `source_ref` does not double-hold (`created=False`); settle is idempotent (no extra spend on re-settle); releasing an already-settled hold fails closed (`cannot release an already-settled`); re-using a released `source_ref` fails closed (`already released`) | PASS |

### Interpreted result
On its exact SHA, the provider-spend DB boundary holds along every axis the task named:

- **Runtime privilege escalation** — SP1 (ceiling backstop), SP2 (immutable ceilings), SP3
  (monotonic committed), SP4 (no direct release), SP6 (append-only). The runtime's
  column-scoped grant is a genuine least-privilege surface; the guard is the backstop even
  for a privileged writer.
- **Forged context** — SP7 (RLS cross-owner isolation). A forged owner GUC is only client
  input; it cannot reach another owner's grant.
- **Stale authority epochs / concurrent STOP / revocation** — SP5 (post-STOP the grant is
  revoked and cannot be resurrected or re-used; the resurrection fence is the DB-level
  control that matters, and it holds).
- **Replay** — SP10 (reserve/settle/release idempotency + fail-closed re-use).
- **Atomic reservation** — SP8 (row-lock-serialized ceiling enforcement under concurrency).
- **Rollback & audit consistency** — SP9 (settle atomicity), SP5 (exactly one audit event on
  the revocation transition).

**No defect found in the provider-spend database authority boundary on `460e8ad8`.**

### Boundary note (truthful, not a defect)
The spend guard enforces ceiling/monotonic/immutable/no-resurrect/no-runtime-release
invariants on the authorization row, but does **not** itself read the owner authority epoch;
post-STOP safety for spend rests on (a) the STOP cascade setting the grant `status='revoked'`
(a privileged AFTER-UPDATE trigger, SP5 precondition) and (b) the no-resurrection guard
(SP5). A compromised runtime with direct SQL and the column grant could still mutate
`reserved_*`/`spent_*` on an already-revoked grant **within the immutable ceiling** (the
counters are not frozen when `status='revoked'`), but this cannot cause a real external
provider call — the actual invoke is gated by the service's live-authorization + live-envelope
check (SP5) and by the SECURITY DEFINER settle path — so it is ledger noise bounded by the
Founder ceiling, not spend escalation. Recorded as the precise trust boundary, not a failing
requirement.

## Scope / discipline
Read-only examiner; drove the real guard, triggers, grants, RLS and SECURITY DEFINER
settle/release/reserve paths through the target's own conftest. No paid calls; loopback only;
no publication/deploy/autonomy. Candidate materialised detached at the exact SHA and left
unchanged. `first_cycle_go=false`.

## Reproduce
```
probes/fc_spendauth_v1/run.sh ../../wtcert-460e8ad      # 10 passed
```

## Next (continuation)
Priority 2 — Transport Database Authority (`mainai_push_grants` / `mainai_draft_pr_grants`):
effect-specific permissions, single-use consumption, stale grants, concurrent claims, runtime
role restrictions, and the relationship between committed authorization and remote effects.
Then Priority 3 — Recall security boundary (identity binding, revocation, erasure permissions,
protected-data isolation, fail-closed), with Recall kept inactive.
