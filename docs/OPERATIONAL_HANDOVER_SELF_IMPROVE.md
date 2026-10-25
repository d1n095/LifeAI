# Operational handover and bounded self-improvement

Isolated child of frozen `#306` @ `2444bcc3da8ffa5ca10ef3295bac13aa045769c6`.
Does **not** amend `#309` (provider integration, under independent certification).

`first_cycle_go=false`. Gate 1 and Gate 2 remain **unpassed**.

## Shortest safe Gate 1 path (this PR)

Reuse founder-ops snapshot, durable ledger, workforce selector, spend grants, STOP,
autonomous gap, and engineering lessons. Add only the missing operating policy:

1. **Always-available interface** — `InteractiveIsolation` reserves Founder-facing
   work. Background queues have in-flight and depth limits. Backpressure rejects
   excess instead of starving chat/ops.
2. **Founder absence mode** — continue approved low-risk work; start new work only
   inside standing grants; recoverable retries are bounded; pause only the affected
   approval/restriction/high-risk item; silence is never approval; catch-up report
   on return.
3. **Observability** — snapshot slice `operational_handover` answers continue /
   paused / next Founder action / gate status from durable rows.
4. **Bounded self-improvement** — observe evidence-backed gaps, propose, refuse
   provider ideas as authority, never expand budget/STOP/protections, never deploy.
   Verified lessons persist on the existing ledger with `authorization_still_valid=false`.

Agent selection stays capability-based (`workforce/selector.py`). Providers are not
permanently assigned to job categories.

## Parallel large-file track

`cursor/large-file-background-workforce-91c1` implements the isolated import lane.
This PR does not raise upload limits or claim 5 GB.

## Gap table (from actual #306 code)

| Gap | Existing | Missing before this PR | Security | Owner | Founder action? |
|---|---|---|---|---|---|
| Interactive availability | Chat + founder-ops snapshot | No reserve/backpressure vs background | Founder auth | `mainai_operational_handover.interactive` | No |
| Absence / silence≠approval | Worker never silent-grants | No named absence policy or catch-up | STOP, spend, envelopes | `absence` | Yes to enable trial + standing grants |
| Workforce coordination | Selector, broker, ledger, receipts | Vendor round-trip | Bridge/API key | existing + `#309` | Yes for credentials |
| Self-improve | autonomous_gap, lessons | Named cycle that cannot deploy | Envelope | `self_improve` | Yes for review/disposition |
| Observability | snapshot questions | Absence/gates slice | — | snapshot | No |
| Large files | 60 MB stream upload | GB-safe worker + 5×5 GB measure | ZIP threat model | parallel PR | No until measured |
| Gate claims | `first_cycle_go=false` | Honest Gate 1/2 object | Codex STOP | `gates` | Yes to pass Gate 1 trial |

## Remaining Founder actions for Gate 1

1. Claude certification of `#306` / `#309` as applicable.
2. Real Cursor provider credentials + live-mutate grant.
3. Explicit unattended-cycle trial approval.
4. Standing low-risk grants and spend ceilings.
5. Keep `first_cycle_go=false` until both gates and authorization are satisfied.
