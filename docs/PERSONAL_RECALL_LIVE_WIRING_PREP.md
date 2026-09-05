# Personal Recall live-wiring preparation

Base: `0958322b6187e622269579487a13a9e0631a759d`, isolated on
`codex/universal-personal-recall`. No merge, push, PR edit, production registration,
chat/UI wiring, provider call, schema migration, or other-branch import.

## Implemented

- `routes_prep.build_recall_router` builds query/status HTTP handlers with a default-off
  gate that runs before service/auth dependencies. A trusted dependency must bind the
  canonical session and grant; HTTP bodies cannot supply owners or authorization contexts.
  The response serializes only the disclosure-filtered evidence handoff and receipt,
  never the internal response containing full source objects. Nothing registers this
  factory in `app.main`. Source-open HTTP awaits a server-owned evidence capability store.
- `RecallIndexWorker` persists identifier-only invalidations and per-source protected
  projections in a private local SQLite database. All seven event kinds are supported:
  new/edited message, new/deleted document, memory revoke/purge, knowledge supersession.
  Events are hints: fresh canonical data determines the result, not queued content or
  the event's asserted lifecycle. Delayed events cannot resurrect a deleted canonical row.
- A single transaction replaces one source projection and acknowledges its queue event.
  Duplicate IDs are idempotent; conflicting reuse is rejected. Competing workers serialize
  through SQLite's write lock. Authority is checked before work and again before writes.
  Pending invalidations hide the old source projection from reads until processing succeeds.
- `CanonicalSourceLoader` scopes existing owner-bound SQL adapters to a single message,
  document (record/chunks/versions), or memory source. It expires ORM identities before
  reading. Missing sources yield empty projections; saturated reads fail without replacing
  state. It requires a fresh canonical READ COMMITTED/RLS session supplied by the caller.
- Document candidate generation executes parametrized PostgreSQL `to_tsvector` /
  `plainto_tsquery` over existing chunk text. Both uploader and chunk owner predicates,
  deletion status, and project scope apply before result limiting. It returns internal
  candidate IDs, not evidence; final canonical authorization/currentness checks remain
  mandatory. It is not wired to query handlers or the retrieval engine.
- `personal-recall-evidence-v1` preserves structured truth/provenance/coverage fields.
  Project/conversation/intent/workspace hints narrow retrieval only. This is a proposed
  compatibility boundary for Operating Shell / Intent Objects, not a claim of compatibility
  verified against their unavailable branch. No executable intent or grant comes from it.

## Snapshot crypto

There is no reviewed AEAD/key hierarchy implementation in this checkout and no production
crypto dependency in backend requirements. The existing deterministic test protector is
explicitly **not encryption**. Worker storage refuses production construction, even if a
caller provides an arbitrary object implementing the protector protocol. Exercising it
requires `test_only=True`; do not use real personal content with that test mode.

The protection context binds format, owner, source family, source identifier, sequence,
protector version and key reference. Tests verify tampering/context mismatch. These are
contract tests, not an AEAD security review. The original `LocalRecallIndex` JSON format is
unchanged and still not an authenticated encrypted production snapshot.

## Recovery evidence and limits

A spawned child process exits during queue acknowledgement, after the projection SQL write
but before commit. Reopening the database recovers the journal, exposes no partial projection,
and successfully replays the unacknowledged event once. Tests also cover ordinary exceptions,
revocation before commit, duplicate delivery, competing workers, restart reads and tampering.
SQLite uses `synchronous=FULL`. This proves process-crash recovery on the test filesystem;
it does not establish power-loss behavior, multi-host operation, or malicious rollback safety.

The queue is a derived local inbox, not an atomic canonical outbox. No change producers or
scheduler are installed. Purge removes content from the logical projection; forensic erasure
of journals/backups is not established by these tests.

## Bugs fixed

- Query disclosure used its start timestamp for expiry validation. Real requests now read
  the clock again; explicit test clocks remain injectable.
- Canonical account lookup could retain an old session epoch in SQLAlchemy's identity map.
  It now refreshes the account row with `populate_existing=True`.
- Revocation during registry lookup could occur after the last authority check. Query and
  source-open now revalidate after registry work, before producing their return values.
- History/current permission changes without a version bump now invalidate retrieval.
- Known pending source invalidations no longer expose the old worker projection.

## Remaining activation blockers

**P0**

1. Reviewed AEAD/key hierarchy integration: key provisioning/rotation/revocation, protected
   durable generations/anti-rollback policy, and encrypted persistence/erasure proof.
2. Canonical transactional outbox or equivalent lossless producer coupling, including
   dependent memory invalidation. Current local enqueue cannot close the canonical-commit
   versus event-delivery crash window.
3. Trusted production grant/session dependencies and canonical content-version binding at
   disclosure. Registry checks establish existence/ownership, not atomic content equality
   across concurrent edits. The source-open facade accepts internal objects; never expose
   those objects/receipts as client-authoritative request inputs.

**P1**

1. Cross-document relationship dependency scheduling. The incremental version loader
   refuses sources with outgoing relationships until complete dependency refresh is
   available, leaving events pending and projections hidden rather than dropping edges.
   Within-document version chains are supported and tested.
2. Production queue retries/backoff, poison-event isolation, compaction, operational
   metrics, and multi-host coordination. A failing event currently blocks the owner's queue.
3. FTS query plans/latency budgets and reviewed GIN migration for scale. LIMIT bounds output,
   not PostgreSQL scan cost. Coverage is document chunks only; no ranking-quality claim.
4. Reviewed external Shell/Intent schema conformance and a server-owned source-open
   capability store. Hint support alone does not establish external interoperability.

## Validation

Run from the repository root using the backend test environment and a disposable PostgreSQL
instance with pgvector; the suite applies the real Alembic migrations and RLS policies:

```sh
PYTHONPATH=backend python -m pytest backend/tests/backend/personal_recall -q
ruff check backend/app/personal_recall backend/tests/backend/personal_recall
git diff --check
```

Validation result: 105 unique tests passed. The full suite passed 103 tests; after adding
the final two canonical cases, all 12 SQLAlchemy integration tests passed. Ruff and
`git diff --check` passed. The original 79 tests remain covered. New tests exercise gated HTTP projections, all event
kinds, canonical edit/delete/version/memory transitions, actual PostgreSQL FTS, RLS owner
attacks, stale session caches, concurrency and forced process-crash recovery. Existing
pytest-asyncio and naive-datetime deprecation warnings remain outside this change's scope.
