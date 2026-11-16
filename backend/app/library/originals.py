"""Shared-original retrieval — the ONE authorized capability for handing back a Library
source's untouched original bytes, used by BOTH the Founder HTTP endpoint
(`GET /api/library/{source_id}/original`, app/routers/library.py) and Life's own explicitly
authorized, read-only path (`load_original_as_life`).

Design intent (Blueprint §16.6; the one verified "missing connection" from the §16 audit):
the Library already preserves content-addressed, hash-verified, owner-isolated originals
(app/storage/local_fs.py) — what was missing is a route to hand a *whole* original back to
the Founder (for non-media sources) or to Life at all. This module is that route, and only
that route: one function, owner-scoped, integrity-checked, audited, read-only. It deliberately
does NOT redesign storage, Library, ownership, RLS or the memory engine — it composes them.

Trust-boundary rules this module enforces, and the ones it explicitly does NOT relax:
  * Owner authorization is checked HERE, not left to the caller: a source is resolvable only
    through `owner_id`, with the same `deleted_at IS NULL` exclusion the Founder's Library
    listing already uses (`_visible_document_query`). RLS remains the backstop; this is
    defense-in-depth, the same convention app/rag/vector_store.py's `search()` follows.
  * Integrity is verified before any byte is returned — the stored blob must still hash to the
    Document's recorded sha256 (`storage.verify`), so a missing/corrupted/tampered object is a
    hard failure, never silently-wrong bytes.
  * Internal storage keys and filesystem paths NEVER cross this boundary: callers get bytes +
    a sanitized filename + the public sha256 checksum, nothing that reveals where or how the
    blob is stored.
  * READ ACCESS IS NOT DISCLOSURE AUTHORITY. Returning the original bytes to Life (or a
    delegated worker) authorizes Life to *read* them; it does NOT authorize sending them to any
    external model. Egress stays governed by app/egress_policy.py on any later provider call —
    this module performs no provider call and no egress of its own, by construction. See the
    Founder's standing rule: "Do not confuse authorization to read an original with
    authorization to disclose it to an external model."
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
import uuid
from typing import Literal

from sqlalchemy.orm import Session

from app.audit import record_audit
from app.models.document import Document, IndexStatus
from app.storage import get_storage
from app.storage.base import StorageError

# IndexStatuses where no durable original is on disk yet — the upload is still in flight or
# only ever lived in the legacy in-DB media_blob. Retrieval of the content-addressed original
# is "not available" for these, distinct from "source not found".
_PRE_STORAGE_STATUSES = frozenset(
    {IndexStatus.pending, IndexStatus.received, IndexStatus.original_storing}
)

ActorType = Literal["founder", "life", "life_worker"]


class OriginalAccessError(Exception):
    """Base for every typed failure below — callers map these to their own surface (HTTP
    status, tool error) without ever leaking storage internals in the message."""


class OriginalNotFound(OriginalAccessError):
    """No source with this id is visible to this owner (never uploaded, wrong owner, or
    soft-deleted/purged) — deliberately indistinguishable from the caller's side, so a probe
    can't tell "not yours" apart from "doesn't exist"."""


class OriginalNotAvailable(OriginalAccessError):
    """The source exists but has no retrievable content-addressed original (still uploading,
    or a legacy row whose bytes only ever lived in the in-DB media_blob)."""


class OriginalIntegrityError(OriginalAccessError):
    """The stored object is missing or no longer hashes to the Document's recorded sha256 —
    fail closed rather than return bytes that don't match their own integrity anchor."""


@dataclasses.dataclass(frozen=True)
class LoadedOriginal:
    """What both callers get back — bytes plus the minimum safe metadata, never a storage_key
    or filesystem path."""

    content: bytes
    filename: str
    checksum: str | None
    size_bytes: int
    # The DECLARED media type from upload, kept for reference only. The HTTP layer deliberately
    # does NOT serve bytes under this type (an original could be text/html or image/svg+xml,
    # which a browser would render inline) — it forces application/octet-stream + attachment.
    declared_media_type: str | None


# Conservative filename sanitizer: keep a readable download name, strip anything that could be
# a path, a header-injection vector, or a control character. Never trust the stored
# original_filename as-is in a Content-Disposition header.
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._ -]")


