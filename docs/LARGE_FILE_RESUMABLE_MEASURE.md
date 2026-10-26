# Large-file resumable sessions and measured isolation

Isolated child of `#311` @ `c3373f93cb6d3881ed6560cdd106ac1852c54a1e`.
Does **not** amend `#309`, `#310`, `#311`, or `#312`.

`first_cycle_go=false`. `five_gb_supported=false`. `five_by_five_gb_measured=false`.

## What this adds

- Durable filesystem resumable sessions (`ResumableUploadSession`) that may *declare* a 5 GB+ total.
- Crash restore from `manifest.json` + `spool.bin`. Finalize hashes the spool.
- Measured interactive isolation while one background stream runs.
- Bounded-memory check on an 8 MiB stream (peak buffer stays one chunk).

## What this does not do

- No production HTTP resumable upload route
- No Caddy / `MAX_UPLOAD_BYTES` change
- No new Alembic table
- No 5 GB file is created or certified
- No live provider, merge, deploy, or autonomy GO

HTTP limits stay unchanged until worker memory is measured on real formats.
See `docs/LARGE_FILE_UPLOAD_PLAN.md`.
