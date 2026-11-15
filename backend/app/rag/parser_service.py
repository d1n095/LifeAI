"""Secretless parser service for the isolated container deployment."""

from __future__ import annotations

import os
import re
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from app.rag.extract_isolated import ExtractionIsolationError, extract_text_in_subprocess


_SOCKET_PATH = Path(os.environ.get("PARSER_SOCKET_PATH", "/run/lifeai-parser/parser.sock"))
_MAX_INPUT_BYTES = 25 * 1024 * 1024
_SAFE_SUFFIX = re.compile(r"\.[a-z0-9]{1,10}\Z")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.post("/v1/extract")
async def extract_document(request: Request) -> Response:
    suffix = request.headers.get("x-document-suffix", ".txt").lower()
    if not _SAFE_SUFFIX.fullmatch(suffix):
        raise HTTPException(status_code=415, detail="Unsupported document type.")

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_INPUT_BYTES:
                raise HTTPException(status_code=413, detail="Document exceeds the parser input limit.")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid content length.") from exc

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_INPUT_BYTES:
            raise HTTPException(status_code=413, detail="Document exceeds the parser input limit.")
        chunks.append(chunk)

    try:
        text = extract_text_in_subprocess(f"input{suffix}", b"".join(chunks))
    except ExtractionIsolationError as exc:
        raise HTTPException(status_code=422, detail="Document could not be parsed.") from exc

    return Response(content=text.encode("utf-8"), media_type="text/plain; charset=utf-8")


def main() -> None:
    _SOCKET_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(_SOCKET_PATH.parent, 0o700)
    try:
        _SOCKET_PATH.unlink()
    except FileNotFoundError:
        pass
    os.umask(0o177)
    config = uvicorn.Config(app, uds=str(_SOCKET_PATH), access_log=False, log_level="warning")
    server = uvicorn.Server(config)
    uds_socket = config.bind_socket()
    try:
        os.chmod(_SOCKET_PATH, 0o600)
        server.run(sockets=[uds_socket])
    finally:
        uds_socket.close()
        _SOCKET_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
