# Life Workspaces

**Branch:** `freebuff/life-workspaces`  
**Code checkpoint:** `a26a23d395147af21b17b43ae6734c788d398478`  
**Migration:** `0093_life_workspaces` (depends on `0092`)  
**Status:** implemented and verified in the isolated test environment; no production deployment or shared-branch merge. `first_cycle_go=false`.

Exact-SHA evidence: `docs/evidence/life-workspaces/`.

## What a workspace is

A Life Workspace is the existing founder `projects` row plus the product composition that was
missing: purpose/goals, archive and soft-delete retention, owner-scoped activity and decision
history, workspace-linked conversations, and a bounded context view. It deliberately does not
introduce a second project model, chat engine, knowledge store, execution engine or
authorization system.

Existing relations reused unchanged:

- `projects` — workspace identity and metadata.
- `documents.project_id` — workspace knowledge.
- `tasks.project_id` — workspace work items.
- `conversations.project_id` — new nullable link added by migration 0093.
- `mainai_jobs`, `mainai_execution`, `provider_*` — untouched.

## Persistence added by migration 0093

- `projects.purpose`, `goals` (JSON array), `settings` (JSON object), `archived_at`,
  `deleted_at`, `last_activity_at`.
- `workspace_members` — owner/collaborator membership foundation, owner-scoped by RLS.
- `workspace_activities` — append-only owner-scoped activity history.
- `workspace_decisions` — proposed/accepted/superseded project decisions.
- `conversations.project_id` — nullable workspace association with `ON DELETE SET NULL`.

The migration is additive and independent of the Security Kernel/R15 migration lane. Claude
owns integration reconciliation; do not renumber or rewrite this migration.

## API

All routes are founder-only and owner-scoped (`created_by == current founder`):

```text
GET    /api/workspaces?include_archived=false
POST   /api/workspaces
GET    /api/workspaces/{id}
PATCH  /api/workspaces/{id}
POST   /api/workspaces/{id}/archive
POST   /api/workspaces/{id}/restore
DELETE /api/workspaces/{id}                 # soft delete, retained row

GET    /api/workspaces/{id}/knowledge
POST   /api/workspaces/{id}/documents/{document_id}
DELETE /api/workspaces/{id}/documents/{document_id}

GET    /api/workspaces/{id}/tasks
POST   /api/workspaces/{id}/tasks

GET    /api/workspaces/{id}/conversations
POST   /api/workspaces/{id}/conversations

GET    /api/workspaces/{id}/activity
GET    /api/workspaces/{id}/decisions
POST   /api/workspaces/{id}/decisions

GET    /api/workspaces/{id}/context?q=...
```

`GET /context` composes objectives, source documents, decisions, outstanding tasks, past
conversations, recent activity and explicitly uncertain/proposed knowledge. With `q`, it also
uses the existing `retrieve_context(..., project_id=...)` path; retrieval failure is returned
as an explicit `retrieval_degraded_reason`, never silently treated as an empty knowledge base.

## Chat isolation

`POST /api/chat` accepts an optional `project_id`:

- a new conversation is persisted with that `project_id`;
- an existing conversation may be adopted into a workspace only if it has no project;
- a conversation already bound to another workspace returns `409`;
- retrieval is called with `project_id=conversation.project_id`, so document chunks from other
  projects are not mixed into the workspace answer.

The existing idempotency, billing, egress, provenance and failure-boundary paths are reused
unchanged. Project documents remain untrusted retrieval data, never instructions.

## Frontend

- `/workspaces` — list, create with purpose/goals, archive/restore, open.
- `/workspaces/[id]` — overview, knowledge association, tasks, decisions, activity,
  uncertain knowledge, workspace-scoped conversations and inline MainAI chat.
- Sidebar entry: `Workspaces`.

The pages use the existing Next.js proxy, auth cookies/CSRF and styling conventions. They
implement loading, empty, error and archived states; no sample data is rendered.

## Verification

Executed against real PostgreSQL with the restricted runtime role:

- `tests/backend/test_workspaces_api.py` — lifecycle, scoped collections, context, archive,
  restore, soft delete, foreign-owner 404, foreign-document 404, conversation link and
  workspace-scoped chat retrieval/conflict.
- `tests/backend/test_projects_detail.py` — existing project-detail contract.
- `tests/backend/chat/test_chat_request_idempotency.py` — chat idempotency regression.
- Frontend `npm run lint` — 0 errors (one pre-existing warning).
- Frontend `npm run build` — success, `/workspaces` and `/workspaces/[id]` generated.
- Browser journey `frontend/e2e/workspaces.spec.ts` — **PASS** (1 expected, 0 unexpected,
  0 flaky): create with goal, add task/decision, workspace chat, reload persistence,
  archive/restore.

## Known limitations

- The browser journey uses the existing deterministic E2E provider, not a real external model.
- Membership is persisted as an owner/collaborator foundation; the current product remains
  founder-only, matching the existing `require_founder` boundary.
- The workspace context is deterministic and bounded; it does not claim to be an autonomous
  planner or an execution authorization.
- No production deployment, shared-branch merge, force-push or migration-lane rewrite was done.
