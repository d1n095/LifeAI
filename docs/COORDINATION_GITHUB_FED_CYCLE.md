# GitHub-fed authorized operating cycle (Phase 3)

The smallest coherent missing integration identified by the coordination certification (PR #337):
feed the authorized operating cycle's task selection from real, read-only GitHub intake.

Both halves already live on this branch (it descends from #303 and #314): read-only GitHub
intake + durable ledger backing (`github_intake.py`, `persist.py`; #300/#303) and the authorized
operating cycle (`autonomous_cycle.py`; #314). Before this change `autonomous_cycle` did not
consume `github_intake`, so Life's "next task" was not derived from observed GitHub state.

`agent_coordination/github_fed_cycle.py::run_github_fed_operating_cycle` composes the existing
PUBLIC primitives only — no new framework, no new STOP/spend/verification/messaging system:

    load_owner_ledger → GitHubDevelopmentIntake.refresh (read-only)
      → persist_owner_ledger (durable, DEF-1/GH-4-safe cold start)
      → load_owner_ledger → run_authorized_operating_cycle → founder_coordination_snapshot

So Life selects its next task from real observed PR / CI / review state instead of the Founder
hand-copying it between agents. Read-only (no GitHub writes, no delivery claims). A GitHub
outage or rate-limit surfaces as BLOCKED with a named missing capability and pauses ONLY GitHub
intake — the authorized cycle still runs. `first_cycle_go` stays false.

## Executed evidence (real Postgres)
`tests/backend/test_github_fed_operating_cycle.py` (3 passed):
1. End-to-end — read-only discovery of a reviewable PR → durable ledger → operating cycle →
   `needed_review` shows `independent_review` for that PR; fail-safe invariants intact.
2. Restart recovery + no duplicate assignment — discovery survives a no-client tick; repeated
   discovery keeps exactly one synthetic ledger-backing row.
3. Provider-unavailable — rate-limited reads → `github.blocked` with
   `missing_capability=github_read_rate_limit`, while the authorized cycle still runs.

## Status
Depends on #314 (and transitively #303/#300). **Independent review required — the author does
not certify this as approved.** No GitHub writes, no merge, no paid providers, no STRATO;
`first_cycle_go=false`.
