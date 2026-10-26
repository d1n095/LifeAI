# Large-file resumable HTTP

Isolated child of `#313` @ `5cb9b46d6471213ec430c88fccc936514c4e0e39`.
Does **not** amend `#309`–`#314`.

`first_cycle_go=false`. `five_gb_supported=false`. `five_by_five_gb_measured=false`.

## What this adds

Founder-authenticated production routes on the `#313` spool/checkpoint:

- `POST /api/library/resumable/sessions`
- `GET /api/library/resumable/sessions`
- `GET /api/library/resumable/sessions/{id}`
- `PUT /api/library/resumable/sessions/{id}/parts`
- `POST /api/library/resumable/sessions/{id}/finalize`
- `DELETE /api/library/resumable/sessions/{id}`

Security and restart properties:

- `require_founder` on every route
- owner-scoped paths; session ids are UUIDs only
- basename-only filenames
- 64 KiB parts (under Caddy 30 MB)
- owner reservation quota toward 5×5 GB, disk headroom gate
- flock around append/finalize/abort
- crash resume from `manifest.json` + `spool.bin`
- stored is not indexed or understood

## What this does not do

- No Caddy / `MAX_UPLOAD_BYTES` change
- No new Alembic table
- No 5 GB file is created or certified
- No worker indexing of the spool
- No live provider, merge, deploy, or autonomy GO

HTTP limits stay unchanged. A session may *declare* 5 GB. That is not support.
