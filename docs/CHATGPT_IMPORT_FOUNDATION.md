# ChatGPT Import Foundation — format-agnostic bootstrap

This foundation provides deterministic, resumable infrastructure for a future ChatGPT
export importer. It does **not** implement ChatGPT import support and contains no assumed
ChatGPT field names, object graph, filenames, or archive layout.

The real ChatGPT structural adapter remains blocked until a real export sample can be
inspected and its invariants verified. Synthetic newline-delimited fixtures exist only in
tests to prove the generic contract; they are not a claimed vendor format.

## Architecture

- The uploaded archive remains the canonical original as an existing owner-scoped
  `Document` in `LocalFilesystemStorage`. Import state never copies or replaces it.
- `structured_export_import` runs through the existing `mainai_jobs` queue, claim, lease,
  fencing, cancellation, audit, retry, and worker dispatch paths.
- `structured_import_runs` stores the adapter identity/version and durable checkpoint.
- `structured_import_items` stores stable item identity, exact in-archive provenance, and a
  deterministic result state: `discovered`, `stored`, `duplicate`, `parsed`, `unsupported`,
  `failed`, or `deferred`.
- Every item-result batch, checkpoint, progress update, and lease fence commits in one
  transaction. A stale worker therefore cannot publish item state after its lease is lost.
- Replay uses `(run_id, source_identity)` uniqueness. Already-durable outcomes are not
  rewritten; conservative adapter checkpoints can safely replay them.
- Adapters receive a seekable stream opened directly from canonical storage. They expose
  bounded item chunk iterators and checkpoints; neither the framework nor its contract
  requires the complete archive or JSON structure in memory.
- The capability has no provider role. No AI key, model, provider, or network call is needed.

This branch starts from `origin/claude/det-kommer-mer-879lcm`, where PR #61's provisional
`source_import_batches` work is not present. It therefore does not copy that schema or create
a competing corpus ledger. The run/item tables here are narrowly job-owned checkpoint state;
later integration may link them to the reviewed Source Foundation batch model where
compatible.

## Adapter boundary

An adapter must implement deterministic `discover()` and `iter_items()` operations, declare
a stable key/version, emit stable source identities and provenance, and resume strictly from
the supplied JSON checkpoint. Malformed individual records are emitted as per-item failures
rather than raised as package failures. Package-level corruption or a violated adapter
contract fails the job truthfully.

The production registry is intentionally empty. Adding the real ChatGPT adapter requires:

1. a real export sample approved for local inspection;
2. a documented structural contract based on observed data, not guesses;
3. bounded archive/JSON traversal for that observed structure;
4. provenance mapping back to exact archive entry and structural location;
5. adapter-specific malformed/collision/resume fixtures.

## ZIP intake safety

The existing ZIP validator remains the reusable archive-security boundary for current
knowledge imports: Zip Slip/absolute paths, nested-depth and shared expansion budgets,
compression ratio, file-count and per-entry limits, encrypted/malformed entries, and magic
bytes. This work additionally rejects Unix symlink entries and duplicate/case-colliding
paths while continuing with safe siblings.

The legacy `validate_and_extract_zip(raw: bytes)` API is intentionally not used by the new
structured-import framework because it materializes the outer archive and accepted entries.
A future format adapter for multi-GB exports must operate on the canonical storage stream at
the adapter boundary and may reuse the ZIP validation rules without using that legacy
materializing return shape.
