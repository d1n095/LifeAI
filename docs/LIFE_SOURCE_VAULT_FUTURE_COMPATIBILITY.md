# LIFE SOURCE VAULT — FUTURE COMPATIBILITY REVIEW (2026-08-18)

## Scope

Founder direction: before any large-scale corpus ingestion happens, inspect whether the
current source-storage architecture would create expensive future lock-in against a long-term
target shape -- `original -> hashing -> deduplication -> chunking -> compression -> encryption
-> durable/cold storage`, with Life working primarily from derived structured knowledge/
indexes and retrieving/decrypting only relevant original chunks when needed. Explicitly NOT a
mandate to build that target shape now -- only to add a small compatibility safeguard if the
CURRENT architecture would otherwise lock in an expensive rewrite later, and otherwise to
document the grounded gap.

**Conclusion up front: no code change is needed.** The existing abstraction boundary already
isolates every caller from the storage backend's own implementation, so every future
requirement below can be added as a NEW backend (or a wrapping layer) later without touching
`app/rag/library_import.py`, `app/worker.py`, `app/routers/library.py`, or any other caller.
One specific risk is worth flagging prominently now, in writing, so it is not silently
forgotten when encryption eventually gets built (see "The one real risk" below) -- but nothing
about it requires acting today.

## What already exists, verified against the actual code

- **Hashing**: `app/storage/local_fs.py` -- every blob is content-addressed by its own sha256
  (`storage_key = "{sha256[:2]}/{sha256}"`), computed incrementally while streaming, never
  derived from a user-controlled filename.
- **Deduplication**: `app/storage/references.py::store_content_with_reference_lock()` -- a
  real, DB-advisory-lock-protected reference-counting mechanism; identical content from two
  different documents/jobs resolves to the exact same `storage_key`, verified this session via
  `tests/backend/rag/test_library_import.py`'s own reference-lock race test (the one
  confirmed-unrelated flake encountered repeatedly this session is IN this exact test file --
  a real, already-known concurrency edge case in an already-hardened area, not a new finding).
- **Streaming, not buffering**: `write_stream()` never holds more than `chunk_size` bytes of
  the input in memory, aborting the instant `max_bytes` is exceeded -- already safe for large
  original files.
