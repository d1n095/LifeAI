# LIFE — MainAI Internal Workforce Foundation (Stage T)

## WHY

Before the first serious MainAI autonomous run, MainAI must be the **executive head of an
internal workforce** — not a lone model that casually calls peers. Assistants are not peer
MainAIs and do not inherit her authority.

Organizational model:

```
Founder → MainAI → Workforce Manager / Delegation Core
                 → Lead Assistants / Department Agents
                 → Specialist Workers
                 → Temporary Subagents
```

## GLOBAL INVARIANTS

| Invariant | Meaning |
|---|---|
| AGENT ROLE ≠ AUTHORITY | Role names intent; authority is explicit and task-scoped |
| MODEL CAPABILITY ≠ ACCESS | Being able to call a model does not grant tools/secrets |
| PROVIDER ≠ TRUSTED INTERNAL STATE | External APIs are temporary contractors |
| PAST AGENT SUCCESS ≠ FUTURE AUTHORITY | Evidence informs selection; never auto-grants power |
| DELEGATION ≠ AUTHORIZATION | Broker assignment grants zero extra authority |
| EXTERNAL MODEL OUTPUT ≠ TRUSTED FACT | Results start UNVERIFIED and are treated as DATA |
| CONFIDENCE ≠ PERFORMANCE EVIDENCE | Agents cannot self-rate into trust |

## REUSE (DO NOT DUPLICATE)

| Concern | Canonical system |
|---|---|
| Coding-agent reachability / repo scope | `app.agent_coordination` + `coordination_agents` (0046) |
| Path/capability/risk authority | `app.execution_envelopes` |
| Provider spend caps | `app.provider_spend` |
| Vault / egress / disclosure ledger | `app.egress_policy` + `provider_disclosure_events` |
| Evidence | `app.intelligence_governance` |
| Capability status | `app.capability_reality` |
| Active context graph | `app.active_context` |

This foundation adds an **owner-scoped organizational layer** above those primitives.

## SAID vs IMPLEMENTED (honesty table)

| Piece | Status in this PR |
|---|---|
| Agent Registry schema + register/retire/disable | **IMPLEMENTED** (`workforce_agent_profiles`) |
| Delegation Broker contract (request → assignment) | **IMPLEMENTED** (no provider invoke) |
| Task-scoped authority envelope fields + revoke/expire | **IMPLEMENTED** (assignment columns + helpers) |
| Context package minimization / forbidden external kinds | **IMPLEMENTED** |
| Trust zones | **IMPLEMENTED** (string field + classify) |
| Performance rollup ledger | **IMPLEMENTED** |
| Explainable selector | **IMPLEMENTED** (evidence-based scoring) |
| Team formation (no shared auto-context) | **IMPLEMENTED** (schema + `form_team` / patterns) |
| Inspectable org snapshot | **IMPLEMENTED** (`organization_snapshot`) |
| Injection scrubbing of authority keys | **IMPLEMENTED** (structural scrub) |
| Failure/takeover/checkpoints (T13) | **IMPLEMENTED** (0068 + `workforce.failure`) |
| Verification policy pipeline (T14) | **IMPLEMENTED** (`workforce.verification`) |
| Cost budgets org ceilings (T16) | **IMPLEMENTED** (gates; real spend still `provider_spend`) |
| Hiring/learning lifecycle (T9/T10) | **IMPLEMENTED** (`workforce.lifecycle`; `trained` only for fine_tune) |
| Low-risk vertical slice dry-run (T19) | **IMPLEMENTED** in-process; **provider activate blocked** |
| Provider-neutral worker harness | **PREPARED** (`provider_worker`) — gates + envelope + receipts; real invoke staged |
| First logical team (9 departments) | **BOOTSTRAPPED** as candidates — honest SAID≠IMPLEMENTED per dept |
| Staff decision loop (need→hire/refuse) | **IMPLEMENTED** (`staff_manager`) — ROI/complexity; no endless hire |
| Wire to live provider / CLI adapter execution | **NOT IMPLEMENTED** (waits verified safety gates) |
| Founder UI | **NOT IMPLEMENTED** (backend snapshot only) |
| Consequential vertical slice with real model call | **NOT YET** — refused until gates verified |

## FIRST TEAM (logical, not fake runtime)

Architecture supports eventual departments: Research, Planning, Memory, Coding,
Testing/Red Team, Security, Cost/Finance, Personal Intent, Self Improvement.

Do **not** pretend autonomous agents exist until runtime wiring is real.

## DEPENDENCY RULE

Schema/contracts/tests proceed now. Runtime delegation that depends on unverified personal-
intent / authority gates (e.g. #218) waits until those fixes are independently Claude-verified
and merged.

## MIGRATION

`0067_workforce_foundation.py` — six owner-scoped RLS tables (registry/broker/context/…).
`0068_workforce_ops.py` — checkpoints, lifecycle events, cost budgets, verification decisions +
assignment failure/takeover columns.
