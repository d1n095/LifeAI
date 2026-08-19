"""File-level contracts for source/storage architecture invariants.

Guards the boundaries Life needs for future encrypted, chunk-addressable source storage
without building that subsystem now. These tests read source/docstrings, not live vault state.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
STORAGE_BASE = REPO_ROOT / "backend/app/storage/base.py"
DOCUMENT_MODEL = REPO_ROOT / "backend/app/models/document.py"
SCHEMAS = REPO_ROOT / "backend/app/schemas.py"
REFERENCES = REPO_ROOT / "backend/app/storage/references.py"


def test_storage_backend_contract_separates_envelope_from_plaintext_identity():
    text = STORAGE_BASE.read_text()
    assert "encryption is not compression" in text
    assert "canonical plaintext source bytes" in text
    assert "open_read()" in text
    assert "decorator" in text or "envelope" in text


def test_stored_blob_sha256_is_plaintext_content_identity():
    text = STORAGE_BASE.read_text()
    assert "PLAINTEXT source bytes" in text
    assert "never encrypted/compressed envelope" in text.lower() or "never encrypted/compressed envelope size" in text


def test_document_content_preview_documented_as_derived_not_canonical():
    text = DOCUMENT_MODEL.read_text()
    assert "NOT canonical source truth" in text
    assert "storage_key" in text


def test_public_library_schema_does_not_expose_content_preview():
    """content_preview is internal/corpus-review cache — must not become an API leak surface."""
    text = SCHEMAS.read_text()
    assert "content_preview" not in text


def test_storage_key_reference_registry_exists_for_shared_blob_safety():
    """Deduped physical blobs require a machine-checkable reference registry before delete."""
    text = REFERENCES.read_text()
    assert "KNOWN_STORAGE_KEY_COLUMNS" in text
    assert "storage_key_still_referenced_global" in text
