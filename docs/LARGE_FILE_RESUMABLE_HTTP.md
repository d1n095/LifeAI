# Large-file resumable HTTP

Isolated child of `#313` @ `5cb9b46d6471213ec430c88fccc936514c4e0e39`
([#315](https://github.com/d1n095/LifeAI/pull/315)).
Does **not** amend `#309`–`#314`. Product SHA `292a09110a2528871d2f537987c37050416f3707`.

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
- every part is bound to the caller's exact committed `Upload-Offset`
- owner reservation quota toward 5×5 GB, disk headroom gate
- flock around append/finalize/abort
- spool `fsync` precedes an atomic, fsynced manifest commit
- restart discards only an uncommitted spool tail and fails closed if the spool is
  shorter than the committed offset
- a replay after an ambiguous response returns `offset_mismatch` instead of appending
  the same bytes twice
- finalize requires manifest offset, declared total, and physical spool size to agree
- stored is not indexed or understood

`PUT .../parts` requires an `Upload-Offset` request header containing the offset returned
by the previous successful request (zero for the first part). A missing, negative, stale,
or future offset is rejected; the client must read current session status before deciding
whether to retry. Model output or a client's assertion never overrides the durable offset.

## What this does not do

- No Caddy / `MAX_UPLOAD_BYTES` change
- No new Alembic table
- No 5 GB file is created or certified
- No worker indexing of the spool
- No live provider, merge, deploy, or autonomy GO

HTTP limits stay unchanged. A session may *declare* 5 GB. That is not support.
