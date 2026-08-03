# MainAI Runtime Truthfulness and Durable Job Foundation

Branch: `claude/mainai-job-runtime-foundation` (based on `claude/det-kommer-mer-879lcm`,
**not** on PR #31's still-unmerged S1A branch — see "Relationship to PR #31" below).

## Why this exists

MainAI must never be able to claim it "started" or "is working" on something unless a real,
durable, independently-observable row exists — one a human (or an automated recovery pass)
can query, cancel, and see fail or complete on its own, without trusting MainAI's own claim
about its own state. Before this branch, nothing in the codebase enforced that: an agent loop
could say "I am reading your documents now" and mean nothing more than an in-flight HTTP
request with no persisted trace if the process died mid-sentence.

This is the first slice of that guarantee — a durable job model, a worker that claims and
resumes it safely across restarts, and a Pydantic-level contract that makes it a validation
error to *say* "started/status/completed/failed/cancelled" without a real job ID behind it.

## Scope boundaries (what this branch is, and is not)

**Is:**
- A durable, owner-scoped job/event/proposal schema (migration 0025).
- A restart-safe claim/lease/heartbeat worker loop, sharing the existing worker process.
- One real job type, `corpus_review`: reads existing indexed documents, calls a real AI
  provider, produces reviewed-content *proposals* — never auto-approved knowledge.
- A founder-only Job API (create/read/list/cancel/retry + an explicitly-separate admin
  cross-owner read).
- The `MainAIExecutionResponse` contract object and `require_capability()` gate — the
  mechanism a *future* conversational/agent layer would use to stay honest. This branch does
  not yet wire that layer into `app/chat.py` or `app/agent_orchestration.py`; it only builds
  the contract and proves it's enforceable.

**Is not:**
- Not arbitrary terminal/shell execution. `CAPABILITY_MANIFEST` today contains exactly one
  entry, `corpus_review`; anything else fails closed via `CapabilityUnavailableError`.
- Not a second memory database. `mainai_jobs`/`mainai_job_events`/`mainai_job_proposals` live
  in the same Postgres database, same RLS pattern, same Alembic chain as everything else.
- Not a promotion path into `knowledge_claims`. `MainAIJobProposal` is a deliberately separate
  table; nothing in this branch writes a `KnowledgeClaim` from a proposal, automatically or
  otherwise.
- Not a second, competing admin/privilege tier. `GET /api/mainai/jobs/admin/all` is gated by
  the same `require_founder` dependency as every other route in this founder-only system —
  documented as an honest limitation below, not a real RBAC boundary.
- Not wired into chat/agent-orchestration UI flows yet. The Jobs/Activity frontend view added
  here is a standalone `/mainai/jobs` page for observing this job type specifically.

## Relationship to PR #31

This branch's base (`claude/det-kommer-mer-879lcm`) has Alembic head `0018`. PR #31's S1A
migrations (`0019`-`0024`, `MemorySourceUnit`/provenance work) exist only on PR #31's own
branch and are **not** touched, imported, or depended on here. This migration is numbered
`0025` specifically so a future rebase/merge with PR #31 has one unambiguous ordering question
to resolve explicitly (which migration chain lands on `origin/main` first), rather than two
different migrations silently claiming the same revision id. PR #31 itself, its branch, and
its migrations were not modified by this work.

## Data model

Three tables (migration `0025_mainai_jobs.py`), same RLS-per-table pattern as every other
owner-scoped table in this codebase (`app/rls.py`, template: migration 0007's
`knowledge_claims`).

### `mainai_jobs` — one row per durable job

| Column | Purpose |
|---|---|
| `id`, `owner_id`, `job_type`, `status` | Identity, ownership, lifecycle state |
| `created_at`/`started_at`/`last_heartbeat_at`/`completed_at` | The five lifecycle timestamps the founder required |
| `progress_current`/`progress_total`/`current_phase` | Observable progress, no fake animation — only real counters |
| `public_message` | Safe, bounded-length text — see "Safe error messages" below |
| `error_category` | Closed vocabulary (`MainAIJobErrorCategory`), never raw exception text |
| `retry_count`/`max_retries` | Bounded retry budget |
| `input_refs`/`output_refs` | JSON artifact references (e.g. `{"type": "document", "id": ...}`) |
| `locked_by`/`lease_expires_at` | Worker attribution + lease for restart-safe claiming |
| `provider`/`model` | Which AI provider/model actually did the work |
| `cancel_requested`/`cancel_acknowledged` | Two-phase cancellation — request is instant, acknowledgement is the job's own next safe checkpoint |
| `idempotency_key` | Unique per `(owner_id, idempotency_key)` — a partial unique index, so two different owners may reuse the same key independently |

Status vocabulary: `queued → running → (paused) → completed | failed | cancelled`. See
`app/models/mainai_job.py` for the three status-set constants
(`CLAIMABLE_MAINAI_JOB_STATUSES`, `RETRYABLE_MAINAI_JOB_STATUSES`,
`CANCELLABLE_MAINAI_JOB_STATUSES`) that encode the actual state machine — `test_status_sets_
are_disjoint_where_they_must_be` in the test suite pins their invariants (e.g. a cancelled job
is never retryable, a completed job is never cancellable).

```
queued ──claim──► running ──complete──► completed
   │                 │
   │                 ├──fail (transient/permanent/timeout/unexpected)──► failed ──retry (within budget)──► queued
   │                 │
   │                 └──cancel_requested seen at next checkpoint──► cancelled
   │
   └──cancel_requested before claim──► cancelled
```

### `mainai_job_events` — append-only execution-event history

One row per lifecycle transition (`created`, `claimed`, `heartbeat`, `phase_changed`,
`progress_updated`, `cancel_requested`, `cancel_acknowledged`, `completed`, `failed`,
`cancelled`, `retry_scheduled`). Application code only ever INSERTs here — enforced by
convention and test coverage, not a DB-level immutability trigger, matching this codebase's
existing append-only tables (e.g. `project_notes`).

### `mainai_job_proposals` — a job's output, never a promoted claim

`corpus_review`'s findings land here, each with `source_document_id`/`source_chunk_id`
provenance, `proposal_text`, and a `status` that starts `proposed` and can only ever become
`dismissed`. Nothing in this schema or its application code can turn a proposal into a
`KnowledgeClaim` automatically — `test_run_corpus_review_job_never_promotes_a_proposal_to_a_
knowledge_claim` asserts this directly against a real database.

`owner_id` is denormalized onto both child tables rather than resolved via a join in the RLS
policy — the same choice `document_chunks.owner_id` already makes relative to `documents`,
for the same reason: every RLS policy in `app/rls.py` is a single-column, non-join predicate,
and mixing join-based and column-based policies in one schema is a real, easy-to-miss
inconsistency for a future reviewer.

## Runtime truthfulness contract

`app/mainai_runtime_contract.py` defines `MainAIExecutionResponse`, a Pydantic model with
seven response modes: `answer`, `proposal`, `execution_started`, `status`, `completed`,
`failed`, `cancelled`. A `model_validator(mode="after")` enforces, at object-construction
time — not by convention, not by code review, but as a `ValueError` raised the moment the
object is built:

- `execution_started`/`status`/`completed`/`failed`/`cancelled` **require** a real `job_id`.
- `answer`/`proposal` **forbid** a `job_id` (they are not job-backed claims).

`require_capability(capability: str)` is the fail-closed gate: `CAPABILITY_MANIFEST` is a
fixed `frozenset` (today: `{"corpus_review"}`); anything not in it raises
`CapabilityUnavailableError` immediately. `create_job()` calls this **before** creating any
database row, so an unknown capability can never leave even a trace row behind
(`test_create_job_rejects_unknown_capability_before_creating_any_row` asserts a zero row count
after the rejection).

This contract is not yet wired into `app/chat.py`'s response path — that integration (making
the conversational layer actually construct `MainAIExecutionResponse` objects instead of free
text) is explicitly out of scope for this branch; see "Remaining work" below.

