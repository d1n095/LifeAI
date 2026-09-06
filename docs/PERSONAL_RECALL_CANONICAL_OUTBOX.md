# Personal Recall canonical outbox propagation

This round starts at `7015fc0a9daa60eecb385b66b1565aa64c6f3b79` and adds migration `0070`.
It remains an isolated preparation change: no route is registered, no chat/UI path is wired,
Claude's branch is not imported, and #245 is untouched.

The canonical tables now carry a monotonic `recall_generation`. Database triggers emit minimal
routing rows into `personal_recall_outbox` in the same transaction as canonical INSERT,
UPDATE, DELETE, lifecycle transition, or supersession relationship mutation. Events contain
owner, source class/id, generation, event type, timestamp, transaction id and bounded routing
metadata; they never contain private source bodies. The outbox is owner-scoped with forced RLS
and runtime SELECT only. Trigger SQL is schema-qualified because memory lifecycle functions
run as SECURITY DEFINER with `search_path=pg_catalog`.

The consumer converts outbox rows to `SourceChange` hints. CanonicalSourceLoader performs a
fresh owner-bound read for one message, document/chunk/version family, or memory source. The
worker validates current generation before writing and uses a SQLite conditional upsert so a
late lower generation cannot overwrite a newer projection. A source already at a higher
generation acknowledges the obsolete hint; a source behind the event remains pending for
retry. Equal-generation delivery is safe and idempotent. Pending invalidations hide old
projections. `reconcile_sources` is explicitly bounded and repairs only caller-supplied
source IDs, reporting stale/orphan/version-conflict counts. `outbox_status` exposes only
counts and age, never identifiers or content.

The real PostgreSQL tests cover trigger atomicity, owner assignment, outbox RLS, generation increments,
deleted-document behavior, memory revoke/purge compatibility, RLS-backed canonical adapters,
duplicate delivery, restart delivery, late lower versions and routing-only consumer data.

Remaining activation blockers:

- **P0:** a production reviewed AEAD/key hierarchy and encrypted durable projection store;
  canonical content-equality fencing across a long-running source read and disclosure; and
  production retry/dead-letter policy for events whose canonical source remains unavailable.
- **P1:** retention/compaction policy for the append-only outbox; operational metrics for
  retries, version conflicts and reconciliation repairs; dependency scheduling for external
  attachment/source-unit graphs; and verified Claude Shell/Intent schema conformance.

The outbox closes canonical mutation versus event-delivery loss and reorder races. It does
not itself grant disclosure authority, replace canonical reads, or provide power-loss and
multi-host guarantees for the local SQLite projection.
