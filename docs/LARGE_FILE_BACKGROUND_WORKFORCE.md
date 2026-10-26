# Large-file background workforce (parallel Gate 2 lane)

Isolated child of frozen `#306` @ `2444bcc3da8ffa5ca10ef3295bac13aa045769c6`.
Does not amend `#309`. Does **not** claim 5 GB support.

## What this implements

- Streaming import with a bounded buffer (default 64 KiB chunks, max two chunks).
- Durable offset checkpoints so a crash can resume without rereading accepted bytes.
- Isolation: background imports queue/backpressure instead of blocking Founder interactive work.
- Explicit `stored ≠ indexed ≠ understood`.

HTTP `MAX_UPLOAD_BYTES`, Caddy 30 MB, and worker container memory stay unchanged.
No new Alembic tables. Existing `knowledge_import_jobs` remains the production queue.

## What is still required for Gate 2

Measured 5 × 5 GB concurrent ingestion, including restart, exhaustion, corruption,
memory and interactive-responsiveness tests on real formats. Until those measurements
exist, `five_gb_supported` and `five_by_five_gb_measured` stay false.

See `docs/LARGE_FILE_UPLOAD_PLAN.md` (worker memory before resumable HTTP).
Successor `cursor/large-file-resumable-measure-91c1` adds durable declared-5GB sessions
and measured isolation without flipping `five_gb_supported`.
