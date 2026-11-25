# Life Workspaces

**Branch:** `freebuff/life-workspaces`
**Code checkpoint:** `83c87049646cf2a52bc1baeb0527776b99fe5a85`
**Base:** Founder Alpha `04c82c3aa4ea411835c48561f6c03cef4bb87999`
**Migrations:** `0093_life_workspaces`, `0094_workspace_sync_and_supervised_goals`
**Status:** implemented and verified in the isolated test environment; no production deployment or shared-branch merge. `first_cycle_go=false`.

Exact-SHA evidence: `docs/evidence/life-workspaces/`.

## What a workspace is

A Life Workspace is the existing founder `projects` row plus the product composition that was
missing: purpose/goals/settings, archive and soft-delete retention, owner-scoped activity and
decision history, workspace-linked conversations, a bounded context view, optimistic
concurrency, and a durable reconciliation ledger. It deliberately does not introduce a second
project model, chat engine, knowledge store, execution engine or authorization system.

Existing relations reused unchanged:

- `projects` — workspace identity and metadata.
- `documents.project_id` — workspace knowledge.
- `tasks.project_id` — workspace work items.
- `conversations.project_id` — workspace conversation link.
- `mainai_goals` / `mainai_tasks` — real supervised work, now optionally linked to a workspace.
- `mainai_jobs`, provider and billing paths — untouched.

## Persistence

Migration `0093_life_workspaces` adds:

- `projects.purpose`, `goals` (JSON array), `settings` (JSON object), `archived_at`,
  `deleted_at`, `last_activity_at`.
- `workspace_members` — owner/collaborator membership foundation, owner-scoped by RLS.
- `workspace_activities` — append-only owner-scoped activity history.
- `workspace_decisions` — proposed/accepted/superseded project decisions.
- `conversations.project_id` — nullable workspace association with `ON DELETE SET NULL`.

Migration `0094_workspace_sync_and_supervised_goals` adds:

- `projects.version` — monotonic optimistic-concurrency version.
- `mainai_goals.project_id` — optional workspace link; `ON DELETE SET NULL`, no execution
  authority change.
- `workspace_sync_runs` — durable idempotent reconciliation runs with snapshot hash,
  progress, attempts, heartbeat, error category and terminal state.
- `workspace_sync_items` — per-entity snapshot rows with content hash, status and error.

Both migrations are additive and independent of the Security Kernel/R15 lane. Claude owns
integration reconciliation; do not renumber or rewrite them.

## API

All routes are founder-only and owner-scoped (`created_by == current founder`):

```text
GET    /api/workspaces?include_archived=false&limit=50&offset=0
POST   /api/workspaces
GET    /api/workspaces/{id}
PATCH  /api/workspaces/{id}          # optional expected_version -> 409 when stale
POST   /api/workspaces/{id}/archive
POST   /api/workspaces/{id}/restore
DELETE /api/workspaces/{id}          # soft delete, retained row

GET    /api/workspaces/{id}/knowledge?limit=...&offset=...
POST   /api/workspaces/{id}/documents/{document_id}
DELETE /api/workspaces/{id}/documents/{document_id}

GET    /api/workspaces/{id}/tasks?limit=...&offset=...
POST   /api/workspaces/{id}/tasks

GET    /api/workspaces/{id}/conversations?limit=...&offset=...
POST   /api/workspaces/{id}/conversations

GET    /api/workspaces/{id}/activity?limit=...&offset=...
GET    /api/workspaces/{id}/decisions?limit=...&offset=...
POST   /api/workspaces/{id}/decisions

GET    /api/workspaces/{id}/context?q=...
GET    /api/workspaces/{id}/supervised-tasks?limit=...&offset=...

GET    /api/workspaces/{id}/sync
POST   /api/workspaces/{id}/sync              # idempotency_key
GET    /api/workspaces/{id}/sync/{run_id}
POST   /api/workspaces/{id}/sync/{run_id}/retry
```

`GET /context` composes objectives, source documents, decisions, outstanding tasks, past
conversations, recent activity and explicitly uncertain/proposed knowledge. With `q`, it also
uses the existing `retrieve_context(..., project_id=...)` path; retrieval failure is returned
as an explicit `retrieval_degraded_reason`, never silently treated as an empty knowledge base.

## Optimistic concurrency and pagination

`PATCH /api/workspaces/{id}` accepts `expected_version`. A stale value returns `409` and does
not overwrite the newer workspace. Successful metadata, archive, restore, document association,
task, decision and conversation changes advance `projects.version`. List and collection
endpoints use bounded `limit`/`offset` with deterministic `(timestamp, id)` ordering.

