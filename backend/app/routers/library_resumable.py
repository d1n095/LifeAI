"""Founder-only resumable library upload HTTP.

Wires ``ResumableUploadSession`` into production routes without raising Caddy
or ``MAX_UPLOAD_BYTES``. Stored is not indexed. ``five_gb_supported`` stays false.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import require_founder
from app.large_file_lane.http_store import (
    HTTP_DECLARED_MAX_BYTES,
    SESSION_SUBDIR,
    ResumableHttpError,
    ResumableHttpStore,
    parse_session_id,
)
from app.limiter import limiter
from app.models.user import User
from app.storage.references import acquire_owner_erasure_lock

settings = get_settings()

router = APIRouter(
    prefix="/api/library/resumable",
    tags=["library-resumable"],
    dependencies=[Depends(require_founder)],
)


class CreateResumableSessionIn(BaseModel):
    declared_total_bytes: int = Field(gt=0, le=HTTP_DECLARED_MAX_BYTES)
    declared_filename: str | None = Field(default=None, max_length=255)


def get_resumable_store() -> ResumableHttpStore:
    return ResumableHttpStore(Path(get_settings().storage_root) / SESSION_SUBDIR)


def _http_error(exc: ResumableHttpError) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message, "five_gb_supported": False},
    )


async def _read_bounded_part(request: Request, *, max_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content-length") from exc
        if declared > max_bytes:
            raise HTTPException(
                status_code=413,
                detail={"code": "part_too_large", "message": "part exceeds HTTP 64 KiB bound"},
            )
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail={"code": "part_too_large", "message": "part exceeds HTTP 64 KiB bound"},
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/sessions")
@limiter.limit(f"{settings.rate_limit_library_import_per_minute}/minute")
def create_resumable_session(
    request: Request,
    payload: CreateResumableSessionIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
    store: ResumableHttpStore = Depends(get_resumable_store),
):
    acquire_owner_erasure_lock(db, user.id)
    if db.query(User).filter_by(id=user.id).first() is None:
        raise HTTPException(status_code=401, detail="Kontot finns inte längre.")
    try:
        return store.create(
            user.id,
            declared_total_bytes=payload.declared_total_bytes,
            declared_filename=payload.declared_filename,
        )
    except ResumableHttpError as exc:
        raise _http_error(exc) from exc


@router.get("/sessions")
def list_resumable_sessions(
    user: User = Depends(require_founder),
    store: ResumableHttpStore = Depends(get_resumable_store),
):
    try:
        return {"sessions": store.list_sessions(user.id), "five_gb_supported": False}
    except ResumableHttpError as exc:
        raise _http_error(exc) from exc


@router.get("/sessions/{session_id}")
def get_resumable_session(
    session_id: str,
    user: User = Depends(require_founder),
    store: ResumableHttpStore = Depends(get_resumable_store),
):
    try:
        return store.get(user.id, parse_session_id(session_id))
    except ResumableHttpError as exc:
        raise _http_error(exc) from exc


@router.put("/sessions/{session_id}/parts")
@limiter.limit(f"{settings.rate_limit_library_import_per_minute}/minute")
async def put_resumable_part(
    session_id: str,
    request: Request,
    user: User = Depends(require_founder),
    store: ResumableHttpStore = Depends(get_resumable_store),
):
    try:
        parsed = parse_session_id(session_id)
        raw_offset = request.headers.get("upload-offset")
        if raw_offset is None:
            raise HTTPException(
                status_code=400,
                detail={"code": "missing_upload_offset", "message": "Upload-Offset is required"},
            )
        try:
            expected_offset = int(raw_offset)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_upload_offset", "message": "Upload-Offset must be a non-negative integer"},
            ) from exc
        if expected_offset < 0:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_upload_offset", "message": "Upload-Offset must be a non-negative integer"},
            )
        data = await _read_bounded_part(request, max_bytes=store.part_max_bytes)
        return store.write_part(user.id, parsed, data, expected_offset=expected_offset)
    except ResumableHttpError as exc:
        raise _http_error(exc) from exc


@router.post("/sessions/{session_id}/finalize")
@limiter.limit(f"{settings.rate_limit_library_import_per_minute}/minute")
def finalize_resumable_session(
    session_id: str,
    request: Request,
    user: User = Depends(require_founder),
    store: ResumableHttpStore = Depends(get_resumable_store),
):
    try:
        return store.finalize(user.id, parse_session_id(session_id))
    except ResumableHttpError as exc:
        raise _http_error(exc) from exc


@router.delete("/sessions/{session_id}")
@limiter.limit(f"{settings.rate_limit_library_import_per_minute}/minute")
def abort_resumable_session(
    session_id: str,
    request: Request,
    user: User = Depends(require_founder),
    store: ResumableHttpStore = Depends(get_resumable_store),
):
    try:
        return store.abort(user.id, parse_session_id(session_id))
    except ResumableHttpError as exc:
        raise _http_error(exc) from exc
