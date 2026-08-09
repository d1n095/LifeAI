# MainAI Runtime Truthfulness and Durable Job Foundation

Branch: `claude/mainai-job-runtime-foundation` (based on `claude/det-kommer-mer-879lcm`,
**not** on PR #31's still-unmerged S1A branch — see "Relationship to PR #31" below).

**INTEGRATION NOTE (`claude/mainai-job-runtime-integration`): the integration this document's
own "Relationship to PR #31" section calls for below has now been done.** PR #31 (S1A) and PR
#35 (durable backfill-run reporting) are merged into `claude/det-kommer-mer-879lcm`, whose real
Alembic head is `0025_memory_source_backfill_runs.py`. This branch's own two migrations were
renumbered onto that head: `0025_mainai_jobs.py` → `0026_mainai_jobs.py` (`down_revision`
changed from `0018` to `0025`) and `0026_mainai_job_integrity.py` → `0027_mainai_job_integrity.py`
(`down_revision` changed from `0025` to `0026`) — no SQL changed in either file, only revision
identity (see each file's own "INTEGRATION NOTE" in its docstring). The chain is linear,
`0001` → `0027`, exactly one head, verified against the real merged schema (not just an empty
database). **Every "migration 0025"/"migration 0026" reference in the rest of this document
below is the ORIGINAL, pre-integration numbering** (accurate history of what was designed and
reviewed under those names) — read "0025" as "0026" and "0026" as "0027" wherever this document
describes the job-runtime schema/integrity migrations from here down. The "Relationship to PR
#31" section immediately below is left unchanged as the historical record of what integration
required; it is no longer a to-do, it is what was actually done.

**FOUNDER RE-REVIEW ROUND (PR #36) — a fresh, independent review of this whole branch's actual
diff (not just the design) found one BLOCKER (a stale worker could still mutate a job after
losing its claim — no fencing token existed at all) plus several HIGH/MEDIUM findings. Every
one of them is fixed on this branch, described in detail in the new "Founder re-review round
(PR #36)" section below**, which supersedes several claims made further down in this document
that are now stale (most importantly: the truthfulness contract described in "Runtime
truthfulness contract" below IS now wired into `app/chat.py` — see that new section for exactly
what it guarantees and what it still cannot). Everywhere below that still says "not yet wired
into `app/chat.py`" is describing the state *before* this round; read the new section as the
current, correct state.

## Why this exists

**Goal, not yet a system-wide guarantee**: MainAI should never be able to claim it "started" or
"is working" on something unless a real, durable, independently-observable row exists — one a
human (or an automated recovery pass) can query, cancel, and see fail or complete on its own,
without trusting MainAI's own claim about its own state. Before this branch, nothing in the
codebase enforced that anywhere: an agent loop could say "I am reading your documents now" and
mean nothing more than an in-flight HTTP request with no persisted trace if the process died
mid-sentence.

This branch builds the *mechanism* for that guarantee — a durable job model, a worker that
claims and resumes it safely across restarts, and a Pydantic-level contract
(`MainAIExecutionResponse`) that makes it a validation error to construct a job-backed response
object without a real job ID behind it. **It is scoped to `/api/mainai/jobs` and its own
`corpus_review` job type, plus `app/chat.py` as of the founder re-review round (PR #36) — see
that section below.** `app/agent_orchestration.py` does not construct `MainAIExecutionResponse`
objects; it was reviewed and found to already refuse completion-from-a-bare-200 via its own
separate, pre-existing `AgentTask` state machine, so it was left unchanged rather than rewritten
outside this PR's scope. Read every claim below about what "MainAI can never do" as scoped to
the job-integrated and chat paths this branch actually touches, not as a description of the
system as a whole.

## Scope boundaries (what this branch is, and is not)

**Is:**
- A durable, owner-scoped job/event/proposal schema (migration 0025).
- A restart-safe claim/lease/heartbeat worker loop, sharing the existing worker process.
- One real job type, `corpus_review`: reads existing indexed documents, calls a real AI
  provider, produces reviewed-content *proposals* — never auto-approved knowledge.
- A founder-only Job API (create/read/list/cancel/retry + an explicitly-separate admin
  cross-owner read).
- The `MainAIExecutionResponse` contract object and `require_capability()` gate, wired into
  `app/chat.py` as of the founder re-review round (PR #36) — see that section below for exactly
  what this guarantees and what it does not. `app/agent_orchestration.py` does not construct
  these objects; see the same section for why.

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
- Not wired into the agent-orchestration flow (see above) — only `app/chat.py`. The
  Jobs/Activity frontend view added here is a standalone `/mainai/jobs` page for observing this
  job type specifically.

## Relationship to PR #31 — NOT mergeable in either order without integration work

This branch's base (`claude/det-kommer-mer-879lcm`) has Alembic head `0018`. PR #31's S1A
migrations (`0019`-`0024`, `MemorySourceUnit`/provenance work) exist only on PR #31's own
branch and are **not** touched, imported, or depended on here. Migrations `0025`/`0026` set
`down_revision = "0018"` — the real head of this branch's actual base.

**This is an Alembic side-branch, not an independent, mergeable-in-any-order chain.** If both
this branch and PR #31 merge as-is, the result is two divergent Alembic heads off `0018`
(`0018 → 0019 → ... → 0024` and `0018 → 0025 → 0026`), which Alembic cannot resolve without
either an explicit merge migration or a rebase. **This branch's own migration chain must be
made linear on top of PR #31 before either can be reviewed for merge against the other** —
concretely: after PR #31 merges, this branch rebases onto the new base, `0025.down_revision`
is updated from `0018` to whatever PR #31's actual final revision id is, `alembic heads` is
re-run to confirm exactly one head, and the full upgrade/downgrade/upgrade cycle is re-verified
from both an empty database and from the pre-integration production head. **No PR has been
opened for this branch, and none should be, until that integration step is done** — the two
chains are not independently reviewable as "mergeable in either order" the way, say, two
purely additive, non-overlapping migrations would be.

## Data model

Three tables (migration `0025_mainai_jobs.py`, integrity-hardened by
`0026_mainai_job_integrity.py` — see below), same RLS-per-table pattern as every other
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

### `mainai_job_events` — append-only execution-event history, enforced at the DB level

One row per lifecycle transition (`created`, `claimed`, `heartbeat`, `phase_changed`,
`progress_updated`, `cancel_requested`, `cancel_acknowledged`, `completed`, `failed`,
`cancelled`, `retry_scheduled`).

Migration `0025` originally left "append-only" as an application convention only — an
independent founder review correctly flagged that as insufficient for something meant to be
independent evidence of what MainAI actually did. Migration `0026` makes it a real DB-level
guarantee: a `BEFORE UPDATE OR DELETE` trigger (`mainai_job_events_deny_mutation()`) denies
every UPDATE unconditionally, no exceptions, and denies DELETE unless the deleting
transaction has explicitly set `app.mainai_job_erasure_in_progress = 'on'` — the only thing
that ever sets that flag is `erase_own_mainai_job_children()`, a narrow
`SECURITY DEFINER` function that is the sole legitimate deletion path (account erasure, see
below). `mainai_app` additionally has `UPDATE`/`DELETE`/`TRUNCATE`/`REFERENCES`/`TRIGGER`
revoked on this table outright — the trigger is defense in depth on top of that, proven by
`test_mainai_job_events_trigger_denies_update_even_for_a_privileged_connection`, which shows
even the superuser/migration connection (which otherwise bypasses RLS and holds every
ordinary privilege) still cannot UPDATE a row, because triggers fire regardless of role.

### `mainai_job_proposals` — a job's output, never a promoted claim, immutable except one transition

`corpus_review`'s findings land here, each with `source_document_id`/`source_chunk_id`
provenance, `proposal_text`, and a `status` that starts `proposed` and can only ever become
`dismissed`. Nothing in this schema or its application code can turn a proposal into a
`KnowledgeClaim` automatically — `test_run_corpus_review_job_never_promotes_a_proposal_to_a_
knowledge_claim` asserts this directly against a real database.

Migration `0026` adds a `BEFORE UPDATE OR DELETE` trigger (`mainai_job_proposals_guard_
mutation()`) that permits exactly one mutation, ever: `status: 'proposed' -> 'dismissed'`
with every other column byte-for-byte unchanged. Never the reverse (`dismissed -> proposed`),
never an edit to `proposal_text`/`source_document_id`/`job_id`/`owner_id` after the fact.
`mainai_app` keeps ordinary `UPDATE` (needed for that one transition) but has `DELETE`/
`TRUNCATE`/`REFERENCES`/`TRIGGER` revoked — deletion, like events, only ever happens through
`erase_own_mainai_job_children()`.

### Composite owner integrity — child rows can no longer point at a different owner's job

Before migration `0026`, `mainai_job_events`/`mainai_job_proposals` had two *independent* FKs
(`job_id -> mainai_jobs.id`, `owner_id -> users.id`) with nothing tying them together. RLS
only checks a row's own `owner_id`, so an attacker who knows a victim's `job_id` (job IDs
aren't secret — they appear in URLs) could INSERT a row with `owner_id = <attacker>` (passes
RLS's `WITH CHECK`, since it's the attacker's own session) but `job_id = <victim's job>` — a
row visible to the attacker, but logically attached to someone else's job. `mainai_jobs` now
has `UNIQUE(id, owner_id)`, and both child tables carry a real composite
`FOREIGN KEY (job_id, owner_id) REFERENCES mainai_jobs (id, owner_id)` — a job/owner pair
that doesn't match a real row is now a constraint violation, not a silently-accepted,
RLS-hidden-from-the-victim row. `test_mainai_job_events_composite_fk_rejects_owner_mismatch`
and the equivalent proposals test prove this via a direct SQL INSERT under RLS, not just
through the service layer.

`owner_id` is denormalized onto both child tables rather than resolved via a join in the RLS
policy — the same choice `document_chunks.owner_id` already makes relative to `documents`,
for the same reason: every RLS policy in `app/rls.py` is a single-column, non-join predicate,
and mixing join-based and column-based policies in one schema is a real, easy-to-miss
inconsistency for a future reviewer.

### `erase_own_mainai_job_children()` — the only deletion path, and no owner parameter to attack

A narrow `SECURITY DEFINER` function (migration `0026`), zero arguments, that sets
`app.mainai_job_erasure_in_progress = 'on'` (satisfying the two tables' delete-deny triggers)
and deletes `mainai_job_proposals` then `mainai_job_events` **for the calling session's own
owner**, derived from `current_setting('app.current_user_id', true)` — the same session GUC
every RLS policy in `app/rls.py` already trusts — and denies outright (`RAISE EXCEPTION`) if
that setting is unset. `mainai_app` holds `EXECUTE` on this function and nothing else that
could delete these rows — granted by `app/rls.py`'s `apply_mainai_job_runtime_privileges()` at
boot, not by the migration itself (see "Portability" below) — see "Account erasure" below for
the only caller. `mainai_jobs`
itself is not locked down this way (it's a live, legitimately-mutable job-state row, not an
append-only log) — `mainai_app` keeps ordinary `DELETE` on it, and account erasure deletes it
directly after the children.

**This function's first draft was a real cross-owner deletion vulnerability, found by a
second independent founder review round and fixed before this branch had ever been opened as
a PR.** The first draft was `erase_mainai_job_children_for_owner(target_owner_id uuid)`:
`SECURITY DEFINER`, `EXECUTE` granted to `mainai_app`, deleting `WHERE owner_id =
target_owner_id` — with no check anywhere that `target_owner_id` matched the calling
session's own identity. Because the function is `SECURITY DEFINER`, its `DELETE`s run with
the function *owner's* privileges, not the caller's, so per-table RLS was not actually a
sufficient boundary around it: any authenticated session could have called `SELECT
erase_mainai_job_children_for_owner('<some other owner's uuid>')` and erased that owner's
entire event/proposal history, RLS notwithstanding. The fix removes the parameter entirely —
there is no argument left for a caller, buggy or malicious, to ever get wrong — and derives
the owner exclusively from the trusted session context instead. Proven directly by
`test_erase_own_mainai_job_children_removes_only_the_calling_owners_rows`, `test_erase_own_
mainai_job_children_has_no_owner_parameter_to_attack` (asserts exactly one zero-argument
overload exists in `pg_proc` and that no `%_for_owner%`/`%_admin%` variant exists at all), and
`test_erase_own_mainai_job_children_denies_a_session_with_no_auth_context`.

No cross-owner/admin variant of this function was built. Nothing in this codebase today has a
legitimate reason to erase another owner's MainAI job data outside that owner's own
account-deletion flow, and adding one with no real caller would reintroduce exactly the kind
of speculative, unreviewed privileged surface this fix round exists to close, not add one. If
a genuine cross-owner maintenance need appears later, it gets its own function, its own
review, and — per the founder's explicit instruction — it must never be `EXECUTE`-granted to
`mainai_app`.

### Account erasure

`app/routers/account.py`'s `delete_account()` explicitly deletes `mainai_job_proposals`/
`mainai_job_events` (via the function above) and then `mainai_jobs` for the deleted user,
inside the same transaction as every other table's deletion there — matching this codebase's
established convention of explicit, ordered per-table deletion rather than relying on
`ON DELETE CASCADE` (the CASCADE constraints on the FKs above remain as a referential-
integrity backstop only, not the intended deletion mechanism; see `account.py`'s own
docstring for why explicit deletion is this codebase's standard, not the exception).

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

This contract is now wired into `app/chat.py`'s response path (founder re-review round, PR #36)
— see "Founder re-review round (PR #36)" below for exactly what that integration guarantees and
what it does not.

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

**Restart safety**: `app/jobs/handlers/corpus_review.py` processes one document per unit of work, commits
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

`app/jobs/handlers/corpus_review.py` receives existing, already-indexed `Document` references
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

## `message_sequence_backfill`: the second job type (S1B) — and the first with no AI at all

Added by S1B (migration `0030`, `app/jobs/handlers/message_sequence_backfill.py`). It numbers the
`messages` rows that predate migration 0030 — see
`docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §4.9 for why `messages.sequence_number` exists at
all, and `app/rag/backfill/message_sequence.py` for the numbering rule.

It is worth reading as the reference example for a NON-AI capability on this runtime, because
it differs from `corpus_review` in three reviewed ways:

1. **No provider dependency.** `_CAPABILITY_PROVIDER_ROLE["message_sequence_backfill"]` is an
   explicit `None`, not a missing entry. `None` means "reviewed: this capability genuinely
   needs no AI provider", so `get_capability_status()` reports it configured and available
   even with zero providers set up. A capability simply *forgotten* from that dict still fails
   closed, exactly as before — the two cases are deliberately distinguishable, and
   `tests/backend/test_message_sequence.py` asserts both directions. This is the founder's
   "the system must keep working without AI wherever architecturally possible" rule made
   enforceable rather than aspirational: numbering the founder's own message history must not
   become unavailable because a model key is missing.
2. **It modifies existing rows**, which `corpus_review` never does. Its
   `_CAPABILITY_WRITE_PROFILE` entry says `modifies_existing_data: True` and
   `writes_new_records: False`, and that is reported to the founder rather than inferred. The
   modification is strictly `NULL -> an ordinal`; migration 0030's
   `messages_deny_sequence_number_rewrite` trigger makes overwriting an already-assigned
   ordinal impossible at the database level, not merely unlikely in this module's code.
3. **It takes no `input_refs`.** Its scope is "every one of this owner's still-unnumbered
   messages", derived at execution time. `create_job()` REJECTS non-empty `input_refs` for
   this job type (422) rather than accepting and ignoring them — accepting refs the executor
   will never read would let a caller believe it had narrowed the job's scope when it had not,
   which is precisely the kind of quiet mismatch between what a job claims and what it does
   that the runtime-truthfulness contract exists to prevent.

One conversation is one unit of work: the lease is renewed and `cancel_requested` re-checked
before each, and the fenced progress write shares a transaction with the numbering it
describes, so a worker that loses its lease mid-conversation writes nothing at all (not even
the numbering). A run that stops at its `MAX_CONVERSATIONS_PER_RUN` cap, or that skipped a
conversation as conflicting, says so in `public_message` together with exactly how much work
remains — it never reports completion for work it did not do.

## Job API

`app/routers/mainai_jobs.py`, `prefix="/api/mainai/jobs"`, every route gated by
`require_founder` (this system is founder-only today — see `app/deps.py`):

| Route | Purpose |
|---|---|
| `POST ""` | Create a job. Rate-limited (`rate_limit_default_per_minute`, currently 120/min). 409 on unknown capability, 422 on invalid `input_refs`. |
| `GET ""` | List the caller's own jobs (paginated, RLS-scoped). |
| `GET "/{job_id}"` | Job detail + its event history. 404 (never 403) for a job that exists but isn't the caller's — RLS makes it genuinely not exist from this session's point of view. |
| `GET "/{job_id}/proposals"` | The job's proposals. |
| `POST "/{job_id}/cancel"` | Idempotent cancel request. Rate-limited (`rate_limit_default_per_minute`). 409 if the job is already terminal. |
| `POST "/{job_id}/retry"` | Retry a failed job within its retry budget. Rate-limited (`rate_limit_default_per_minute`). 409 if cancelled, completed, or budget exhausted — retry can **never** silently override a cancellation. |
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
- **Composite owner integrity on child rows** (migration `0026`): `mainai_jobs.UNIQUE(id,
  owner_id)` plus a composite FK on both child tables closes the "known job_id, mismatched
  owner_id" gap described in "Data model" above — proven via direct SQL INSERT under RLS
  (`test_mainai_job_events_composite_fk_rejects_owner_mismatch` and the proposals
  equivalent), not just through the service layer.
- **DB-enforced append-only event log, not just convention** (migration `0026`):
  `mainai_job_events` denies every UPDATE unconditionally and denies DELETE outside an
  authorized erasure, enforced by both a revoked grant AND a trigger that still fires for a
  privileged connection (`test_mainai_job_events_trigger_denies_update_even_for_a_privileged_
  connection`). `mainai_job_proposals` permits exactly one mutation ever
  (`proposed -> dismissed`, no other column changed) and denies everything else, including
  the reverse transition (`test_mainai_job_proposals_rejects_the_reverse_transition`).
- **Boot-persistent privilege lockdown, verified not just asserted**: `scripts/
  ensure_app_role.py` unconditionally re-grants `ALL PRIVILEGES` to `mainai_app` on every
  container boot, before Alembic even runs — a REVOKE applied once, at migration time, would
  be silently undone by the next restart (the exact bug class documented as the Pass 12
  incident in `docs/BRANCH_REGISTRY.md`, for a different table). `app/rls.py`'s
  `apply_mainai_job_runtime_privileges(engine, require_complete=True)` re-asserts the
  `mainai_job_events`/`mainai_job_proposals` lockdown on every boot, called from
  `app/main.py`'s startup right after `apply_rls()`, with `engine` expected to be `app/db.py`'s
  `migration_engine` — the real superuser/admin connection — because the policy reads its own
  `expected_owner` as `current_user` on that same connection rather than hardcoding a role
  name. A first version of this policy (Pass 16) only checked that each function's owner
  *wasn't* `mainai_app`, which a second founder review correctly pointed out proves nothing
  about who the owner actually *is* — a function reassigned to any other unexpected role would
  have passed silently. Fixed: it is not a fire-and-hope enforcement step, and it no longer
  checks by exclusion. After issuing its REVOKE/GRANT statements, in the same transaction, it
  re-queries Postgres's own catalogs (`pg_tables`, `pg_proc`, `pg_language`, `pg_roles`,
  `information_schema.routine_privileges`, plus `has_table_privilege`/`has_function_privilege`
  for *effective* grants — see below) and confirms: `mainai_jobs`/`mainai_job_events`/
  `mainai_job_proposals` are owned by exactly `expected_owner` (never `mainai_app`); each
  function's owner is exactly `expected_owner` and that owner actually holds `SUPERUSER` or
  `BYPASSRLS` (an owner that can't itself bypass FORCE RLS can't make the function do so
  either); each function's exact argument signature (`pg_get_function_identity_arguments`, not
  just an argument *count*), return type, `SECURITY DEFINER` flag, `search_path`, and language
  match; no unexpected second overload exists; `PUBLIC` has no `EXECUTE` on any of them; and
  mainai_app's table/function grants match exactly — computed via `has_table_privilege`/
  `has_function_privilege` (Postgres's own *effective*-privilege check, following role
  membership) rather than a raw `information_schema.role_table_grants` filter, which would
  only see grants made directly to `mainai_app` by name and miss one reaching it indirectly
  through membership in some other granted role. Raises (`require_complete=True`, the real
  startup default) or logs a warning (`require_complete=False`) on any drift, with the whole
  enforce-then-verify pass — including the enforce phase's own REVOKE/GRANT statements — rolled
  back atomically on failure since it all runs inside one `engine.begin()` transaction
  (`test_apply_mainai_job_runtime_privileges_rolls_back_the_enforce_phase_too_on_failure` proves
  this directly, from a separate connection, rather than trusting that a caught exception
  implies nothing persisted). `test_apply_mainai_job_runtime_privileges_passes_against_the_
  real_migrated_state`, `..._detects_a_wrong_function_owner`, `..._detects_an_owner_without_
  bypassrls_or_superuser`, `..._detects_mainai_app_as_table_owner`, `..._detects_an_unexpected_
  overload`, and `..._survives_reboots_blanket_grant_all` prove each of these checks against a
  real database, not a mock.
- **The erasure-in-progress flag is not itself an authorization boundary.**
  `app.mainai_job_erasure_in_progress` only silences the append-only triggers' DELETE denial —
  it grants no privilege on its own. `test_mainai_app_cannot_delete_events_even_with_the_
  erasure_flag_manually_set` proves that a session connected as `mainai_app` which manually
  sets that GUC to `'on'` still cannot `DELETE` from `mainai_job_events` directly, because
  `mainai_app` has no table-level `DELETE` privilege on it at all — the only way past both
  layers together is through `erase_own_mainai_job_children()` itself.
- **Account erasure genuinely removes this data**, not just anonymizes it:
  `test_account_deletion_removes_mainai_job_data` drives the real `DELETE /api/account`
  endpoint end to end and confirms `mainai_jobs`/`mainai_job_events`/`mainai_job_proposals`
  rows are gone afterward, via the superuser connection (bypassing RLS, so a false pass from
  RLS merely hiding the rows is not possible).

## Threat model: the `/admin/all` endpoint

`GET /api/mainai/jobs/admin/all` deliberately bypasses per-owner RLS (it runs on the
superuser/migration connection, like the worker's own claim step, since it must see jobs
across every owner). The only gate in front of it is `require_founder` — the exact same
dependency every other route in this founder-only system already uses. **This is not a real
second privilege tier**: in a system with more than one privileged role, this endpoint would
need an actual admin-role check, not just "is this the founder." It is safe *today* only
because the founder is the sole account with any elevated access at all. This is flagged here
explicitly as a known, accepted limitation — not something to silently outgrow later.

## Founder re-review round (PR #36): lease fencing, idempotency, and truthfulness contract wiring

A fresh, independent re-review of this branch's actual merged diff (not the design doc) found
one BLOCKER and several HIGH/MEDIUM findings. All are fixed here, each with its own regression
test; migration `0028` carries the schema changes.

**BLOCKER — no lease fencing.** Before this round, every worker-driven write against a claimed
job (`renew_mainai_job_lease`, `update_progress`, `mark_completed`/`failed`/`cancelled`,
`record_claimed`) trusted only `worker_id` + `status = 'running'`. `worker_id` alone
(`app/worker.py`'s `_worker_id()`, a hostname-or-configured string) can repeat across a process
restart, so a worker whose lease had already expired and been reclaimed by someone else could
still successfully renew, report progress, or mark the job completed/failed as if it still held
the claim — directly racing the new claimant's writes. Fixed with a fencing token,
`lease_generation` (migration `0028`, `mainai_jobs.lease_generation integer NOT NULL DEFAULT 0`):
`claim_next_mainai_job()` bumps it by exactly 1 on every claim AND every reclaim, and every
worker-driven write now goes through a single guarded UPDATE pattern
(`app/rag/mainai_jobs_service.py::_guarded_job_write`) —
`WHERE id = :job_id AND locked_by = :worker_id AND lease_generation = :lease_generation AND
status = 'running'` — in the SAME statement as the write itself. Zero rowcount raises
`JobLeaseLostError`, updating nothing; the caller (`app/rag/corpus_review_job.py`) stops
immediately on that error and makes no further writes. Proven with a real two-worker race
(`test_stale_worker_is_rejected_by_every_write_after_a_reclaim`): worker A claims, its lease is
force-expired, worker B reclaims, and EVERY one of worker A's subsequent write attempts
(renew/progress/record_document_reviewed/record_document_skipped/mark_completed/mark_failed/
mark_cancelled) is rejected while worker B completes normally with exactly one `completed`
event — run clean 20/20 times.

**HIGH — `create_job()` was not safely idempotent under real concurrency.** The old
select-then-insert had a classic TOCTOU race: two requests with the same `(owner_id,
idempotency_key)` could both pass the SELECT before either committed its INSERT, and the loser
got an unhandled `IntegrityError` instead of the existing job. Fixed with the same
SAVEPOINT + real INSERT + catch-the-exact-constraint-violation + rollback-to-savepoint +
fresh-SELECT pattern already established by `app/rag/memory_source.py`'s
`get_or_create_memory_source_unit()`. Proven with two real threads and two real DB sessions
(`test_create_job_concurrent_same_owner_and_key_is_race_safe`, clean 20/20): both calls
succeed, both return the same `job_id`, exactly one row and one `created` event exist.

**HIGH — the truthfulness contract existed but nothing actually used it.** Before this round,
`MainAIExecutionResponse`/`CAPABILITY_MANIFEST` were built and tested in isolation; `app/
chat.py`, the highest-traffic MainAI surface, never constructed one. A chat reply's free text
could still say "I'm working on it in the background" with nothing structurally stopping it.
Fixed with three layers, deliberately not relying on any single one:
1. **Primary — the model is told the truth.** `chat.py`'s `SYSTEM_PROMPT` now explicitly states
   that the reply IS the model's entire work on the request, that no background job or ongoing
   process exists on this path, and that a real durable job must be started by the founder
   through Jobb & Aktivitet, never claimed as already running.
2. **Structural — `build_answer_response()`.** Every chat reply is now passed through this new
   function before being persisted or returned; it constructs a real `MainAIExecutionResponse`
   with `mode=answer, job_id=None` — the one and only shape a plain chat reply can ever
   truthfully be — so `MainAIExecutionResponse`'s own Pydantic validator (job_id forbidden for
   `answer`) is now a real, exercised guarantee about this call site, not an unused shape.
3. **Secondary, explicitly not the only protection — `sanitize_unverified_execution_claims()`.**
   A regex-based (not bare-substring), reviewed ruleset covering seven claim categories in
   Swedish and English (working in the background; will get back to you later; monitoring it;
   has already reviewed everything; is done/finished; has started the job; will notify you when
   done — see the function's own module-level comment in
   `app/mainai_runtime_contract.py` for the exact patterns). **Fourth founder re-review round
   (same PR): rewritten from append-only to sentence-level REPLACEMENT.** The original version
   only ever appended a `[MainAI-obs: ...]` correction after the model's own text, so a reply
   could (and, once tested end-to-end, demonstrably did) show the user "Jag arbetar med det i
   bakgrunden." immediately followed by a notice saying no such work exists — both claims
   visible in the same message at once, which the founder classified HIGH: a self-contradictory
   message is still a misleading one. This version splits the message into sentences and
   REPLACES the specific sentence containing the claim with a fixed truthful sentence, leaving
   every other sentence of the same message untouched — the false claim itself is gone from
   what the user reads, not just followed by a disclaimer. Still deliberately not a general
   sentiment/intent classifier (unreviewable, prone to both false positives and false
   negatives) and deliberately never the ONLY defense, per the founder's explicit instruction
   against keyword-hack-only protection. **Also found and fixed during this same round's own
   mandated self-review** (not a founder-review finding): the first draft of the "is
   done/finished" category matched bare generic subjects (`jag är`/`det är`/`i'm`/`it's`/`this
   is` + `klar`/`done`), which over-matched ordinary sentences with nothing to do with a
   background job (e.g. "Jag är klar med kaffet" / "It's done!") — a real violation of "ordinary
   informational answers must be left unchanged." Fixed by requiring an explicit job/task/work
   noun in that category's patterns instead of a bare pronoun.

Proven end to end through the real `/api/chat` HTTP endpoint, not just unit tests of the
contract functions themselves:
`test_unverified_execution_claim_from_the_model_is_sanitized_through_the_real_endpoint` (and its
English and retry-path siblings) make a fake provider return exactly such a claim and assert the
false claim text is ABSENT from both the HTTP response body and the persisted `Message` row;
`test_ordinary_reply_without_an_execution_claim_is_left_untouched` proves normal replies are
never modified.

**What this now actually guarantees**: a chat reply can never be *structurally* classified as
`execution_started`/`status`/`completed` (those require a real `job_id`, impossible to
construct on this path); the model is told not to make unverified execution claims in free
text; and if it does anyway, the false claim itself — not just an accompanying disclaimer — is
removed from what the user reads.
**What this does not (yet) guarantee**: `app/agent_orchestration.py`'s own agent-task loop was
reviewed and found to already refuse to report completion from a bare HTTP 200 — it has its own
pre-existing `AgentTask`/`AgentTaskEvent` state machine requiring recorded task results — so no
change was made there; it was not rewritten to construct `MainAIExecutionResponse` objects
itself, since that would be an unreviewed rewrite of a stable subsystem outside this PR's scope.
A determined model could still phrase an execution claim in words this pattern list doesn't
anticipate; the structural (2) and prompt-level (1) defenses are what actually bound the
*classification* of the response, not the sanitizer's coverage of every possible phrasing.

**MEDIUM — capability manifest was a static membership check, not runtime-aware.** Before this
round, `require_capability()` only checked `job_type in CAPABILITY_MANIFEST` — a capability
could report "available" with zero AI providers configured, only failing later when the worker
actually tried to run it, creating a job that was certain to fail from the moment it was queued.
Fixed with `get_capability_status()`/`CapabilityStatus`, distinguishing `implemented` (in code)
from `configured` (a provider is actually usable — the same cheap, no-network-call
`provider.is_configured()` check `app/providers/registry.py`'s `resolve_chat_chain()` already
uses) from `currently_available` (both). `require_capability()` now fails closed with a
machine-readable `reason` (`not_implemented` vs `not_configured`) BEFORE any job row is created,
mapped to a 409 with that reason by `app/routers/mainai_jobs.py`.

**MEDIUM — DB-level state invariants.** Migration `0028` adds four CHECK constraints:
`progress_current <= progress_total` (when a total exists), a terminal status requires
`completed_at` set and forbids it when non-terminal, `started_at` required once a job leaves
`queued`, and `retry_count <= max_retries`. These caught a real, pre-existing bug during this
round: `retry_job()` transitioned a job back to `queued` without resetting `completed_at` to
`NULL`, which the new terminal-status constraint immediately rejected — fixed alongside adding
the constraint, with `progress_current`/`progress_total`/`current_phase` also now reset on
retry, fulfilling that function's own long-standing (previously unfulfilled) docstring claim.

**MEDIUM — truthful corpus-review completion semantics.** Before this round, a document deleted
mid-run, a document with no reviewable content, or a single document's provider failure could
each distort "reviewed N of N" into a claim that was not quite true, and a provider failure for
one document aborted the WHOLE job even though the other documents were never at fault. Fixed:
`app/rag/corpus_review_job.py` now tracks reviewed/skipped-deleted/unavailable/provider-failed
counts separately against the job's fixed snapshot total, writes a `document_skipped` event
(migration `0028` adds this event type) with a closed-vocabulary `reason` for every
non-reviewed outcome, and the completion message reports the real breakdown
(`"Reviewed 1 of 3 document(s) ... 2 not reviewed (1 deleted, 1 failed)."`) instead of one
number that blurs "actually reviewed" with "counted as done." A provider failure for one
document is now recorded as that document's own skip and the loop continues; only a genuinely
unexpected (non-provider, non-per-document) exception still fails the whole job via
`mark_failed`. Proven with `test_run_corpus_review_job_mixed_outcomes_in_one_run` (three
documents, three different outcomes in one run) and
`test_run_corpus_review_job_fails_the_whole_job_on_a_genuinely_unexpected_error` (the other
branch of that same split).

**HIGH — account export omitted all MainAI job data.** `app/rag/account_export.py` now exports
`mainai_jobs`/`mainai_job_events`/`mainai_job_proposals`, owner-scoped, deterministically
ordered, same convention as every other section — `EXPORT_SCHEMA_VERSION` bumped to `3`. Event
`detail`/proposal `proposal_text` are exported as-is: every event type this table can contain
is already restricted to a closed, safe vocabulary at write time (see `_PUBLIC_ERROR_MESSAGES`/
`document_skipped`'s reasons above), so there is nothing left to sanitize on the way out.

**LOW — rate limiting and pagination.** `POST /{job_id}/cancel` and `POST /{job_id}/retry` now
carry the same `rate_limit_default_per_minute` limiter `POST ""` already had (neither had any
before). The `/admin/jobs` frontend page now paginates both the founder's own list and the
admin cross-owner list (20 rows/page, Föregående/Nästa), instead of fetching every row
unbounded.

### Fourth founder re-review round (same PR): the H1 truthfulness fix above, plus six MEDIUM fixes

The HIGH sanitizer rewrite (append → sentence-level replacement) is documented in full under
"HIGH — the truthfulness contract existed but nothing actually used it" above. This round also
fixed six MEDIUM findings from the same review, all in the same round per the founder's explicit
priority ordering:

- **M1 — pagination was not stable across pages.** `list_jobs()` and `/admin/all`'s raw SQL both
  ordered by `created_at DESC` alone; two jobs created within the same timestamp (realistic under
  fast/automated creation) could shuffle order between page fetches, producing a duplicate or
  missing row across pages. Both now order by `created_at DESC, id DESC` — `id` (a UUID, unique,
  immutable) as a tiebreaker makes the ordering total and deterministic, and both endpoints use
  the identical ordering so they can never disagree with each other.
- **M2 — a stale `/admin/jobs` poll response could overwrite a newer page's state.**
  `refreshJobs()` is called both on page/scope change and every poll tick, with no guarantee
  those requests resolve in the order they were sent. A monotonic `useRef` counter, bumped at the
  start of each call and captured into that call's closure, lets a response detect it is no
  longer the most recent request and discard itself instead of overwriting what the user is
  currently looking at. Proven with a dedicated Playwright test
  (`e2e/mainai-jobs-pagination.spec.ts`) that deliberately delays page 1's poll response past
  page 2's real response and confirms page 2's data survives.
- **M3 — `sandbox_only`/`production_prohibited` were reported but never enforced.** These flags
  existed as pure metadata on `CapabilityStatus`; nothing ever read them to actually block
  execution. Folded into `get_capability_status()` (checked against
  `get_settings().environment == "production"`), the one function both `create_job()` (creation)
  and, as of this round, `app/worker.py::process_claimed_mainai_job()` (execution, re-checked
  immediately before dispatch) call — so job creation and worker execution can never apply a
  different policy from each other, and a capability that becomes blocked between creation and
  execution is still caught.
- **M4 — nothing defended against `progress_current` decreasing.** Migration `0028`'s CHECK
  constraints can only validate a single row's new values, never compare against the row's
  previous value, so decreasing progress needed a trigger, not another CHECK. Migration `0029`
  adds a `BEFORE UPDATE` trigger rejecting any decrease EXCEPT the one legitimate case: the
  `failed → queued` retry transition resetting to exactly `0`. Composes with, does not replace,
  the pre-existing lease-fencing `WHERE` clause: a stale worker's write already matches zero rows
  before the trigger ever runs for that row.
- **M5 — the mid-run lease-loss rollback guarantee had no test proving it end to end.** Added
  `test_run_corpus_review_job_rolls_back_the_proposal_when_lease_dies_between_provider_call_and_commit`,
  which injects the "another worker reclaims the lease" side effect INSIDE the mocked provider
  call's own return, landing precisely between "provider call succeeded" and the guarded
  progress/proposal commit inside the real `run_corpus_review_job()` — not just at the lower
  service-function level. Confirms `JobLeaseLostError`, a full rollback, and exactly one proposal
  once the job actually finishes under the new worker.
- **M6 — idempotency semantics for a reused key with a different payload were undefined.**
  Previously, any existing job under `(owner_id, idempotency_key)` was returned unconditionally,
  regardless of whether the new call's `job_type`/`input_refs` actually matched. Added
  `IdempotencyConflictError` (409, `reason: "idempotency_conflict"`) raised when a
  `_canonical_request_fingerprint()` comparison (order-independent JSON of `job_type` + sorted
  `input_refs`) shows the new request differs from the existing job's own. The idempotency lookup
  now runs BEFORE `require_capability()`, so a replay under an existing key is recognized as a
  replay (or a conflict) before any capability check of the new call's parameters runs — closing
  a case where a client-side bug changing `job_type` under a reused key could otherwise surface a
  misleading `CapabilityUnavailableError`. The pre-existing SAVEPOINT-based concurrency safety
  (`test_create_job_concurrent_same_owner_and_key_is_race_safe`) still holds with the fingerprint
  check added.

Also fixed during this round's own mandated self-review (not itself a founder-review finding):
the "is done/finished" sanitizer category's first draft matched bare generic subjects (`jag
är`/`det är`/`i'm`/`it's`/`this is` + `klar`/`done`), over-matching ordinary sentences with
nothing to do with a background job. See the HIGH section above for the fix.

## Known limitations

- The runtime-truthfulness contract is now wired into `app/chat.py` (see "Founder re-review
  round (PR #36)" above for exactly what that guarantees). `app/agent_orchestration.py` was
  reviewed and found to already refuse completion-from-a-bare-200 via its own pre-existing
  `AgentTask` state machine; it was deliberately not rewritten to construct
  `MainAIExecutionResponse` objects itself in this round, to avoid an unreviewed rewrite of a
  stable subsystem outside this PR's scope.
- `/admin/all` is founder-gated, not role-gated (see threat model above).
- **This branch is not yet mergeable against PR #31 in either order** without integration
  work (rebase + `down_revision` update) — see "Relationship to PR #31" above. No PR has been
  opened for this branch for exactly that reason.
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

- `app/chat.py` is now wired (see "Founder re-review round (PR #36)" above). A future pass
  could extend the same structural wiring to `app/agent_orchestration.py` directly (constructing
  `MainAIExecutionResponse` objects there too) rather than relying on its own separate, already-
  reviewed state machine — not required for truthfulness today, but would unify both surfaces
  under one contract object.
- **`app/routers/workbench.py` (`/api/workbench/analyze`) and `app/agent_orchestration.py`
  are the explicit, named NEXT truthfulness surfaces — not a hidden follow-up.** Both call
  `chat_with_fallback()` directly and return the model's free text to a human, the same shape
  of risk `sanitize_unverified_execution_claims()`/`build_answer_response()` now guard on
  `app/chat.py`; neither goes through either function today. This PR's diff touches neither
  file. Recorded here explicitly, by name, so the next pass has a stated target instead of this
  being rediscovered from scratch.
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

`0026_mainai_job_integrity.py`'s `downgrade()` reverses everything it adds in the opposite
order: drops the `erase_own_mainai_job_children()` function, drops both triggers and their
functions, then drops the composite FKs and the `UNIQUE(id, owner_id)` constraint. It does
**not** touch any `mainai_app` grant — the migration itself never grants anything to
`mainai_app` in the first place (see "Portability" below), so there is nothing for its
downgrade to restore. Verified locally: apply `0025→0026`, downgrade `-1`, upgrade back to
`head` — all three tables, both triggers, both composite FKs, and the erasure function present
and correctly shaped after the round trip.

### Portability: this migration never references the `mainai_app` role

A second, independent bug found in this migration's first draft, alongside the cross-owner
vulnerability above: it issued `GRANT`/`REVOKE ... TO/FROM mainai_app` directly, which fails
with `role "mainai_app" does not exist` on a bare database that runs `alembic upgrade head`
before `scripts/security/ensure_app_role.py` has ever provisioned that role — a scenario PR #31's own
migrations are already careful to avoid. Fixed by removing every `mainai_app`-specific
statement from the migration; the only privilege statements it still issues are `REVOKE ALL
... FROM PUBLIC` on the functions it creates, which needs no named role to exist (`PUBLIC` is
a pseudo-role that always exists). All `mainai_app`-specific grants — `SELECT`/`INSERT` on the
two child tables, `EXECUTE` on `erase_own_mainai_job_children()`, and nothing else — now live
entirely in `app/rls.py`'s `apply_mainai_job_runtime_privileges()`, applied and *verified* at
application boot, after both Alembic and `ensure_app_role.py` have run. A full
`upgrade head` → `downgrade -1` → `upgrade head` → `downgrade base` → `upgrade head` round
trip was re-run against a database, and a source-level grep confirms zero executable
`mainai_app` references remain in the migration file (only prose in its docstring mentions the
role by name). A truly role-absent Postgres *cluster* could not be constructed in this shared
dev environment to additionally prove the round trip empirically against a database where the
role has never existed anywhere in the cluster — Postgres roles are cluster-level, not
per-database, and a `mainai_app` role created earlier in this same session for unrelated
scratch-database testing persists cluster-wide. The source-level grep is the decisive,
environment-independent guarantee; this limitation is recorded here rather than overclaimed as
a full empty-cluster test.

**Still outstanding before this branch is PR-ready** (see "Relationship to PR #31"): a full
upgrade/downgrade/upgrade cycle from an empty database AND from a pre-integration production
head, run again after this branch's `down_revision` is rebased onto PR #31's actual final
revision — not yet done, because that rebase itself hasn't happened yet.