- **Atomic, race-safe writes**: `os.link()` as the primary publish mechanism (atomic,
  `FileExistsError` on a genuine race, no check-then-act gap), plus a real `fcntl.flock()`
  around publish/unlink -- this took multiple founder review passes (Pass 31/32, per the
  module's own docstring) to get right; not something to casually re-derive when adding a new
  backend.
- **Integrity verification**: `verify()` re-reads a blob and confirms its actual sha256/size
  match what the database recorded -- the recovery-time check this whole system already relies
  on.
- **Clean abstraction**: `app/storage/base.py`'s `StorageBackend` ABC is EXACTLY the extension
  point a future backend needs -- "a future backend (object storage, etc.) is a new class
  implementing this interface, not a rewrite of every caller" is the abstraction's own stated
  purpose, already true today, not aspirational.
- **Derived knowledge != source truth, already enforced, not just intended**: `documents.
  storage_key`/`file_path` are immutable once set (a DB trigger enforces this, per this
  session's own CI log evidence from `test_source_foundation_bootstrap_privileges.py`).
  `memory_source_units`/`document_source_units` (migration 0019/0037) keep an immutable
  `content_text`/`content_hash` snapshot of the SOURCE separate from `knowledge_claims` (the
  derived, interpreted layer) -- reopening and reinterpreting a source later, while the old
  interpretation stays superseded-not-deleted, is already this codebase's own established
  discipline (the exact same supersession-preserves-history pattern this entire mission's own
  foundations reuse).
- **Original recoverable/verifiable**: the combination of immutable `storage_key` + `content_
  hash` + `verify()` already gives exactly this property.

## What does NOT exist yet -- genuine gaps, not silently assumed away

- **No compression.** Blobs are stored as raw bytes. Not a lock-in risk: compression is a
  transparent transform a future backend (or a wrapping stream in the existing one) can add
  around `write_stream()`/`open_read()` without changing the interface or any caller.
- **No encryption.** Blobs are plaintext on a single private VPS Docker volume today (per
  `local_fs.py`'s own docstring: "Local, private-VPS-volume storage backend"). This is the
  largest genuine gap relative to the founder's target shape -- see "The one real risk" below
  for the one thing worth getting right when this is eventually built.
- **No key/access-policy separation mechanism** -- does not exist yet because encryption does
  not exist yet. When built, keys and access policy belong in their own store, never embedded
  in or alongside the encrypted blob itself (this is a design requirement for that future work,
  not a gap in today's architecture, since today's architecture has no keys to separate).
- **No cold/durable storage tiering.** Single local filesystem volume only. The `StorageBackend`
  ABC is what makes adding a tiered/cold backend later a new class, not a rewrite.
- **Storage-level chunking is a different concept from RAG's `document_chunks`.** `document_
  chunks` exist for semantic retrieval (splitting text into embeddable units) -- they are not
  a storage-layer partitioning of the original blob. Future partial-read/partial-decrypt
  support (see below) would need its own storage-level notion of a range or shard, not a reuse
  of RAG chunk boundaries, which are about meaning, not bytes.

## The one real risk worth writing down now

The founder specifically warned against "giant encrypted blobs that require full-file
decryption for tiny reads." `open_read()` today returns a plain `BinaryIO` for the WHOLE blob
at its `storage_key` -- for today's unencrypted backend, a caller can already `seek()`/`read()`
an arbitrary range cheaply, so there is no current problem. The risk is specifically in HOW a
future encrypted backend gets implemented: the easiest, most naive approach -- encrypt each
`storage_key`'s entire blob as a single AEAD-sealed unit -- would silently recreate exactly the
anti-pattern the founder wants avoided, because `open_read()`'s contract does not by itself
prevent that choice.

**This is not something to fix today** -- there is no encryption to retrofit yet, and building
one now would be exactly the "huge storage subsystem" the founder said not to build. It is
recorded here as a **grounded design constraint for whenever encryption is built**: a future
encrypted backend must encrypt at a granularity smaller than "one whole original file" (e.g.
per-shard or per-range, with an index of {shard -> nonce/tag} kept alongside, itself separate
from the key material) so that reading a small part of a large document never requires
decrypting the whole thing. The existing `StorageBackend.open_read()` contract is compatible
with this -- a future implementation can return a lazily-decrypting stream that seeks/decrypts
only the ranges actually read -- so no interface change is needed now to keep this option open
later.

## Grounded future requirements (not built, explicitly not silently assumed unnecessary)

- At-rest encryption of stored originals, implemented at a sub-file granularity (see above).
- A separate key/access-policy store, never colocated with encrypted content.
- Compression, likely as a transparent wrapping layer around the existing stream-based
  write/read contract.
- Cold/tiered storage as an additional `StorageBackend` implementation (or a policy layer that
  migrates blobs between a hot and cold backend based on access recency).
- A storage-level (not RAG-semantic) partial-read/range mechanism, needed once encryption
  exists at sub-file granularity -- today's plain `BinaryIO.seek()` already covers the
  unencrypted case, so this has no current urgency.

None of the above blocks any work already built or planned in this mission's own stack
(`capability_reality`/`founder_memory`/`diagnosis`/`corpus_trial`/`candidate_learning_signals`)
-- those foundations record facts and provenance, they do not touch original blob storage
directly. This review exists purely to confirm that a LATER, large-scale corpus ingestion
effort will not have to fight the storage layer's own past decisions to get encryption/
compression/cold-storage right -- and it will not, because the abstraction already in place is
sound.
