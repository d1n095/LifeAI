# Multi-Agent Coordination Under Concurrent Execution — `2308b697` (#314 lineage, Priority 10)

- **Subject:** the durable multi-agent coordination implementation (`app.agent_coordination`) on
  the #314 production lineage, product SHA `2308b697c6ffd4e792ffee5acccc4f4659d86389`, examined
  for **multi-instance / multi-process** behaviour — two independent service instances against
  one shared Postgres. Distinct from the single-service lifecycle / basic-restart suites.
- **Suite:** `probes/fc_coordconc_v1` — **3 passed, 4 xfailed** (`RESULTS_2308b69.txt`),
  deterministic (run 3×). DB-backed via the target's own `tests.conftest`; two independent
  Sessions on the shared engine and/or two `DurableCoordinationService` objects. Each xfail was
  verified via `--runxfail` to fail at its intended assertion (not setup). Loopback-only; no paid
  calls; no production mutation; target worktree unchanged. `first_cycle_go=false`.

## Verdict
The coordination substrate is **two layers with very different multi-instance safety**, and the
suite reports each honestly rather than calling the capability PASS:

- **DB-fenced, genuinely multi-instance-safe (PASS):** the **lease** layer (write-scope authority,
  recovery, takeover) — `FOR UPDATE`, per-assignment partial unique index, generation + staleness
  fencing (CC5); cross-instance **reads** and the status view read committed DB rows, not stale
  memory (CC6); service-level **owner isolation** holds (CC7).
- **Missing multi-instance boundaries (documented with deterministic reproductions, xfail):**
  concurrent duplicate **assignment** creation (CC1), concurrent **terminal transition** on one
  assignment (CC2), **stale whole-ledger snapshot overwrite** (CC3), and **per-process provider
  dedup** (CC4).

## Findings

| ID | Item(s) | Property | Result |
|---|---|---|---|
| CC1 | 2 | Two instances concurrently creating the same canonical work both commit — `create_work_assignment` is a check-then-insert with a NON-unique `canonical_work_key` index (migration 0046) | **XFAIL** (count=2) |
| CC2 | 3, 6 | Two instances racing a terminal transition (complete vs cancel) on one running assignment both commit and both record a terminal event — `transition_status` has no status-guarded UPDATE / version | **XFAIL** (events=2) |
| CC3 | 4 | A second instance that started without instance A's obligation overwrites the whole-ledger JSON snapshot — `persist_owner_ledger` has no optimistic-concurrency guard → lost update | **XFAIL** (reloaded kept only β) |
| CC4 | 5 | Provider-event dedup (`_seen_event_ids`/`_by_canonical`) is per-process, so two instances both act on the same `event_id` | **XFAIL** (r2=applied) |
| CC5 | 8, 9 | Lease takeover fences a stale holder (live lease can't be taken; after TTL expiry a new agent takes over, generation 1→2; old holder's renew is rejected; double-takeover with a stale `expected_generation` is rejected) | PASS |
| CC6 | 1, 12 | Two instances read the same committed state; a brand-new instance's per-assignment status view reflects a committed transition (not one process's stale memory) | PASS |
| CC7 | 11 | `work_registry_snapshot(owner=A)` returns only A's assignments, never B's, and vice-versa | PASS |

### Owner-isolation note (CC7)
Isolation is enforced **only** by the service's `WHERE owner_id = …` clauses — there is **no RLS**
on `agent_work_assignments` / `agent_scope_leases` (unlike the FORCE-RLS security-kernel tables).
The service contract holds (CC7 PASS), but the DB does not fence a caller that supplies or forges
the wrong `owner_id`. Informational boundary for the builders, not a defect of the tested contract.

## Items 7 and 10 (addressed, not re-tested)
- **Item 7 (STOP while multiple agents run):** STOP is enforced at the cycle/execution-authority
  layer (fc314 B1/C1 — `run_authorized_operating_cycle` halts at entry under STOP, spend blocked),
  not inside the low-level coordination ledger (`create_work_assignment`/`transition_status` do
  not consult the kill switch). That is the intended layering; no coordination-level STOP gate is
  claimed by the product, so none is asserted. STOP/epoch revocation durability is certified under
  the 460e8ad `fc_effauth`/`fc_crossauth` suites.
- **Item 10 (verification concurrent with reassignment):** follows from CC2 (an unguarded terminal
  transition such as `verified → completed` can race) plus CC5 (lease recovery is fenced); the
  verification-authority gate itself is certified under the 460e8ad independent-verification work.

## Responsible components & narrow remediation (builder-facing; examiner does NOT modify product)
- **CC1** — `create_work_assignment` + migration 0046: add a **partial unique index** on
  `(owner_id, canonical_work_key) WHERE status NOT IN (terminal) AND parallel_exploration_group_id
  IS NULL`; keep the app-level check as defense-in-depth.
- **CC2** — `transition_status`: guard the UPDATE on the **expected current status** (optimistic
  concurrency), or `SELECT … FOR UPDATE` the assignment row before transitioning, so exactly one
  terminal transition commits.
- **CC3** — `persist_owner_ledger`: **version** the snapshot (or move obligations/handoffs to
  per-row storage with their own constraints) and reject a stale-base write.
- **CC4** — provider/event dedup: back it with a **shared, transactional unique key** (unique
  index on the provider event id / `stable_event_id`), not per-process memory.

## Scope / discipline
Read-only examiner; drove the real `app.agent_coordination` service with two independent
sessions/instances via the target's own conftest. No paid calls; loopback only; no
publication/deployment/autonomy; no production branch modified; the already-documented #314
selector/broker defects are not re-litigated. Candidate materialised detached at the exact SHA
and left unchanged. `first_cycle_go=false`.

## Reproduce
```
probes/fc_coordconc_v1/run.sh ../../wtcert-2308b69               # 3 passed, 4 xfailed
probes/fc_coordconc_v1/run.sh ../../wtcert-2308b69 --runxfail    # forces the 4 to show their real assertion failures
```