def safe_filename(raw: str | None, *, source_id: uuid.UUID) -> str:
    """Return a safe, non-empty ASCII fallback-able download name. Drops directory components
    and control/odd characters; collapses to a deterministic `original-<id>` if nothing usable
    remains, so the Content-Disposition header is always well-formed."""
    if raw:
        base = raw.replace("\\", "/").rsplit("/", 1)[-1]  # strip any path, both separators
        base = _UNSAFE_FILENAME_CHARS.sub("_", base).strip(" .")
        base = base[:200]
        if base:
            return base
    return f"original-{source_id}"


def load_original(
    db: Session,
    *,
    owner_id: uuid.UUID,
    source_id: uuid.UUID,
    actor_type: ActorType,
    actor_id: uuid.UUID | None,
    access_label: str,
    request=None,
) -> LoadedOriginal:
    """Resolve, authorize, integrity-check, audit and return one source's untouched original.

    This is the single enforcement point shared by every caller. `actor_type`/`actor_id`/
    `access_label` only shape the audit record — they NEVER widen authorization: every caller
    is scoped to `owner_id`, and a `life`/`life_worker` actor gets exactly the same owner-bound
    visibility the Founder does, never more. The source bytes are read with no transformation
    and the Document row is never written to, so this can never mutate the original.
    """
    document = (
        db.query(Document)
        .filter(Document.id == source_id, Document.uploaded_by == owner_id, Document.deleted_at.is_(None))
        .first()
    )
    if document is None:
        raise OriginalNotFound("Källan hittades inte.")

    if document.storage_key is None or document.status in _PRE_STORAGE_STATUSES:
        raise OriginalNotAvailable("Originalet är inte tillgängligt för den här källan.")

    storage = get_storage()

    # Integrity gate. When the Document carries its sha256 (every new upload does), the stored
    # blob MUST still verify against it — a missing file, a size change or a content change all
    # fail here. A legacy row with no recorded checksum can only be existence-checked.
    if document.checksum:
        if not storage.verify(document.storage_key, expected_sha256=document.checksum, expected_size=document.size_bytes):
            raise OriginalIntegrityError("Originalets integritet kunde inte verifieras.")
    elif not storage.exists(document.storage_key):
        raise OriginalNotAvailable("Originalet är inte tillgängligt för den här källan.")

    try:
        with storage.open_read(document.storage_key) as handle:
            content = handle.read()
    except (FileNotFoundError, StorageError) as exc:
        # Lost between verify() and open() (a concurrent purge), or an I/O error — treat as an
        # integrity failure, never a partial/empty "success".
        raise OriginalIntegrityError("Originalet kunde inte läsas.") from exc

    # Re-check the bytes we actually read, closing the verify()->open() TOCTOU window: the file
    # could have been repaired/replaced in between. Cheap for the <=60 MB Library cap.
    if document.checksum:
        if hashlib.sha256(content).hexdigest() != document.checksum:
            raise OriginalIntegrityError("Originalets innehåll matchar inte dess kontrollsumma.")

    # Auditable: one row per retrieval, attributed to the real actor (never inferred), with no
    # storage_key in the detail. commit=True so the trail survives even a read-only request.
    record_audit(
        db,
        user_id=actor_id,
        action="original_access",
        entity_type="document",
        entity_id=str(source_id),
        detail=f"actor_type={actor_type} label={access_label[:80]}",
        request=request,
        commit=True,
    )

    return LoadedOriginal(
        content=content,
        filename=safe_filename(document.original_filename, source_id=source_id),
        checksum=document.checksum,
        size_bytes=len(content),
        declared_media_type=document.media_type,
    )


def load_original_as_life(
    db: Session,
    *,
    owner_id: uuid.UUID,
    source_id: uuid.UUID,
    worker_label: str = "life",
) -> LoadedOriginal:
    """Life's explicitly authorized, read-only retrieval of an original she is already allowed
    to see — the "same underlying capability through an authorized tool interface" the contract
    asks for, as a narrow function rather than any unrestricted DB/filesystem access.

    `worker_label` names the delegated worker for the audit trail (least-privilege, bounded).
    Authority is identical to `load_original` and strictly owner-scoped: Life can never reach
    another owner's original, nor a revoked/purged/soft-deleted one. Reading here does NOT
    authorize disclosing the bytes to an external model — see the module docstring.
    """
    return load_original(
        db,
        owner_id=owner_id,
        source_id=source_id,
        actor_type="life_worker",
        actor_id=None,
        access_label=worker_label,
    )