## Incremental synchronization

`POST /sync` builds a deterministic snapshot of the workspace row, owned documents, tasks,
conversations and decisions. It stores only entity ids, versions and content hashes — never
document text or chat message content.

- `idempotency_key` is unique per owner/workspace; replaying it returns the same run.
- Unchanged entities from an earlier completed run are marked `skipped`, not reprocessed.
- A changed snapshot produces a new run and real `applied` items.
- A failed run is durable and retryable; retry resets items and increments `attempt`.
- A retry whose snapshot is no longer current returns `409` instead of applying stale results.
- A run that changes during execution ends `stale`, with the current snapshot hash recorded.
- A running run whose heartbeat is older than five minutes is reconciled to `failed` with
  `error_category="interrupted"` before the next sync.
- No provider call and no execution is performed by sync; it reconciles existing durable state.

## Supervised MainAI work

`POST /api/mainai/execution/goals` accepts an optional `project_id`, verified against the
caller's own non-deleted workspace. `GET /api/workspaces/{id}/supervised-tasks` composes the
real `mainai_goals` and `mainai_tasks` with:

- goal/task state, liveness, attempts, risk, blocker and approval requirement;
- append-only task events and verification evidence;
- checkpoints, external waits and recovery records;
- the linked `mainai_job` status.

Approval and cancellation use the existing `/api/mainai/execution/tasks/{id}/approve` and
`/cancel` endpoints. The workspace layer does not dispatch, execute, retry or duplicate
execution logic, and it does not activate autonomy.

## Chat isolation

`POST /api/chat` and `/api/chat/stream` accept an optional `project_id`:

- a new conversation is persisted with that `project_id`;
- an existing conversation may be adopted into a workspace only if it has no project;
- a conversation already bound to another workspace returns `409`;
- retrieval is called with `project_id=conversation.project_id`, so document chunks from other
  projects are not mixed into the workspace answer.

The existing idempotency, billing, egress, provenance and failure-boundary paths are reused
unchanged. Project documents remain untrusted retrieval data, never instructions.

## Frontend

- `/workspaces` — list, create with purpose/goals, archive/restore, open.
- `/workspaces/[id]` — overview, versioned settings edit (name/purpose/goals/status/settings
  JSON), knowledge association, simple tasks, decisions, activity, sync history and retry,
  supervised MainAI task evidence with approval/cancel controls, workspace-scoped streaming
  chat with source attribution and a real stop button.
- Sidebar entry: `Workspaces`.

The pages use the existing Next.js proxy, auth cookies/CSRF and styling conventions. They
implement loading, empty, error, permission-denied, archived and recovery states; no sample
data is rendered. Switching workspace resets the conversation and loads only the new
workspace's conversations.

## Verification

Executed against real PostgreSQL with the restricted runtime role at code checkpoint
`83c87049646cf2a52bc1baeb0527776b99fe5a85`:

- `tests/backend/test_workspaces_api.py`, `test_projects_detail.py` and
  `chat/test_chat_request_idempotency.py` — **15 passed, 0 failed**.
  Covers lifecycle, scoped collections, context, archive/restore/soft delete, foreign-owner
  and foreign-document denial, pagination, optimistic conflict, sync idempotency/duplicate
  prevention, failed-run retry, stale rejection, interrupted-run reconciliation, supervised
  task evidence, approval/cancel and owner scoping.
- `test_mainai_execution_api.py` + `test_mainai_execution_planner.py` — **58 passed, 0 failed**.
- Frontend `npm run lint` — **0 errors**, 1 pre-existing warning.
- Frontend `npm run build` — **PASS**, `/workspaces` and `/workspaces/[id]` generated.
- Browser `frontend/e2e/workspaces.spec.ts` — **2 expected / 0 unexpected / 0 flaky**:
  - create with goal, task, decision, streamed chat, reload persistence, versioned settings
    edit, sync, archive/restore;
  - transient create failure shown and recovered on retry.

## Known limitations

- The browser journey uses the existing deterministic E2E provider, not a real external model.
- Membership is persisted as an owner/collaborator foundation; the current product remains
  founder-only, matching the existing `require_founder` boundary.
- Workspace context and sync are bounded and deterministic; they do not claim to be an
  autonomous planner or an execution authorization.
- The sync ledger is a reconciliation checkpoint over existing state, not a second ingestion
  pipeline; document processing still belongs to the existing import/worker path.
- No production deployment, push, PR, merge, force-push or migration-lane rewrite was done.
