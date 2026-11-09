# Life — First Operational Integration Map (2026-11-09)

**Status: documentation only. No merge/deploy/activation. `first_cycle_go=false`.** Base of
record: default `claude/det-kommer-mer-879lcm` @ `64c07d5b`; PR states via GitHub API 2026-11-09.

This is Mission 4 of the GitHub-first directive, written **after** the P1 security work it was
gated on (PR #374, DB-role password; and PR #371/#372). It identifies the **missing end-to-end
integration** for the operational chain, **reusing existing components and active PRs — no new
product architecture**. It is the forward-looking companion of
`docs/LIFE_FIRST_START_READINESS_MATRIX_2026-11-09.md` (which inventories readiness); this doc
names, hop by hop, the *one integration* each hop still needs and classifies it.

## 1. The chain (from the directive)

```
read authorized project sources → understand implementation status → resolve dependencies →
select feasible next work → communicate with a real authorized agent → receive usable results →
correlate CI and independent review → persist continuity → explain next steps
```

## 2. Integration map — existing component, active PR, the missing integration

Gap kind: **ACTIVATION** (built, needs merge + Founder authorization) · **CODE(bounded)** (a
small, reviewed increment on an existing module) · **DESIGN** (design exists, build deferred) ·
**DECISION** (Founder/operational, not code).

| Hop | Existing component (reuse) | Active PR(s) | The missing integration | Gap kind |
|---|---|---|---|---|
| Read authorized sources | `app.rag`, `app.storage`, `app.concept_reconciliation` | #327/#329/#330, #369 (importer) | None structural; importer breadth (#369) is future | ACTIVATION |
| Understand implementation status | `app.capability_reality` (0048) + this readiness package | — | Record classifications **as capability records** once a run is authorized | ACTIVATION |
| Resolve dependencies | `app.life_intents` (0041), `docs/BRANCH_REGISTRY.md` (manual) | — | Automated cross-PR dependency reasoning (today manual) | CODE(bounded)/future |
| Select feasible next work | `app.work_candidates`, `app.safe_planner`, `app.mainai_executive.priority`/`missing_piece` | — | Wire selection into the composed loop | ACTIVATION |
| **Communicate with a real authorized agent** | `app.agent_coordination` (adapters/dispatch/routing) | #338 → #339 | **Real agent runtime** beyond single-shot `LocalCLIAdapter` (it returns output *refs*, not results) + merge #338→#339 + provider/workforce activation | **CODE(bounded)** + ACTIVATION |
| **Receive usable results** | `app.mainai_execution` (executor/verify/approval), `app.workforce.verification` | #335 → #336 | **Usable-result capture** (depends on the runtime gap above) + model-backed workers merge | **CODE(bounded)** + ACTIVATION |
| **Correlate CI and independent review** | `app.integrations.github_client`, `app.mainai_startup_readiness`, review lanes (#367) | #338 | **GitHub read-surface** (list branches / open-PRs / PR-reviews / workflow-runs) + a correlation step that joins check-runs ↔ review state ↔ readiness | **CODE(bounded)** |
| Persist continuity | `app.mainai_executive.continuity`, `app.founder_memory`, `app.memory_threads` | — | Session-start context assembler (reuses these; **design-only**, see assembly design doc) | DESIGN |
| Explain next steps | `app.mainai_executive.why_graph`/`priority`, `app.safe_planner` | — | Wire explanation output into the loop | ACTIVATION |

## 3. The single missing integration (critical path)

Everything above is either activation of built foundations or one of **three bounded, reviewed
code increments** — none is new architecture:

1. **GitHub read-surface** on `app.integrations.github_client` (FS-9): add
   list-branches / list-open-PRs / list-PR-reviews / list-workflow-runs so Life can *correlate CI
   and independent review* and *track activity* — the current client is write/targeted-read only.
2. **Usable-result agent runtime** (capability 7/8): a runtime that returns real agent output
   (not `LocalCLIAdapter`'s byte-length references), so "receive usable results" becomes real.
   Design/owner TBD — not started here.
3. **Session-start context assembler** (FS-4): read-only, reusing existing memory/continuity
   services — **design complete**, build deferred (`LIFE_SESSION_CONTEXT_ASSEMBLY_DESIGN_2026-11-09.md`).

These three + the **operating-cycle wiring already in flight** (PR #338 GitHub-fed intake → #339
authorized dispatch) + the **composed loop that exists to hold them** (`app.mainai_executive`,
which "wires, does not replace") are the entire missing integration. The first authorized
end-to-end pass then needs the Founder **DECISION** points only: activation + spend grant
(`first_cycle_go`), a configured provider, and independent review kept in the loop.

## 4. Recommended first integration increment (safe, bounded, no activation)

The highest-leverage piece that is **pure bounded code, needs no activation, and unblocks
two hops at once** (tracking + CI/review correlation) is **#1: the GitHub read-surface**. It
would be its own reviewed PR on `app.integrations.github_client`, with tests against recorded
GitHub API fixtures (no live token in CI) — not started here, flagged as the recommended next
docs→code step after the security P1s land.

Order (dependency-respecting, merge-rule honored):
1. Land security P1s (#371, #372, #374) + #323 — independent, de-risking.
2. Build the GitHub read-surface (#1) as its own PR.
3. Merge the operating cycle #338 → #339 as each dependency actually merges.
4. Address the agent-runtime gap (#2) when an owner/design is confirmed.
5. Build the session-context assembler (#3) per its design, when prerequisites + owner confirmed.
6. **Founder decision:** authorize one egress-gated, spend-bounded end-to-end cycle; prove the
   chain once with real evidence + independent review; record results as capability records.

## 5. Non-duplication

Reuses and does not restate: `LIFE_OPERATIONAL_PRIORITY_2026-11-09.md` (capability audit),
`LIFE_FIRST_START_READINESS_MATRIX_2026-11-09.md` (readiness + FS blockers),
`LIFE_SESSION_CONTEXT_ASSEMBLY_DESIGN_2026-11-09.md` (hop "persist continuity"),
`docs/BRANCH_REGISTRY.md` (the living dependency map). Introduces no new product architecture —
only names the integration seams between components that already exist.
