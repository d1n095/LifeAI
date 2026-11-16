from __future__ import annotations

from fastapi.testclient import TestClient

from app.rag import parser_service


def test_parser_service_accepts_a_safe_extension_and_returns_extracted_text(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_extract(filename: str, raw: bytes) -> str:
        seen["filename"] = filename
        seen["raw"] = raw
        return "extracted"

    monkeypatch.setattr(parser_service, "extract_text_in_subprocess", fake_extract)
    response = TestClient(parser_service.app).post(
        "/v1/extract",
        content=b"document bytes",
        headers={"x-document-suffix": ".md"},
    )

    assert response.status_code == 200
    assert response.content == b"extracted"
    assert seen == {"filename": "input.md", "raw": b"document bytes"}


def test_parser_service_rejects_path_like_extension_before_parsing(monkeypatch) -> None:
    monkeypatch.setattr(
        parser_service,
        "extract_text_in_subprocess",
        lambda *_args: (_ for _ in ()).throw(AssertionError("parser must not be called")),
    )

    response = TestClient(parser_service.app).post(
        "/v1/extract",
        content=b"document bytes",
        headers={"x-document-suffix": "/../../secret.pdf"},
    )

    assert response.status_code == 415


def test_parser_service_rejects_oversized_content_before_parsing(monkeypatch) -> None:
    monkeypatch.setattr(
        parser_service,
        "extract_text_in_subprocess",
        lambda *_args: (_ for _ in ()).throw(AssertionError("parser must not be called")),
    )

    response = TestClient(parser_service.app).post(
        "/v1/extract",
        content=b"x" * (parser_service._MAX_INPUT_BYTES + 1),
        headers={"x-document-suffix": ".txt"},
    )

    assert response.status_code == 413