## Worker: claim, lease, resume

`app/jobs/mainai_job_lease.py` implements a single-phase `SELECT ... FOR UPDATE SKIP LOCKED`
+ lease-TTL claim (`claim_next_mainai_job`), deliberately simpler than `ImportJob`'s two-phase
owner-erasure-lock-coordinated claim (`app/jobs/lease.py`): `corpus_review` jobs only ever
*read* documents/chunks, never write a storage blob, so there is no write-before-reference
race for a concurrent account erasure to lose. A future MainAI job type that *does* write
storage would need to take the same `acquire_storage_key_lock`/`acquire_owner_erasure_lock`
every other blob writer in this codebase takes — nothing here exempts it.

`app/worker.py`'s `Worker.run_once()` claims a `knowledge_import_jobs` row first (existing
behavior, unchanged); only if nothing was claimable does it try `claim_next_mainai_job` — one
shared poll loop, not a second worker process, per the explicit "reuse the existing
architecture" instruction. On a successful mainai claim, `process_claimed_mainai_job()`:

1. Sets the session's RLS owner context to the claimed job's owner
   (`_set_mainai_job_rls_owner` — mirrors `library_import.py`'s `_set_rls_owner`, required
   because the worker session never goes through `app/deps.py`'s `get_current_user`, and
   `run_corpus_review_job` makes several separate `db.commit()` calls as it progresses,
   each ending the current transaction).
