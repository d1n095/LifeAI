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
what is active / finished / independently certified, why something is blocked, and
`needed_work` (which agent must acknowledge, execute, or unblock). MainAI reads that
list instead of asking the Founder to forward routine reports.

The Founder-ops page renders the same section. `first_cycle_go` stays false;
`blocks_first_cycle` stays false.

The home dashboard polls the same snapshot. A later failed poll drops the previous
reachable copy (D4).

## GitHub read-only development intake

`app.agent_coordination.github_intake.GitHubDevelopmentIntake` observes public GitHub
PRs, exact head SHAs, required CI (the same 12-check contract as `required_checks.py`),
commit ancestry and reviews, then records them on the existing durable ledger.

- Credential-free public GET is the default. Token, if present, is still GET-only.
- Rate limits, 401/403 and 5xx are BLOCKED with a named missing capability.
- A published (non-draft) PR creates one builder obligation and one examiner obligation
  per PR. Duplicate ingest is idempotent.
- Failed or running required checks on the current SHA block freeze. A failure on an
  ancestor SHA is stale and cannot invalidate a newer green SHA.
- The intake never writes to GitHub and never claims Cursor/Claude/Codex delivery.
- Founder-ops shows `durable_coordination.github_intake` so the Founder does not copy
  GitHub status between agents. `POST /api/mainai/founder-ops/github-intake/refresh`
  is Founder-only and read-only.

## Production lifecycle ingestion

`app.agent_coordination.lifecycle` is the production boundary that feeds assignment
status changes, execution observations and GitHub read-only intake into the same durable
ledger. The worker poll loop (`Worker._reconcile_durable_coordination`) replays persisted
events so the Founder does not trigger every refresh.

- Authenticated `POST /api/mainai/founder-ops/coordination/events` rejects spoofed identities,
  forged completions and GitHub-CI-as-certification.
- A completed builder becomes eligible for further work; a published candidate creates exactly
  one examiner obligation. Blocked work keeps its dependency.
- GitHub-green CI is never independent certification.
- Local CLI adapters may execute only when Founder-enabled. Cursor/Claude/Codex messaging stays
  BLOCKED. A prepared instruction is not a delivered message.
- `first_cycle_go` stays false.

## Agent Messaging & Delivery Gateway

`app.agent_coordination.delivery` is the production inbox + receipt layer on the same
ledger. `Worker._reconcile_durable_coordination` also prepares and transmits inbox
messages after lifecycle reconcile.

States: `prepared` → `available` → `claimed` → `delivered` → `acknowledged` →
`executing` → `completed`, or `blocked` / `failed`. Creating an inbox row is not
delivery. A Founder-issued `local_authorized_bridge` that successfully claims a
payload is delivered. Recipient confirmation is acknowledged. Execution requires
evidence. Cursor/Claude/Codex vendor transports stay BLOCKED (`agent_messaging_adapter`).

Recipient API: `GET /api/mainai/agent-gateway/inbox` and
`POST /inbox/{id}/claim` / `POST /receipts` with a scoped Bearer token. The token
cannot override STOP, write to GitHub, or mint independent certification.

Founder issues tokens at `POST /api/mainai/founder-ops/agent-gateway/bridges`.
Founder-ops shows per-agent delivery status so routine reports are not copied by hand.

## Out of scope

Codex Founder Authority, security grants, GitHub publication, Recall activation,
policy overrides, personal credentials, live paid providers, and any competing approval
system. Frozen PR #297 is not modified.
