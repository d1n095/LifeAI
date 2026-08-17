# LIFE CAPABILITY REALITY / SELF-MODEL

## Foundation scope

This document defines the foundation introduced by migration 0048: a queryable, evidence-backed
record of what Life and the surrounding system can actually do right now, distinct from what is
merely configured, planned, or unknown. It answers the "capability matrix and derived scoring"
layer named as explicitly deferred in `docs/LIFE_INTELLIGENCE_GOVERNANCE_META_LEARNING.md` and
the "automatic capability scoring" layer named as explicitly deferred in
`docs/LIFE_PROBLEM_SOLUTION_DECISION_LEARNING.md` — but builds only the capability-reality half
of that gap. It introduces no queue, executor, router, or second agent/adapter registry, and no
automatic scoring: every fact here is an explicit, caller-supplied assertion.

## Relationship to other governance/status concepts — not a duplicate of any of them

Three existing narrower "is X actually usable" mechanisms already exist in this codebase, each
scoped to one specific thing:

- `app.agent_coordination.adapter_config.adapter_availability()` (PR #85/#87) — is a specific
  coding-agent CLI provider (Claude Code/Cursor Agent/Codex) supported/found/enabled.
- `app.agent_coordination.adapters.AdapterCapabilities` (PR #90) — what interactive operations
  ONE already-constructed adapter instance declares it supports.
- `provider_verification_checks` (referenced in `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`
  §4.6) — is a configured AI chat/embedding provider's key actually valid.

None of these is general-purpose. This foundation is the first general capability registry —
spanning any domain (agent dispatch, document ingestion, search, deployment, anything else) —
and it REUSES the narrower mechanisms above as evidence sources rather than replacing or
duplicating their own logic. `app.capability_reality.agent_bridge.sync_agent_adapter_capability()`
is the one concrete bridge this migration ships: it translates
`adapter_availability()`'s own facts into a `capability_records` row, reimplementing none of
that function's own logic and adding no second source of truth for real-adapter availability.

## Principles

- Never infer `verified_available` from a binary, executable, or adapter merely existing or
  being enabled. Verification requires an explicit, caller-supplied observation of a real
  successful use.
- `unknown` is always a valid, first-class status — never a gap to paper over with a guess.
- Recording a capability GAP (`record_capability_gap()`) never pretends the capability exists;
  it can only ever produce `status="planned"`.
- `authority` reuses the EXACT closed vocabulary migration 0042 (`LifeProblem`/`LifeProblemDecision`)
  already established (`founder | repeated_founder_preference | deterministic_source |
  inferred_pattern | ai_interpretation | unknown`) — never a second, competing provenance
  taxonomy for "who/what asserted this fact."
- The live current-state row (`CapabilityRecord`) and the append-only history
  (`CapabilityObservationEvent`) are kept strictly separate, mirroring the SAME split already
  proven twice in this codebase: `agent_scope_leases` vs. `agent_work_assignment_events`
  (migration 0046) and `agent_dispatch_executions` vs. the same events table (migration 0047).
  A capability's current state is cheap to query without reconstructing it from history; the
  history stays fully auditable and is never itself mutated.
- Every append-only event's own `detail` carries every fact a call actually asserted
  (status/status_reason/authority, plus verification_evidence_id/success when supplied) — never
  only the fact that happened to determine the event's label. A caller reconstructing "what did
  observation N assert" never has to cross-reference the live row's own overwritable
  `last_verification_evidence_id`/`last_success_at`/`last_failure_at`.
- Re-asserting the SAME status with no new verification/success information is a real, distinct,
  honestly-labelled fact (`observation_reasserted`) — never mislabelled as `status_changed` when
  nothing changed.

## Existing systems reused

`intelligence_evidence` (migration 0038) may be referenced as the concrete evidence a
verification observation is grounded in — `capability_records.last_verification_evidence_id` is
a nullable FK (`(id, owner_id)` composite, `ON DELETE SET NULL`), never a copy of evidence
content. `coordination_agents` (migration 0046) may be referenced when a capability IS an
agent/adapter capability — `capability_records.agent_id` is a nullable FK, never a second agent
registry. The `authority` vocabulary is migration 0042's own, reused verbatim.

## Durable records

`capability_records` — one owner-scoped, queryable fact per `capability_key` (e.g.
`"agent_dispatch.codex"`, `"document_ingestion.ocr"`): `status` (`verified_available |
configured_unavailable | configured_disabled | planned | unknown`), `status_reason`,
`authority`, `required_permissions`, `dependencies`, `known_limitations`, `confidence`
(0-1, nullable), `agent_id` (nullable), `last_verification_evidence_id`/`last_verified_at`,
`last_success_at`, `last_failure_at`, `provenance`. `domain` is an open, non-enumerated grouping
label, matching `intelligence_executions.domain`'s own precedent of staying extensible rather
than a fixed CHECK-constrained vocabulary. `UNIQUE (owner_id, capability_key)` — one live row
per capability, updated in place as new observations arrive, never a growing pile of rows for
the same capability.

`capability_observation_events` — append-only history (`status_changed | verification_recorded
| success_recorded | failure_recorded | gap_recorded | observation_reasserted`), DB-trigger
enforced (`trg_capability_observation_events_deny_mutation`), same pattern as
`agent_work_assignment_events`.

`app.capability_reality.service`:
- `record_capability_observation()` — the one write path; finds-or-creates the live row for
  `(owner_id, capability_key)`, applies exactly the fields the caller supplied, always appends
  exactly one observation event.
- `record_capability_gap()` — a thin, named wrapper that can only ever produce
  `status="planned"`, plus its own `gap_recorded` event on top of the inner call's own event —
  so "a gap was explicitly recorded" and "the status changed to planned" both remain durable,
  distinct facts.
- `get_capability_reality()` / `list_capability_records()` / `list_capability_gaps()` — read
  paths. `list_capability_gaps()` is `list_capability_records(status="verified_available")`'s
  complement: everything NOT verified — planned, configured-but-disabled,
  configured-but-unavailable, or genuinely unknown.

`app.capability_reality.agent_bridge.sync_agent_adapter_capability()` — the one concrete bridge
to an existing narrower system (see "Relationship" above). Can only ever produce
`configured_disabled`/`configured_unavailable`/`unknown` — an executable being found on PATH
AND the founder having enabled it is still not verification; only a caller who has observed a
real successful dispatch may pass `status="verified_available"` for an agent-dispatch
capability.

`erase_own_capability_reality_children()` — wired into `erase_account_data()`, same
`SECURITY DEFINER`, no-owner-argument shape as `erase_own_agent_coordination_children()`.

## Explicitly deferred layers

- Automatic capability discovery (scanning the filesystem/environment and inventing capability
  records without an explicit caller assertion) — deliberately never built; see Principles.
- Automatic capability scoring/ranking across capabilities — this foundation records facts, it
  does not rank them.
- A UI surface for founders to browse capability reality (data + service layer only, matching
  every other "foundation" layer in this codebase).
- Wiring capability-gap detection into an automatic self-improvement trigger — see the separate,
  not-yet-built controlled self-improvement loop this foundation is a prerequisite for.
- A generic tool/module installation lifecycle (`installation_source`, `update_policy`,
  `uninstall_behavior` as sketched in `docs/LIFE_CANONICAL_ARCHITECTURE.md` §H's own provisional
  design) — this migration ships the smaller, evidence/status-focused subset of that sketch;
  the installation-lifecycle fields remain future work, not silently assumed unnecessary.
