# Delegate wiring, SPEND-STOP, and absence

Isolated successor of `#312` @ `22458257fbcfcf4170d2a957cd1c6464b8a2cdc8`,
composing `#310` operational-handover policy onto the corrected provider path.

Does **not** amend `#309`–`#313`. `first_cycle_go=false`.

## What this wires

`run_authorized_operating_cycle` is a real tick on existing services:

1. Interactive Founder admit (`InteractiveIsolation`)
2. Authoritative STOP / assignment fence (`assert_transition_authority`, `assert_grant_allowed`)
3. Absence policy (`evaluate_absence_tick`) — silence is never permission
4. Delegation Broker request and capability/provider-constrained workforce selection
5. Task-scoped `WorkforceAssignment`, separately namespaced durable obligation, then provider bind
6. Authenticated fixture ingestion (`ingest_authenticated_provider_event`)
7. Independent examination (`continue_after_independent_verification`)
8. Next eligible task + catch-up report

No live paid mutate. Provider FINISHED is not independent certification.

## Selector and broker boundary hardening

The operating cycle never treats a workforce `agent_key` as a provider identity. A profile
selected for the Cursor webhook path must explicitly declare `provider_type=cursor`; its
organizational key is retained as workforce evidence while the provider binding uses the
separate canonical `cursor` identity.

Every new obligation must be backed by `submit_delegation_request()` and
`resolve_delegation()`. The broker enforces owner scope, independent builder/verifier
identities, STOP/authority-epoch fencing, minimized context and a task-scoped assignment.
Missing candidates, missing verifiers, wrong providers and legacy obligations without a
broker assignment fail closed. A retry reuses the same live broker assignment and rechecks
revocation instead of minting a parallel grant.

## SPEND-STOP

`reserve_provider_spend_call` now calls `assert_not_killed` and `assert_grant_allowed`
before creating a new hold. Revoked/expired grants still fail closed through the
existing authorization row. Codex STOP-clear is untouched.

## Out of scope

- Autonomy GO / `first_cycle_go`
- Live provider credentials
- Merge, deploy, Recall
- 5 GB certification (`#313` remains the large-file lane)