2. Records the `claimed` event + `mainai_job_claimed` audit entry (`record_claimed`).
3. Dispatches to the job type's own processing function (today: `run_corpus_review_job`).
4. Catches anything that escapes unexpectedly (a bug, not a designed failure path) and still
   leaves the job in a terminal, truthful state (`mark_failed(..., unexpected)`) rather than
   stuck `running` forever with a dead claim.

**Restart safety**: `corpus_review_job.py` processes one document per unit of work, commits
progress + a proposal (or a per-item failure event) together after each document, checks
`cancel_requested` and renews the lease before every document, and resumes correctly after a
crash by querying already-created `MainAIJobProposal.source_document_id` values for that job
to skip work already done — proven by
`test_run_corpus_review_job_is_restart_safe_and_skips_already_reviewed_documents`, which
seeds a proposal as if a prior attempt had crashed mid-job and asserts the resumed run does
not re-review that document.

**Concurrency**: `test_two_workers_racing_many_jobs_never_claim_the_same_job` drives two real
OS threads against 15 real jobs through `claim_next_mainai_job` directly (same pattern as
`test_worker.py`'s `ImportJob` race test) and asserts no job is ever claimed twice and no job
is lost.

**Stale-lease recovery**: `claim_next_mainai_job`'s claim predicate also matches
`status = 'running' AND lease_expires_at < now()` — a worker that crashed or was killed mid-
job leaves its claim behind only until the lease naturally expires, after which any worker's
next poll reclaims it (`test_claim_next_mainai_job_reclaims_an_expired_lease`).

## `corpus_review`: the first real job type

`app/rag/corpus_review_job.py` receives existing, already-indexed `Document` references
(never duplicates or re-imports files), concatenates their `DocumentChunk` text (bounded to
8000 characters per document — `_MAX_REVIEW_CHARS`), and calls
`app.providers.registry.chat_with_fallback()` — the **same real provider-fallback call**
`app/agent_orchestration.py`'s code/review agent loop uses. This is a genuine AI provider
call, never a canned or fabricated response, per the explicit "no placeholder functionality
or fake progress" requirement.

Each document's finding becomes one `MainAIJobProposal` with `source_document_id`/
`source_chunk_id` provenance. `ProviderError` is classified via the existing
`is_transient_error()` helper into `transient_io` or `permanent`; anything else is
`unexpected` — the job is marked failed with the matching safe category, never with the raw
exception text.

## Job API

`app/routers/mainai_jobs.py`, `prefix="/api/mainai/jobs"`, every route gated by
`require_founder` (this system is founder-only today — see `app/deps.py`):

| Route | Purpose |
|---|---|
| `POST ""` | Create a job. Rate-limited (`rate_limit_default_per_minute`, currently 120/min). 409 on unknown capability, 422 on invalid `input_refs`. |
| `GET ""` | List the caller's own jobs (paginated, RLS-scoped). |
| `GET "/{job_id}"` | Job detail + its event history. 404 (never 403) for a job that exists but isn't the caller's — RLS makes it genuinely not exist from this session's point of view. |
| `GET "/{job_id}/proposals"` | The job's proposals. |
| `POST "/{job_id}/cancel"` | Idempotent cancel request. 409 if the job is already terminal. |
| `POST "/{job_id}/retry"` | Retry a failed job within its retry budget. 409 if cancelled, completed, or budget exhausted — retry can **never** silently override a cancellation. |
| `GET "/admin/all"` | Cross-owner read, bypasses RLS via the migration/superuser connection. See threat model below for why this is an honest limitation, not a real admin tier. |

## Security guarantees

- **Owner isolation via RLS, not application logic.** Every table has `FORCE ROW LEVEL
  SECURITY` + a single-column policy (`app/rls.py`). `get_job()` uses
  `db.get(MainAIJob, job_id, populate_existing=True)` — the `populate_existing=True` is load-
  bearing, not cosmetic: plain `Session.get()` returns straight from SQLAlchemy's identity
  map without re-querying if the same `(class, pk)` was already loaded on that session, which
  would silently bypass RLS if the session's RLS owner context changes between two lookups of
  the same `job_id` (exactly what the worker loop does, session-per-process, across many
  owners' jobs; and exactly what
  `test_get_job_raises_not_found_for_a_different_owners_job` caught during development —
  forcing a real `SELECT` means Postgres re-evaluates the RLS policy against the *current*
  `app.current_user_id` every time, not the value at first load).
- **No cross-owner access by ID**, proven directly:
  `test_get_job_raises_not_found_for_a_different_owners_job`,
  `test_list_jobs_only_returns_the_current_owners_jobs`.
- **No secrets/raw content in status or error text.** `public_message` is set *only* from the
  fixed `_PUBLIC_ERROR_MESSAGES` table, keyed by the closed `MainAIJobErrorCategory` enum —
  never from `str(exception)`
  (`test_mark_failed_never_stores_raw_exception_text_as_public_message`).
- **Rate limiting** on job creation, via the same `slowapi`/Redis-backed limiter every other
  mutating route uses.
- **Audit events for every mutation**: create (`mainai_job_created`), start
  (`mainai_job_claimed`), cancel (`mainai_job_cancel_requested`), retry (`mainai_job_retry`),
  complete (`mainai_job_completed`), fail (`mainai_job_failed`), cancelled
  (`mainai_job_cancelled`) — each paired with an append-only `MainAIJobEvent` row. Proven end
  to end through the real worker poll cycle, not just by calling the service function
  directly, by `test_worker_run_once_records_a_claimed_event_and_audit_entry`.
- **Unauthorized access denied**: `test_api_requires_authentication` (401/403 unauthenticated)
  plus the RLS isolation tests above.
- **Idempotency is per-owner**, not global: `uq_mainai_jobs_owner_idempotency_key` is a
  partial unique index on `(owner_id, idempotency_key)`, so two different owners submitting
  the same key is not a collision (`test_create_job_different_owners_can_reuse_the_same_
  idempotency_key`).

## Threat model: the `/admin/all` endpoint

`GET /api/mainai/jobs/admin/all` deliberately bypasses per-owner RLS (it runs on the
superuser/migration connection, like the worker's own claim step, since it must see jobs
across every owner). The only gate in front of it is `require_founder` — the exact same
dependency every other route in this founder-only system already uses. **This is not a real
second privilege tier**: in a system with more than one privileged role, this endpoint would
need an actual admin-role check, not just "is this the founder." It is safe *today* only
because the founder is the sole account with any elevated access at all. This is flagged here
explicitly as a known, accepted limitation — not something to silently outgrow later.

## Known limitations

- The runtime-truthfulness contract (`MainAIExecutionResponse`) is built and tested in
  isolation but not yet wired into `app/chat.py` or `app/agent_orchestration.py` — a
  conversational response still is not *required* to go through it yet.
- `/admin/all` is founder-gated, not role-gated (see threat model above).
- Only one job type exists (`corpus_review`). The lease/claim design documents what a
  storage-writing job type would additionally need (owner-erasure lock coordination) but does
  not implement one.
- No `paused` transition is actually driven by any code path yet — the status exists in the
  model and state-machine sets for forward compatibility but nothing currently pauses a job.
- Job progress/heartbeat is polled by the frontend (see UI section), not pushed — no
  WebSocket/SSE channel exists for live updates in this branch.
- The new `/admin/jobs` page passed `tsc --noEmit`, `eslint`, and a full `next build` (which
  generates and type-checks the route). A live, logged-in, in-browser click-through could not
  be completed in this sandboxed session: a differential test proved the hang is in the
  sandbox's headless-browser harness itself, not this branch's code — the pre-existing,
  already-shipped `/admin/agents` page (identical `AuthGuard`/polling pattern, unrelated to
  this branch) reproduces the exact same stuck "Kontrollerar inloggning…" state under the
  same harness. A manual browser click-through by the founder is the recommended next
  verification step before this UI is trusted, per this repo's own UI-verification standard.

## Remaining work for later Agent Runtime / terminal execution / self-upgrade phases

- Wire `MainAIExecutionResponse` into the actual chat/agent response path so a conversational
  claim of "I'm working on it" is structurally required to carry a real `job_id`.
- Expand `CAPABILITY_MANIFEST` deliberately, one capability at a time, each with its own job
  type, its own storage/lock story if it writes blobs, and its own test suite — never as a
  blanket unlock.
- A real terminal/shell execution capability is explicitly **not** part of this or any
  described near-term slice — it needs its own sandboxing/threat-model design before any code
  is written, per the founder's standing instruction.
- A real multi-tenant admin role (replacing the founder-only stand-in for `/admin/all`) if
  and when this system grows beyond a single privileged account.
- Push-based progress (WebSocket/SSE) if polling proves insufficient once there are multiple
  concurrent long-running job types.

## Migration / rollback notes

`alembic downgrade -1` from `0025` drops `mainai_job_proposals`, `mainai_job_events`, then
`mainai_jobs`, in that order (children before parent, matching their FK dependencies) — a
clean, reversible rollback with no data migration to reverse (this is a net-new schema, not an
alteration of existing tables). Verified locally: apply `0018→0025`, downgrade `-1`, upgrade
back to `head` — all three tables present and correctly shaped after the round trip.
