# Failure Recovery and Dynamic Agent Replacement — `2308b697` (#314 lineage, Priority 11)

- **Subject:** whether work can safely transfer from an unavailable provider to another
  authorized, capable agent — without losing provenance, duplicating effects, or bypassing
  delegation authorization — on the #314 production lineage, product SHA
  `2308b697c6ffd4e792ffee5acccc4f4659d86389`.
- **Suite:** `probes/fc_agentreplace_v1` — **5 passed, 1 xfailed** (`RESULTS_2308b69.txt`),
  deterministic (run 3×). DB-backed via the target's own `tests.conftest`; drives the real
  recovery primitives. Loopback-only; no paid calls; no production mutation; target worktree
  unchanged. `first_cycle_go=false`.

## Mechanism (established by reading the code)
There is **no assignment-level reassignment primitive**. The sole transfer mechanism is
`take_over_lease` (lease-level write authority). Dynamic replacement is **assembled** by the
orchestrator: (1) take over the stale lease, (2) stand up a successor assignment, (3) terminate
the source. The primitive performs only step 1. The already-documented #314 selector/broker
defects are **not** re-litigated here.

## Findings

| ID | Property | Result |
|---|---|---|
| AR1 | Recovery is vendor-neutral — a Codex agent takes over a stale lease a Cursor agent held; the Cursor holder is fenced. Recovery survives Cursor being unavailable; identity is not hardcoded to one vendor **at the lease/recovery layer** | PASS |
| AR2 | Recovery preserves provenance — `lease_taken_over` records old→new agent and old→new generation (auditable) | PASS |
| AR3 | Recovery does not duplicate work (single instance) — a recovered retry for the same canonical work is idempotent ("old completion cannot mint a second assignment") | PASS |
| AR4 | Delegation authorization is re-checked for a replacement — readiness is `AGENT_UNAVAILABLE` for an unavailable replacement and `AUTHORITY_CHANGED` when the goal's authorized instruction changed; both short-circuit before `LEASE_REQUIRED` (fail-closed) | PASS |
| AR5 | Recovery cannot widen write scope — takeover keeps the lease's original `allowed_paths`; a widening re-acquire is rejected (`ScopeExpansionError`) | PASS |
| AR6 | **Acceptance requirement** — `take_over_lease` transfers write authority but does not terminate the source assignment, and `running → superseded` is not allowed, so recovery alone leaves a live `running` orphan | **XFAIL** |

## Interpreted result
The recovery/replacement **primitive** (`take_over_lease`) is sound where it acts: it is
DB-fenced, vendor-neutral (AR1), auditable (AR2), scope-preserving (AR5), and the successor is
still gated by the authorization re-check (AR4). Single-instance de-duplication holds (AR3). The
gap is that **recovery is not atomic**: the primitive does not terminate the recovered-away source
(AR6), and the surrounding de-duplication that would make recovery safe **across instances** is
the per-process/whole-blob machinery already shown broken in Priority 10 (CC1, CC4). So on
`2308b697`, dynamic replacement is safe *only* when a single orchestrator instance correctly
assembles takeover + an authority-preserving successor + explicit source termination; the system
provides no single operation that guarantees this, and no cross-instance guarantee.

## Builder-facing acceptance requirements
- **R1 — atomic replacement:** one transaction that fences the source (lease takeover +
  generation bump), terminates the source assignment (a legal `running → superseded/failed`
  transition), and binds a successor that carries the **same** authorized authority
  (`authorized_instruction_sha256`) and a scope that is a **subset** of the source's.
- **R2 — re-authorize on replace:** the replacement path must run
  `evaluate_assignment_readiness` for the successor (AR4 shows the gates exist); recovery must
  never skip the `AUTHORITY_CHANGED` / `AGENT_UNAVAILABLE` gate.
- **R3 — vendor-neutral selection:** never hardcode the successor to one provider (depends on
  fixing the documented #314 selector-namespace defect so a real workforce `agent_key` can be
  assigned).
- **R4 — transactional, cross-instance effect de-duplication:** back recovery de-dup with a
  shared transactional key, not per-process memory (Priority 10 CC1/CC4).

## Scope / discipline
Read-only examiner; drove the real recovery primitives via the target's own conftest; the
documented #314 selector/broker defects are not re-litigated; no paid calls; loopback only; no
production branch modified; candidate detached at the exact SHA and left unchanged.
`first_cycle_go=false`.

## Reproduce
```
probes/fc_agentreplace_v1/run.sh ../../wtcert-2308b69               # 5 passed, 1 xfailed
probes/fc_agentreplace_v1/run.sh ../../wtcert-2308b69 --runxfail    # forces AR6 to show its assertion
```
