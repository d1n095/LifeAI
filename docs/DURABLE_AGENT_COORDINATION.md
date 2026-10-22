# Durable Agent Coordination & Artifact Handoff (CO-4b / CO-5)

Reusable Life OS coordination layer above existing `agent_work_assignments`, assignment
events, AuditLog, and Founder-ops snapshot. It records who owns each cross-agent obligation
and whether an artifact actually moved — without a second privilege system and without
becoming a first-cycle GO blocker.

## What this is

- **CO-4b** — durable obligation + ownership-timeline ledger.
- **CO-5** — artifact-handoff registry with exact hashes, provenance, and receipt evidence.

`app.agent_coordination.durable.DurableCoordinationService` is the single subsystem.

## States

Explicit founder-facing states: `assigned`, `acknowledged`, `executing`, `blocked`,
`completed`, `failed`, `superseded`, `independently_verified`.

An instruction records `assigned`. Execution is never inferred from an instruction, a plan,
or a queue-empty claim. Independent certification requires a completed obligation and an
examiner identity distinct from the builder.

Repeated planning without execution evidence becomes `blocked` with
`stall_reason=planning_without_execution`. No new plan is generated.

## Reconciliation

Events are keyed by `event_id`. Duplicate ids are replay-ignored (lost-ack safe).
Out-of-order reports against an unknown assignment are buffered until assign lands.
Stale generation / superseded completions cannot mint a second assignment.
Canonical `instruction_id` assign is idempotent, including after completion.
Queue-empty claims that contradict active obligations are recorded as stale and ignored.

## Artifact handoff

Each handoff stores sha256, byte length, producer/consumer, expiry, integrity, delivery
evidence, and receipt evidence.

Authorized adapters (only what this runtime actually has):

- `in_process_registry` — durable hash + provenance in the ledger.
- `local_authorized_workspace` — copy inside the current workspace, destination must stay
  inside assignment `allowed_paths`, integrity checked before and after copy.

Unavailable, fail-closed (BLOCKED with a named missing capability):

- `vendor_private_workspace` → `vendor_private_workspace_access`
- `github_publication` → `codex_github_publication_authority` (Codex-owned)
- `personal_credentials` → `personal_credential_use_forbidden`

Agents cannot assert that the Founder completed a transfer.

## Persistence

No new Alembic revision. The ledger slice is stored on
`AgentWorkAssignment.provenance["durable_coordination"]`, handoff refs in `output_refs`,
chronology as `evidence_recorded` events, and security-relevant writes in `AuditLog`.

## Founder visibility

`build_founder_ops_snapshot()` includes `durable_coordination`: who owns each obligation,
what is active / finished / independently certified, and why something is blocked.
The Founder-ops page renders the same section. `first_cycle_go` stays false;
`blocks_first_cycle` stays false.

## Out of scope

Codex Founder Authority, security grants, GitHub publication, Recall activation,
policy overrides, personal credentials, live paid providers, and any competing approval
system. Frozen PR #297 is not modified.
